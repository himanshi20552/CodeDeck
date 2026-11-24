from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.database import Base
from app.db.models import Analysis, Deployment, MonitoringAlert, MonitoringEvent, Project
from app.api import monitoring as monitoring_api
from app.services import kubernetes_monitor, monitoring


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(bind=engine)
TestingSession = sessionmaker(bind=engine, autoflush=False)


def make_project(db, status="ready"):
    row = Project(repository_url="https://github.com/acme/monitor-demo", repository_name="monitor-demo", status=status)
    db.add(row); db.commit(); db.refresh(row)
    return row


def test_path_validation_and_exclusions(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "monitoring_workspace_root", str(tmp_path))
    monkeypatch.setattr(settings, "monitoring_exclude_patterns", "*.generated")
    service = monitoring.MonitoringService()
    project_root = tmp_path / "project_10"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "main.py").write_text("source")
    service.register_project(10)
    assert service._identify_path(str(project_root / "src" / "main.py")) == (10, "src/main.py")
    for excluded in (".git/objects/a", "node_modules/pkg/index.js", "__pycache__/main.pyc", "pycache/a.pyc", "build/out", "dist/app.js", ".env", "secrets/token.txt", "id_rsa", "data.generated"):
        assert service._identify_path(str(project_root / excluded)) is None, excluded
    outside = tmp_path / "project_11" / "secret.py"
    assert service._identify_path(str(project_root / ".." / "project_11" / outside.name)) is None


def test_filesystem_events_are_queued_not_persisted_in_callback_and_are_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "monitoring_workspace_root", str(tmp_path))
    monkeypatch.setattr(settings, "monitoring_exclude_patterns", "")
    service = monitoring.MonitoringService()
    root = tmp_path / "project_3"
    root.mkdir()
    (root / "a.py").write_text("no contents are recorded")
    service.register_project(3)
    service.handle_fs_event("created", str(root / "a.py"))
    assert service._queue.qsize() == 1
    assert TestingSession().query(MonitoringEvent).filter_by(project_id=3).count() == 0
    service.handle_fs_event("deleted", str(root / ".env"))
    assert service._queue.qsize() == 1


def test_file_move_records_both_relative_paths_without_file_contents(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "monitoring_workspace_root", str(tmp_path))
    root = tmp_path / "project_4"
    root.mkdir()
    old = root / "old.py"
    new = root / "src" / "new.py"
    service = monitoring.MonitoringService(); service.register_project(4)
    service.handle_fs_event("moved", str(old), destination=str(new))
    event = service._queue.get_nowait()
    assert event["event_type"] == "moved"
    assert event["relative_path"] == "src/new.py"
    assert event["summary"] == "Moved from old.py to src/new.py"


def test_modify_debounce_persists_one_event_with_workflow_context(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "monitoring_workspace_root", str(tmp_path))
    monkeypatch.setattr(settings, "monitoring_exclude_patterns", "")
    monkeypatch.setattr(settings, "monitoring_event_debounce_seconds", 0.08)
    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(monitoring, "SessionLocal", TestingSession)
    db = TestingSession(); project = make_project(db, "analyzing")
    project_id = project.id
    db.add(Analysis(project_id=project_id, language="Python")); db.commit(); db.close()
    project_root = tmp_path / f"project_{project_id}"
    project_root.mkdir()
    target = project_root / "main.py"
    target.write_text("placeholder")
    service = monitoring.MonitoringService()
    service.register_project(project_id)
    thread = threading.Thread(target=service._process_queue, daemon=True)
    thread.start()
    service.handle_fs_event("modified", str(target))
    time.sleep(0.02)
    service.handle_fs_event("modified", str(target))
    time.sleep(0.15)
    service._stop.set(); thread.join(timeout=2)
    db = TestingSession()
    events = db.query(MonitoringEvent).filter_by(project_id=project_id).all()
    assert len(events) == 1
    assert events[0].relative_path == "main.py"
    assert events[0].workflow_context == "analysis"
    assert "2 updates coalesced" in events[0].summary
    assert not hasattr(events[0], "content")
    db.close()


def test_delete_alert_is_deduplicated_and_suppressed_during_managed_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "monitoring_workspace_root", str(tmp_path))
    monkeypatch.setattr(monitoring, "SessionLocal", TestingSession)
    db = TestingSession(); project = make_project(db)
    project_root = tmp_path / f"project_{project.id}"
    project_root.mkdir()
    service = monitoring.MonitoringService(); service.register_project(project.id)
    payload = {"project_id": project.id, "relative_path": "src/gone.py", "event_type": "deleted", "summary": "Repository file deleted", "occurred_at": datetime.now(timezone.utc).isoformat(), "coalesced": 1}
    service._persist_file_event(payload)
    service._persist_file_event(payload)
    alert = db.query(MonitoringAlert).filter_by(fingerprint=f"repository-delete:{project.id}:src/gone.py").one()
    assert alert.occurrence_count == 1
    project.status = "analyzing"; db.commit()
    payload["relative_path"] = "src/managed.py"
    service._persist_file_event(payload)
    assert db.query(MonitoringAlert).filter_by(fingerprint=f"repository-delete:{project.id}:src/managed.py").count() == 0
    db.close()


def test_observer_starts_and_stops_cleanly_with_one_shared_instance(monkeypatch, tmp_path):
    class FakeObserver:
        def __init__(self): self.schedules = []; self.running = False; self.stopped = False
        def schedule(self, handler, path, **kwargs):
            self.schedules.append((path, kwargs))
            return SimpleNamespace(path=path)
        def start(self): self.running = True
        def is_alive(self): return self.running
        def stop(self): self.stopped = True; self.running = False
        def join(self, timeout=None): pass
        def unschedule(self, watch): pass

    observer = FakeObserver()
    monkeypatch.setattr(monitoring.settings, "monitoring_enabled", True)
    monkeypatch.setattr(monitoring.settings, "monitoring_workspace_root", str(tmp_path))
    monkeypatch.setattr(monitoring.settings, "deployment_target", "remote")
    monkeypatch.setattr(monitoring, "WATCHDOG_AVAILABLE", True)
    monkeypatch.setattr(monitoring, "Observer", lambda: observer)
    monkeypatch.setattr(monitoring, "SessionLocal", TestingSession)
    service = monitoring.MonitoringService()
    service.register_project(999)
    watched_project = tmp_path / "project_999"
    (watched_project / "src").mkdir(parents=True)
    for excluded in (".git/objects", "node_modules/pkg", "__pycache__", "pycache", "build", "dist", ".docker"):
        (watched_project / excluded).mkdir(parents=True)
    service.start()
    assert observer.running and len(observer.schedules) == 3
    assert observer.schedules[0][1]["recursive"] is False
    watched_paths = [Path(path) for path, _kwargs in observer.schedules]
    assert not any(any(part in monitoring.BASE_EXCLUDED_DIRS for part in path.parts) for path in watched_paths)
    service.stop()
    assert observer.stopped
    assert not service.service_status()["event_processor_running"]


def test_alert_upsert_deduplicates_and_tracks_occurrences():
    db = TestingSession(); project = make_project(db)
    observed = datetime.now(timezone.utc)
    for timestamp in (observed, observed, observed + timedelta(seconds=1)):
        monitoring.upsert_alert(db, fingerprint="pod:ns:api:CrashLoopBackOff", source="kubernetes", alert_type="pod_failure",
                                severity="critical", title="CrashLoopBackOff", message="pod failed", project_id=project.id,
                                observed_at=timestamp)
    db.commit()
    assert db.query(MonitoringAlert).filter_by(project_id=project.id).count() == 1
    assert db.query(MonitoringAlert).filter_by(project_id=project.id).one().occurrence_count == 2
    db.close()


def test_kubernetes_snapshot_reports_rollout_pod_hpa_and_metrics_server_unavailable(monkeypatch):
    ns = "codedeck-p7-attempt"
    def obj(**kwargs): return SimpleNamespace(**kwargs)
    deployment = obj(metadata=obj(name="api-staging", namespace=ns, labels={}),
                     spec=obj(replicas=2, selector=obj(match_labels={"app": "api"})),
                     status=obj(ready_replicas=0, available_replicas=0, updated_replicas=1,
                                conditions=[obj(type="Progressing", reason="ProgressDeadlineExceeded", status="False")]))
    pod = obj(metadata=obj(name="api-staging-123", namespace=ns, labels={"app": "api"}),
               status=obj(phase="Running", container_statuses=[obj(restart_count=4, state=obj(waiting=obj(reason="CrashLoopBackOff")))],
                          conditions=[obj(type="Ready", status="False"), obj(type="PodScheduled", status="True")] ))
    unscheduled = obj(metadata=obj(name="api-staging-456", namespace=ns, labels={"app": "api"}),
                      status=obj(phase="Pending", container_statuses=[], conditions=[obj(type="PodScheduled", status="False", reason="Unschedulable")]))
    hpa = obj(metadata=obj(name="api", namespace=ns), spec=obj(scale_target_ref=obj(name="api-staging"), min_replicas=1, max_replicas=4),
               status=obj(current_replicas=2, desired_replicas=2, conditions=[]))

    class Core:
        def list_namespace(self, **kwargs): return obj(items=[obj(metadata=obj(name=ns))])
        def list_node(self, **kwargs): return obj(items=[])
        def list_pod_for_all_namespaces(self, **kwargs): return obj(items=[pod, unscheduled])
    class Apps:
        def list_deployment_for_all_namespaces(self, **kwargs): return obj(items=[deployment])
    class Autoscaling:
        def list_horizontal_pod_autoscaler_for_all_namespaces(self, **kwargs): return obj(items=[hpa])
    class Custom:
        def list_cluster_custom_object(self, *args, **kwargs): raise RuntimeError("metrics API missing")
    fake_client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV1Api=Autoscaling, CustomObjectsApi=Custom)
    monkeypatch.setattr(kubernetes_monitor, "_load_client", lambda: fake_client)
    monkeypatch.setattr(settings, "monitoring_restart_alert_threshold", 3)
    snapshot = kubernetes_monitor.collect_cluster_snapshot()
    assert snapshot["status"] == "available"
    assert snapshot["deployments"][0]["status"] == "unhealthy"
    assert snapshot["pods"][0]["reason"] == "CrashLoopBackOff"
    assert snapshot["pods"][0]["deployment_names"] == ["api-staging"]
    assert snapshot["pods"][1]["reason"] == "Unschedulable"
    assert snapshot["hpas"][0]["desired_replicas"] == 2
    assert snapshot["metrics"]["status"] == "unavailable"
    assert any(alert["alert_type"] == "unhealthy_rollout" for alert in snapshot["alerts"])
    assert any(alert["alert_type"] == "pod_failure" for alert in snapshot["alerts"])


def test_kubernetes_api_failure_is_reported_without_metrics(monkeypatch):
    monkeypatch.setattr(kubernetes_monitor, "_load_client", lambda: (_ for _ in ()).throw(TimeoutError()))
    snapshot = kubernetes_monitor.collect_cluster_snapshot()
    assert snapshot["status"] == "unavailable"
    assert "TimeoutError" in snapshot["message"]
    assert snapshot["metrics"]["status"] == "unavailable"
    assert snapshot["metrics"]["pods"] == []


def test_kubernetes_quantity_conversion():
    assert kubernetes_monitor._quantity("250m", "cpu") == 250
    assert kubernetes_monitor._quantity("100000000n", "cpu") == 100
    assert kubernetes_monitor._quantity("128Mi", "memory") == 128 * 1024 * 1024


def test_metrics_server_usage_is_returned_only_when_api_provides_it(monkeypatch):
    ns = "codedeck-p8-attempt"
    def obj(**kwargs): return SimpleNamespace(**kwargs)
    class Core:
        def list_namespace(self, **kwargs): return obj(items=[obj(metadata=obj(name=ns))])
        def list_node(self, **kwargs): return obj(items=[])
        def list_pod_for_all_namespaces(self, **kwargs): return obj(items=[])
    class Apps:
        def list_deployment_for_all_namespaces(self, **kwargs): return obj(items=[])
    class Autoscaling:
        def list_horizontal_pod_autoscaler_for_all_namespaces(self, **kwargs): return obj(items=[])
    class Custom:
        def list_cluster_custom_object(self, group, version, plural, **kwargs):
            return {"items": [{"metadata": {"namespace": ns, "name": "api-0"}, "containers": [{"usage": {"cpu": "120m", "memory": "64Mi"}}]}]} if plural == "pods" else {"items": [{"metadata": {"name": "node-a"}, "usage": {"cpu": "2", "memory": "4Gi"}}]}
    client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV1Api=Autoscaling, CustomObjectsApi=Custom)
    monkeypatch.setattr(kubernetes_monitor, "_load_client", lambda: client)
    snapshot = kubernetes_monitor.collect_cluster_snapshot()
    assert snapshot["metrics"]["status"] == "available"
    assert snapshot["metrics"]["pods"][0]["cpu_millicores"] == 120
    assert snapshot["metrics"]["pods"][0]["memory_bytes"] == 64 * 1024 * 1024
    assert snapshot["metrics"]["nodes"][0]["cpu_millicores"] == 2000


def test_monitoring_dashboard_filters_persisted_events_and_kubernetes_data(monkeypatch):
    db = TestingSession(); project = make_project(db)
    deployment = Deployment(project_id=project.id, environment="staging", version="blue", status="healthy", deployment_type="staging", details={"namespace": "demo-p1-a", "app_name": "demo"})
    db.add(deployment); db.commit(); db.refresh(deployment)
    event = MonitoringEvent(project_id=project.id, deployment_id=deployment.id, relative_path="src/main.py", event_type="modified", occurred_at=datetime.now(timezone.utc), workflow_context="build", processing_status="recorded")
    db.add(event); db.commit()
    monkeypatch.setattr(settings, "deployment_target", "local")
    monkeypatch.setattr(monitoring_api, "_snapshot", lambda: ({"status": "available", "deployments": [{"namespace": "demo-p1-a", "name": "demo-staging"}, {"namespace": "other-p2-a", "name": "other"}], "pods": [], "hpas": [], "metrics": {"status": "unavailable", "message": "Metrics Server unavailable", "pods": [], "nodes": []}, "alerts": []}, True, None))
    result = monitoring_api.monitoring_dashboard(project.id, deployment.id, None, 100, db)
    assert len(result["file_activity"]) == 1
    assert result["file_activity"][0]["workflow_context"] == "build"
    assert len(result["kubernetes"]["deployments"]) == 1
    assert result["kubernetes"]["deployments"][0]["namespace"] == "demo-p1-a"
    db.close()
