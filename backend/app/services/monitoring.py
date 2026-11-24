"""Watchdog repository events, persisted alerts, and monitoring lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Analysis, AutoscalingEvent, Deployment, MonitoringAlert, MonitoringEvent, PipelineStep, Project

try:
    from watchdog.events import (
        DirCreatedEvent, DirDeletedEvent, DirMovedEvent, FileCreatedEvent, FileDeletedEvent,
        FileModifiedEvent, FileMovedEvent, FileSystemEventHandler,
    )
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except ImportError:  # Keep the API available while surfacing a clear dependency state.
    WATCHDOG_AVAILABLE = False

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    Observer = None
    DirCreatedEvent = DirDeletedEvent = DirMovedEvent = object
    FileCreatedEvent = FileDeletedEvent = FileModifiedEvent = FileMovedEvent = object


BASE_EXCLUDED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "pycache", "build", "dist",
    ".docker", "docker-cache", ".cache", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".nuxt",
    ".turbo", "site-packages", "bower_components", "target", "vendor",
}
SENSITIVE_NAMES = {".aws", ".ssh", ".kube", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt"}
ACTIVE_DEPLOYMENT_STATUSES = {"QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"}
ACTIVE_ANALYSIS_STEPS = {"Repository Analysis", "Repository Intelligence", "Tests", "Security", "Docker", "Kubernetes"}


def _utc(value: datetime | str | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            result = datetime.now(timezone.utc)
    else:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def _is_newer(value: datetime, old: datetime) -> bool:
    return _utc(value) > _utc(old)


def upsert_alert(db: Session, *, fingerprint: str, source: str, alert_type: str, severity: str,
                 title: str, message: str, project_id: int | None = None, deployment_id: int | None = None,
                 observed_at: datetime | str | None = None) -> MonitoringAlert:
    now = _utc(observed_at)
    row = db.query(MonitoringAlert).filter(MonitoringAlert.fingerprint == fingerprint).first()
    if row is None:
        row = MonitoringAlert(fingerprint=fingerprint, source=source, alert_type=alert_type, severity=severity,
                              title=title[:200], message=message[:1000], project_id=project_id, deployment_id=deployment_id,
                              occurrence_count=1, is_resolved=False, first_seen=now, last_seen=now)
        db.add(row)
        db.flush()
        return row
    if _is_newer(now, row.last_seen):
        row.occurrence_count += 1
        row.last_seen = now
        row.message = message[:1000]
    row.source = source
    row.alert_type = alert_type
    row.severity = severity
    row.title = title[:200]
    row.project_id = project_id
    row.deployment_id = deployment_id
    row.is_resolved = False
    row.resolved_at = None
    return row


def _excluded(relative_path: str, extra_patterns: list[str]) -> bool:
    path = relative_path.replace("\\", "/").strip("/")
    parts = [part.lower() for part in path.split("/") if part]
    if any(part in BASE_EXCLUDED_DIRS or part in SENSITIVE_NAMES for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    if any(Path(part).suffix.lower() in SENSITIVE_SUFFIXES or "secret" in part.lower() or "credential" in part.lower() for part in parts):
        return True
    normalized_patterns = [pattern.strip().replace("\\", "/") for pattern in extra_patterns if pattern.strip()]
    return any(fnmatchcase(path, pattern) or any(fnmatchcase(part, pattern) for part in parts) for pattern in normalized_patterns)


class _QueueHandler(FileSystemEventHandler):
    def __init__(self, service: "MonitoringService"):
        super().__init__()
        self.service = service

    def on_created(self, event):
        self.service.handle_fs_event("created", event.src_path, is_directory=event.is_directory)

    def on_modified(self, event):
        if not event.is_directory:
            self.service.handle_fs_event("modified", event.src_path)

    def on_deleted(self, event):
        self.service.handle_fs_event("deleted", event.src_path, is_directory=event.is_directory)

    def on_moved(self, event):
        self.service.handle_fs_event("moved", event.src_path, destination=event.dest_path, is_directory=event.is_directory)


class MonitoringService:
    def __init__(self):
        self.root = Path(settings.monitoring_workspace_root or settings.workspace_dir).resolve()
        self._extra_patterns = [value.strip() for value in settings.monitoring_exclude_patterns.split(",") if value.strip()]
        self._queue: queue.Queue = queue.Queue(maxsize=20000)
        self._lock = threading.RLock()
        self._registered: set[int] = set()
        self._watches: dict[str, Any] = {}
        self._observer = None
        self._processor: threading.Thread | None = None
        self._kubernetes_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_error: str | None = None
        self._last_event_at: str | None = None
        self._latest_snapshot: dict | None = None
        self._watchdog_failure_recorded = False
        self._started = False

    def start(self) -> None:
        if not settings.monitoring_enabled or self._started:
            return
        self._stop.clear()
        self._started = True
        self._processor = threading.Thread(target=self._process_queue, name="codedeck-monitor-events", daemon=True)
        self._processor.start()
        db = SessionLocal()
        try:
            with self._lock:
                self._registered.update(row[0] for row in db.query(Project.id).all())
        except Exception as exc:
            self._last_error = f"Could not load projects for monitoring ({type(exc).__name__})"
        finally:
            db.close()

        if not WATCHDOG_AVAILABLE:
            self._last_error = "Python Watchdog is not installed"
        elif not self.root.is_dir():
            self._last_error = "Configured workspace root is unavailable"
        else:
            try:
                observer = Observer()
                handler = _QueueHandler(self)
                self._observer = observer
                directory_events = [DirCreatedEvent, DirDeletedEvent, DirMovedEvent]
                observer.schedule(handler, str(self.root), recursive=False, event_filter=directory_events, follow_symlink=False)
                with self._lock:
                    project_ids = list(self._registered)
                for project_id in project_ids:
                    self._schedule_project_tree(project_id)
                observer.start()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"Watchdog observer could not start ({type(exc).__name__})"
                if self._observer:
                    try:
                        self._observer.stop()
                        self._observer.join(timeout=5)
                    except Exception:
                        pass
                self._observer = None

        if self._last_error and settings.monitoring_enabled:
            self._record_service_failure(self._last_error)

        if settings.deployment_target == "local":
            self._kubernetes_thread = threading.Thread(target=self._local_kubernetes_loop, name="codedeck-kubernetes-monitor", daemon=True)
            self._kubernetes_thread.start()

    def stop(self) -> None:
        self._stop.set()
        observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=10)
            except Exception:
                pass
        for thread in (self._processor, self._kubernetes_thread):
            if thread and thread.is_alive():
                thread.join(timeout=10)
        with self._lock:
            self._watches.clear()
        self._started = False

    def register_project(self, project_id: int) -> None:
        with self._lock:
            self._registered.add(int(project_id))
        if self._observer and self._observer.is_alive():
            self._schedule_project_tree(int(project_id))

    def _project_root(self, project_id: int) -> Path:
        return (self.root / f"project_{project_id}").resolve()

    def _safe_relative(self, project_id: int, raw_path: str) -> str | None:
        try:
            candidate = Path(raw_path).resolve(strict=False)
            project_root = self._project_root(project_id)
            if not project_root.is_relative_to(self.root) or not candidate.is_relative_to(project_root):
                return None
            relative = candidate.relative_to(project_root).as_posix()
            if relative == ".":
                return ""
            if _excluded(relative, self._extra_patterns):
                return None
            return relative
        except (OSError, RuntimeError, ValueError):
            return None

    def _identify_path(self, raw_path: str) -> tuple[int, str] | None:
        with self._lock:
            project_ids = tuple(self._registered)
        for project_id in project_ids:
            relative = self._safe_relative(project_id, raw_path)
            if relative is not None:
                return project_id, relative
        return None

    def handle_fs_event(self, event_type: str, raw_path: str, destination: str | None = None, is_directory: bool = False) -> None:
        source = self._identify_path(raw_path)
        dest = self._identify_path(destination) if destination else None
        if is_directory:
            if source and source[1] == "":
                self._enqueue_control("watch_remove" if event_type in {"deleted", "moved"} else "watch_add", source[0], raw_path)
            elif source and source[1]:
                self._enqueue_control("watch_remove" if event_type in {"deleted", "moved"} else "watch_add", source[0], raw_path)
            if event_type == "moved" and dest and dest[1]:
                self._enqueue_control("watch_add", dest[0], destination)
            return

        if event_type == "moved":
            if source and dest and source[0] == dest[0]:
                project_id, relative = dest
                summary = f"Moved from {source[1]} to {relative}"
            elif dest:
                project_id, relative = dest
                event_type, summary = "created", "File moved into the monitored repository workspace"
            elif source:
                project_id, relative = source
                event_type, summary = "deleted", "File moved out of the monitored repository workspace"
            else:
                return
        elif source:
            project_id, relative = source
            summary = {"created": "Repository file created", "modified": "Repository file modified", "deleted": "Repository file deleted"}.get(event_type, "Repository file event")
        else:
            return
        if not relative or not event_type:
            return
        payload = {"kind": "file_event", "project_id": project_id, "relative_path": relative,
                   "event_type": event_type, "occurred_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "coalesced": 1}
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._last_error = "Monitoring event queue is full; some file events may have been dropped"

    def _enqueue_control(self, operation: str, project_id: int, path: str) -> None:
        if not WATCHDOG_AVAILABLE:
            return
        try:
            self._queue.put_nowait({"kind": operation, "project_id": project_id, "path": path})
        except queue.Full:
            self._last_error = "Monitoring event queue is full; directory watches could not be updated"

    def _schedule_project_tree(self, project_id: int, start_path: Path | None = None) -> None:
        if not self._observer or not self.root.is_dir():
            return
        root = self._project_root(project_id)
        try:
            if not root.is_relative_to(self.root) or not root.is_dir():
                return
        except (OSError, ValueError):
            return
        base = start_path or root
        base = base.resolve(strict=False)
        if not base.is_relative_to(root) or not base.is_dir() or _excluded(base.relative_to(root).as_posix(), self._extra_patterns):
            return
        directories: list[Path] = []
        for current, dirs, _files in os.walk(base, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink() and not _excluded((current_path / name).relative_to(root).as_posix(), self._extra_patterns)]
            directories.append(current_path)
        event_types = [DirCreatedEvent, DirDeletedEvent, DirMovedEvent, FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent]
        for directory in directories:
            key = str(directory)
            with self._lock:
                if key in self._watches:
                    continue
                try:
                    watch = self._observer.schedule(_QueueHandler(self), key, recursive=False, event_filter=event_types, follow_symlink=False)
                    self._watches[key] = watch
                except (OSError, RuntimeError, FileNotFoundError):
                    continue

    def _unschedule_tree(self, project_id: int, path: Path) -> None:
        if not self._observer:
            return
        project_root = self._project_root(project_id)
        try:
            target = path.resolve(strict=False)
            if not target.is_relative_to(project_root):
                return
        except (OSError, RuntimeError, ValueError):
            return
        with self._lock:
            matches = [key for key in self._watches if Path(key) == target or target in Path(key).parents]
            for key in matches:
                watch = self._watches.pop(key, None)
                if watch:
                    try:
                        self._observer.unschedule(watch)
                    except (KeyError, RuntimeError):
                        pass

    def _process_queue(self) -> None:
        pending: dict[tuple[int, str, str], dict] = {}
        debounce = settings.monitoring_event_debounce_seconds
        while not self._stop.is_set() or not self._queue.empty() or pending:
            if self._observer is not None and not self._observer.is_alive() and not self._stop.is_set() and not self._watchdog_failure_recorded:
                self._last_error = "Watchdog observer stopped unexpectedly"
                self._record_service_failure(self._last_error)
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                item = None
            if item:
                if item["kind"] in {"watch_add", "watch_remove"}:
                    if item["kind"] == "watch_add":
                        self._schedule_project_tree(item["project_id"], Path(item["path"]))
                    else:
                        self._unschedule_tree(item["project_id"], Path(item["path"]))
                else:
                    key = (item["project_id"], item["relative_path"], item["event_type"])
                    existing = pending.get(key)
                    if existing and item["event_type"] == "modified":
                        item["coalesced"] = existing["coalesced"] + 1
                    item["queued_at"] = time.monotonic()
                    pending[key] = item
            now = time.monotonic()
            for key, payload in list(pending.items()):
                if payload["event_type"] != "modified" or now - payload["queued_at"] >= debounce or self._stop.is_set():
                    pending.pop(key, None)
                    self._persist_file_event(payload)
            if item:
                self._queue.task_done()

    def _context(self, db: Session, project: Project) -> tuple[str, int | None, int | None]:
        analysis = db.query(Analysis).filter(Analysis.project_id == project.id).first()
        running = db.query(PipelineStep).filter(PipelineStep.project_id == project.id, PipelineStep.status == "running").all()
        active = db.query(Deployment).filter(Deployment.project_id == project.id, Deployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES)).order_by(Deployment.created_at.desc()).first()
        if active:
            info = active.details if isinstance(active.details, dict) else {}
            phase = str(info.get("phase") or active.status).upper()
            context = "build" if phase in {"BUILDING"} else "deployment"
            return context, active.id, analysis.id if analysis else None
        if project.status in {"deploying", "staging_cancel_requested", "deploying_green"} or any(step.name == "Staging" for step in running):
            return "deployment", None, analysis.id if analysis else None
        if any(step.name in {"Docker", "Kubernetes"} for step in running):
            return "build", None, analysis.id if analysis else None
        if project.status == "analyzing" or any(step.name in ACTIVE_ANALYSIS_STEPS - {"Docker", "Kubernetes"} for step in running):
            return "analysis", None, analysis.id if analysis else None
        return "idle", None, analysis.id if analysis else None

    def _persist_file_event(self, payload: dict) -> None:
        db = SessionLocal()
        try:
            project = db.get(Project, payload["project_id"])
            if not project:
                return
            context, deployment_id, analysis_id = self._context(db, project)
            coalesced = payload.get("coalesced", 1)
            summary = payload["summary"]
            if coalesced > 1 and payload["event_type"] == "modified":
                summary = f"{summary} ({coalesced} updates coalesced)"
            db.add(MonitoringEvent(project_id=project.id, deployment_id=deployment_id, analysis_id=analysis_id,
                                   relative_path=payload["relative_path"], event_type=payload["event_type"],
                                   occurred_at=_utc(payload["occurred_at"]), workflow_context=context,
                                   processing_status="recorded", summary=summary))
            db.flush()
            if payload["event_type"] == "deleted" and self._project_root(project.id).is_dir() and context == "idle":
                upsert_alert(db, fingerprint=f"repository-delete:{project.id}:{payload['relative_path']}", source="repository",
                             alert_type="repository_file_deleted", severity="warning", title="Repository file deleted",
                             message=f"{project.repository_name}: {payload['relative_path']} was deleted outside an active CodeDeck operation.",
                             project_id=project.id, observed_at=payload["occurred_at"])
            db.commit()
            self._last_event_at = payload["occurred_at"]
            self._last_error = None
        except Exception as exc:
            db.rollback()
            self._last_error = f"Monitoring event could not be persisted ({type(exc).__name__})"
        finally:
            db.close()

    def process_deployment_outcome(self, db: Session, deployment: Deployment) -> None:
        if (deployment.status or "").lower() not in {"failed", "error"}:
            return
        deployment_id = getattr(deployment, "id", None)
        if deployment_id is None:
            return
        upsert_alert(db, fingerprint=f"deployment-failed:{deployment_id}", source="deployment",
                     alert_type="deployment_failure", severity="critical", title="Deployment failed",
                     message=f"Project {deployment.project_id} deployment {deployment_id} failed. Review deployment logs for details.",
                     project_id=deployment.project_id, deployment_id=deployment_id)

    def _record_service_failure(self, message: str) -> None:
        self._watchdog_failure_recorded = True
        db = SessionLocal()
        try:
            upsert_alert(db, fingerprint="monitoring-service:watchdog", source="monitoring", alert_type="monitoring_failure",
                         severity="critical", title="Repository monitoring unavailable", message=message[:1000])
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def ingest_kubernetes_snapshot(self, db: Session, snapshot: dict | None) -> None:
        if not isinstance(snapshot, dict):
            return
        self._latest_snapshot = snapshot
        now = snapshot.get("sampled_at")
        if snapshot.get("status") != "available":
            upsert_alert(db, fingerprint="kubernetes-monitoring-unavailable", source="monitoring",
                         alert_type="monitoring_failure", severity="critical", title="Kubernetes monitoring unavailable",
                         message=str(snapshot.get("message") or "Kubernetes sampling failed")[:1000], observed_at=now)
            db.commit()
            return

        active_fingerprints = set()
        for hpa in snapshot.get("hpas", []):
            deployment_id = hpa.get("deployment_id")
            if not isinstance(deployment_id, int):
                continue
            deployment = db.get(Deployment, deployment_id)
            if not deployment or (hpa.get("project_id") is not None and deployment.project_id != hpa.get("project_id")):
                continue
            latest = db.query(AutoscalingEvent).filter(AutoscalingEvent.deployment_id == deployment_id).order_by(AutoscalingEvent.occurred_at.desc(), AutoscalingEvent.id.desc()).first()
            current, desired = hpa.get("current_replicas"), hpa.get("desired_replicas")
            if latest is None or latest.current_replicas != current or latest.desired_replicas != desired:
                db.add(AutoscalingEvent(project_id=deployment.project_id, deployment_id=deployment_id,
                                        event_type="replica_sample" if latest is None else "replica_change",
                                        occurred_at=_utc(now), current_replicas=current, desired_replicas=desired,
                                        details={"hpa_name": hpa.get("name"), "current_metrics": hpa.get("current_metrics", []),
                                                 "targets": hpa.get("targets", []), "conditions": hpa.get("conditions", [])}))
        for item in snapshot.get("alerts", []):
            fingerprint = item.get("fingerprint")
            if not fingerprint:
                continue
            active_fingerprints.add(fingerprint)
            project_id = item.get("project_id")
            if project_id is not None and db.get(Project, project_id) is None:
                project_id = None
            upsert_alert(db, fingerprint=fingerprint, source=item.get("source", "kubernetes"),
                         alert_type=item.get("alert_type", "kubernetes_health"), severity=item.get("severity", "warning"),
                         title=item.get("title", "Kubernetes health alert"), message=item.get("message", "Kubernetes health issue"),
                         project_id=project_id, observed_at=now)
        for row in db.query(MonitoringAlert).filter(MonitoringAlert.source == "kubernetes", MonitoringAlert.is_resolved.is_(False)).all():
            if row.fingerprint not in active_fingerprints:
                row.is_resolved = True
                row.resolved_at = _utc(now)
        unavailable = db.query(MonitoringAlert).filter(MonitoringAlert.fingerprint == "kubernetes-monitoring-unavailable", MonitoringAlert.is_resolved.is_(False)).first()
        if unavailable:
            unavailable.is_resolved = True
            unavailable.resolved_at = _utc(now)
        db.commit()

    def _namespace_projects(self) -> dict[str, int]:
        db = SessionLocal()
        try:
            mapping: dict[str, int] = {}
            rows = db.query(Deployment).filter(Deployment.deployment_type.in_(["staging", "production_green"])).all()
            for row in rows:
                details = row.details if isinstance(row.details, dict) else {}
                namespace = details.get("namespace") or (settings.k8s_namespace if row.environment == "production" else None)
                if namespace:
                    mapping[namespace] = row.project_id
            return mapping
        finally:
            db.close()

    def _local_kubernetes_loop(self) -> None:
        from app.services.kubernetes_monitor import collect_cluster_snapshot

        interval = settings.monitoring_kubernetes_interval_seconds
        while not self._stop.is_set():
            snapshot = collect_cluster_snapshot(self._namespace_projects())
            db = SessionLocal()
            try:
                self.ingest_kubernetes_snapshot(db, snapshot)
            except Exception as exc:
                db.rollback()
                self._last_error = f"Kubernetes monitoring result could not be stored ({type(exc).__name__})"
            finally:
                db.close()
            self._stop.wait(interval)

    def service_status(self) -> dict:
        observer_running = bool(self._observer and self._observer.is_alive())
        return {"enabled": settings.monitoring_enabled, "watchdog_available": WATCHDOG_AVAILABLE,
                "observer_running": observer_running, "event_processor_running": bool(self._processor and self._processor.is_alive()),
                "monitored_project_count": len(self._registered), "last_event_at": self._last_event_at,
                "last_error": self._last_error, "kubernetes_mode": "local-poller" if settings.deployment_target == "local" else "remote-worker-heartbeat",
                "kubernetes_snapshot_at": self._latest_snapshot.get("sampled_at") if self._latest_snapshot else None}

    def latest_snapshot(self) -> dict | None:
        return self._latest_snapshot


monitoring_service = MonitoringService()
