import logging
import shutil
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from app.db.database import SessionLocal
from app.db.models import Deployment, WorkerHeartbeat
from app.core.config import settings
from app.services.repository_ai import get_active_ai_provider
from app.services.observability import langfuse_status, metrics_status

router = APIRouter(prefix="/api/system", tags=["system"])
logger = logging.getLogger("deploymind")


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _cmd_ok(args: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        ok = proc.returncode == 0
        out = (proc.stdout or proc.stderr or "").strip()[:300]
        return ok, out
    except FileNotFoundError:
        return False, f"{args[0]} not found in PATH"
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} timed out"
    except Exception as exc:
        return False, str(exc)


def _db_ok() -> tuple[bool, str]:
    db = None
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        return True, "Connected"
    except Exception:
        return False, "Database is unavailable"
    finally:
        if db is not None:
            db.close()


def _kind_clusters() -> list[str]:
    try:
        proc = subprocess.run(
            ["kind", "get", "clusters"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0:
            return [c.strip() for c in proc.stdout.splitlines() if c.strip()]
    except Exception:
        pass
    return []


@router.get("/status")
def system_status():
    worker = worker_status()
    if settings.deployment_target == "remote":
        details = worker.get("details") or {}
        docker_ok = bool(worker.get("online") and details.get("docker"))
        kubectl_ok = bool(worker.get("online") and details.get("k3s"))
        kind_available, clusters, kind_ok = False, [], False
        local_kind = {"ok": False, "clusters": [], "message": "Local kind is disabled"}
        if settings.local_kind_enabled:
            local_docker, _ = _cmd_ok(["docker", "info"], timeout=10)
            local_client, _ = _cmd_ok(["kubectl", "version", "--client"], timeout=8)
            local_kind_available = _which("kind")
            local_clusters = _kind_clusters() if local_kind_available else []
            local_kind = {
                "ok": local_docker and local_client and bool(local_clusters),
                "available": local_kind_available,
                "clusters": local_clusters,
                "message": f"Clusters: {', '.join(local_clusters)}" if local_clusters else "Local kind cluster unavailable",
            }
        docker_msg = "Remote worker available" if docker_ok else "Remote worker Docker is unavailable"
        kubectl_msg = "Remote K3s node is ready" if kubectl_ok else "Remote K3s is unavailable"
    else:
        docker_ok, docker_msg = _cmd_ok(["docker", "info"], timeout=10)
        kubectl_ok, kubectl_msg = _cmd_ok(["kubectl", "version", "--client"], timeout=8)
        kind_available = _which("kind")
        clusters = _kind_clusters() if kind_available else []
        kind_ok = len(clusters) > 0
        local_kind = {
            "ok": docker_ok and kubectl_ok and kind_ok,
            "available": kind_available,
            "clusters": clusters,
            "message": f"Clusters: {', '.join(clusters)}" if kind_ok else "Local kind cluster unavailable",
        }
    db_ok, db_msg = _db_ok()

    return {
        "backend": {"ok": True, "message": "Running"},
        "deployment_target": settings.deployment_target,
        "deployment_targets": ["remote", *(["local"] if (settings.local_kind_enabled or settings.deployment_target == "local") else [])],
        "worker": worker,
        "local_kind": local_kind,
        "database": {"ok": db_ok, "message": db_msg if not db_ok else "Connected"},
        "docker": {
            "ok": docker_ok,
            "message": "Available" if docker_ok else docker_msg,
        },
        "kind": {
            "ok": kind_ok,
            "available": kind_available,
            "clusters": clusters,
            "message": f"Clusters: {', '.join(clusters)}" if kind_ok else (
                "kind installed but no clusters found" if kind_available else "kind not found in PATH"
            ),
        },
        "kubectl": {
            "ok": kubectl_ok,
            "message": "Available" if kubectl_ok else kubectl_msg,
        },
        "ai_provider": get_active_ai_provider(),
        "integrations": {
            "langfuse": langfuse_status(),
            "prometheus": metrics_status(),
        },
    }


def worker_status() -> dict:
    db = SessionLocal()
    try:
        queue_length = db.query(Deployment).filter(
            Deployment.deployment_type.in_(["remote_staging", "remote_cleanup"]),
            Deployment.status == "QUEUED",
        ).count()
        row = db.get(WorkerHeartbeat, settings.worker_id)
        if row is None:
            return {"online": False, "worker_id": settings.worker_id, "last_seen": None, "details": {}, "queue_length": queue_length, "message": "No worker heartbeat received"}
        last_seen = row.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        online = (datetime.now(timezone.utc) - last_seen).total_seconds() <= settings.worker_heartbeat_interval * 3
        return {
            "online": online,
            "worker_id": row.worker_id,
            "last_seen": last_seen.isoformat(),
            "details": row.details or {},
            "queue_length": queue_length,
            "message": "Worker heartbeat current" if online else "Worker heartbeat is stale",
        }
    except Exception:
        db.rollback()
        return {"online": False, "worker_id": settings.worker_id, "last_seen": None, "details": {}, "queue_length": None, "message": "Worker state is unavailable"}
    finally:
        db.close()


@router.get("/worker")
def get_worker_status():
    return worker_status()
