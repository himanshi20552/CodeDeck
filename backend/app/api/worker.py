"""Authenticated pull API used by the single remote K3s deployment worker."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import Deployment, Project, WorkerHeartbeat
from app.services.pipeline import upsert_step
from app.services.monitoring import monitoring_service

router = APIRouter(prefix="/api/worker", tags=["deployment-worker"])


def _authenticate(authorization: str | None = Header(default=None)) -> None:
    expected = settings.remote_worker_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Worker authentication failed")


class ProgressUpdate(BaseModel):
    status: str = Field(pattern="^(RUNNING|BUILDING|DEPLOYING|VERIFYING)$")
    message: str = Field(min_length=1, max_length=1000)


class Completion(BaseModel):
    status: str = Field(pattern="^(SUCCEEDED|FAILED)$")
    result: dict = Field(default_factory=dict)
    failure_reason: str | None = Field(default=None, max_length=4000)


class Heartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)
    details: dict = Field(default_factory=dict)


def _redact(message: str) -> str:
    message = re.sub(r"(gh[pousr]_[A-Za-z0-9_]{20,})", "[REDACTED]", message)
    return message[:1000]


def _fail_stale_worker_job(db: Session, heartbeat_row: WorkerHeartbeat | None) -> None:
    """Release a persisted active job after its worker heartbeat has gone stale."""
    if heartbeat_row is None:
        return
    active_job_id = (heartbeat_row.details or {}).get("active_job_id")
    if not active_job_id:
        return
    last_seen = heartbeat_row.last_seen
    if last_seen is None:
        stale = True
    else:
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        stale_after = max(settings.worker_heartbeat_interval * 3, 60)
        stale = (datetime.now(timezone.utc) - last_seen).total_seconds() > stale_after
    if not stale:
        return
    try:
        interrupted = db.get(Deployment, int(active_job_id))
    except (TypeError, ValueError):
        interrupted = None
    if interrupted and interrupted.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        reason = "Worker heartbeat expired while this deployment was active; retry the deployment."
        details = dict(interrupted.details or {})
        details.pop("worker_payload", None)
        details["failure_reason"] = reason
        details["completed_at"] = datetime.now(timezone.utc).isoformat()
        interrupted.details = details
        interrupted.status = "FAILED"
        if interrupted.deployment_type == "remote_staging":
            from app.services.release_approvals import finish_staging_tracking
            finish_staging_tracking(db, interrupted, success=False, failure_reason=reason)
        project = db.get(Project, interrupted.project_id)
        if project and interrupted.deployment_type == "remote_staging":
            project.status = "staging_failed"
        monitoring_service.process_deployment_outcome(db, interrupted)
        if interrupted.deployment_type != "remote_autoscaling":
            step_name = "Cleanup" if interrupted.deployment_type == "remote_cleanup" else "Staging"
            upsert_step(db, interrupted.project_id, step_name, "failed", error=reason)
    heartbeat_details = dict(heartbeat_row.details or {})
    heartbeat_details.pop("active_job_id", None)
    heartbeat_row.details = heartbeat_details


@router.post("/heartbeat", dependencies=[Depends(_authenticate)])
def heartbeat(payload: Heartbeat, db: Session = Depends(get_db)):
    row = db.get(WorkerHeartbeat, payload.worker_id)
    if row is None:
        row = WorkerHeartbeat(worker_id=payload.worker_id, details=payload.details)
        db.add(row)
    else:
        previous_job_id = (row.details or {}).get("active_job_id")
        if previous_job_id and not payload.details.get("active_job_id"):
            interrupted = db.get(Deployment, int(previous_job_id))
            if interrupted and interrupted.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                details = dict(interrupted.details or {})
                details.pop("worker_payload", None)
                details["failure_reason"] = "Worker restarted while this deployment was active; retry the deployment."
                details["completed_at"] = datetime.now(timezone.utc).isoformat()
                interrupted.details = details
                interrupted.status = "FAILED"
                if interrupted.deployment_type == "remote_staging":
                    from app.services.release_approvals import finish_staging_tracking
                    finish_staging_tracking(db, interrupted, success=False, failure_reason=details["failure_reason"])
                project = db.get(Project, interrupted.project_id)
                if project and interrupted.deployment_type == "remote_staging":
                    project.status = "staging_failed"
                monitoring_service.process_deployment_outcome(db, interrupted)
                if interrupted.deployment_type != "remote_autoscaling":
                    step_name = "Cleanup" if interrupted.deployment_type == "remote_cleanup" else "Staging"
                    upsert_step(db, interrupted.project_id, step_name, "failed", error=details["failure_reason"])
        row.last_seen = datetime.now(timezone.utc)
        row.details = payload.details
    monitoring_service.ingest_kubernetes_snapshot(db, (payload.details or {}).get("kubernetes_monitoring"))
    db.commit()
    return {"ok": True, "last_seen": row.last_seen}


@router.post("/jobs/claim", dependencies=[Depends(_authenticate)])
def claim_job(db: Session = Depends(get_db)):
    heartbeat_row = db.get(WorkerHeartbeat, settings.worker_id)
    _fail_stale_worker_job(db, heartbeat_row)
    active_count = db.scalar(
        select(func.count(Deployment.id)).where(
            Deployment.deployment_type.in_(["remote_staging", "remote_cleanup", "remote_autoscaling"]),
            Deployment.status.in_(["RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"]),
        )
    ) or 0
    if active_count >= settings.deployment_max_concurrent_jobs:
        db.commit()
        return {"job": None}
    row = db.scalar(
        select(Deployment)
        .where(Deployment.deployment_type.in_(["remote_staging", "remote_cleanup", "remote_autoscaling"]), Deployment.status == "QUEUED")
        .order_by(Deployment.created_at.asc(), Deployment.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        db.commit()
        return {"job": None}
    details = dict(row.details or {})
    payload = details.get("worker_payload")
    if not isinstance(payload, dict):
        row.status = "FAILED"
        details.update({"failure_reason": "Queued deployment payload is missing", "completed_at": datetime.now(timezone.utc).isoformat()})
        row.details = details
        db.commit()
        return {"job": None}
    if row.deployment_type == "remote_cleanup":
        source_id = payload.get("source_deployment_id")
        source = db.get(Deployment, source_id) if isinstance(source_id, int) else None
        namespace = payload.get("namespace")
        if (
            payload.get("operation") != "cleanup"
            or payload.get("project_id") != row.project_id
            or source is None
            or source.project_id != row.project_id
            or source.deployment_type != "remote_staging"
            or (source.details or {}).get("namespace") != namespace
            or not isinstance(namespace, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
        ):
            row.status = "FAILED"
            details.pop("worker_payload", None)
            details.update({"failure_reason": "Cleanup job failed source deployment validation", "completed_at": datetime.now(timezone.utc).isoformat()})
            row.details = details
            db.commit()
            return {"job": None}
    if row.deployment_type == "remote_autoscaling":
        source_id = payload.get("source_deployment_id")
        source = db.get(Deployment, source_id) if isinstance(source_id, int) else None
        source_details = (source.details or {}) if source else {}
        expected_app = source_details.get("app_name") if isinstance(source_details, dict) else None
        expected_name = f"{expected_app}-green" if source and source.deployment_type == "production_green" else f"{expected_app}-staging"
        valid_config = None
        try:
            from app.services.kubernetes import validate_autoscaling_config
            valid_config = validate_autoscaling_config(payload.get("config") or {})
        except (ValueError, TypeError):
            pass
        if (payload.get("operation") not in {"configure_autoscaling", "remove_autoscaling"}
            or payload.get("project_id") != row.project_id or source is None
            or source.project_id != row.project_id or source.deployment_type not in {"remote_staging", "production_green"}
            or source.status not in {"SUCCEEDED", "healthy", "staging_healthy", "production_healthy", "awaiting_approval"}
            or source_details.get("namespace") != payload.get("namespace")
            or not expected_app or payload.get("deployment_name") != expected_name
            or not isinstance(payload.get("namespace"), str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", payload.get("namespace", ""))
            or (payload.get("operation") == "configure_autoscaling" and valid_config != payload.get("config"))):
            row.status = "FAILED"
            details.pop("worker_payload", None)
            details.update({"failure_reason": "Autoscaling job failed source deployment or configuration validation", "completed_at": datetime.now(timezone.utc).isoformat()})
            row.details = details
            db.commit()
            return {"job": None}
    row.status = "RUNNING"
    details["started_at"] = datetime.now(timezone.utc).isoformat()
    details.setdefault("logs", []).append({"time": details["started_at"], "message": "Claimed by remote worker"})
    row.details = details
    heartbeat_details = dict(heartbeat_row.details or {}) if heartbeat_row else {}
    heartbeat_details["active_job_id"] = row.id
    if heartbeat_row:
        heartbeat_row.details = heartbeat_details
    else:
        # A claim can race the first heartbeat after service startup. Persist
        # the active job here so a worker crash is still recoverable.
        db.add(WorkerHeartbeat(worker_id=settings.worker_id, details=heartbeat_details))
    project = db.get(Project, row.project_id)
    if project and row.deployment_type != "remote_autoscaling":
        project.status = "deploying"
    db.commit()
    return {"job": {"id": row.id, **payload}}


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(_authenticate)])
def update_progress(job_id: int, update: ProgressUpdate, db: Session = Depends(get_db)):
    row = db.get(Deployment, job_id)
    if row is None or row.deployment_type not in {"remote_staging", "remote_cleanup", "remote_autoscaling"}:
        raise HTTPException(status_code=404, detail="Deployment job not found")
    if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return {"ok": True, "terminal": True}
    details = dict(row.details or {})
    details["logs"] = (details.get("logs") or [])[-499:]
    details["logs"].append({"time": datetime.now(timezone.utc).isoformat(), "message": _redact(update.message)})
    details["phase"] = update.status
    row.status = update.status
    row.details = details
    if row.deployment_type != "remote_autoscaling":
        upsert_step(db, row.project_id, "Staging", "running", result=_redact(update.message))
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(_authenticate)])
def complete_job(job_id: int, update: Completion, db: Session = Depends(get_db)):
    row = db.get(Deployment, job_id)
    if row is None or row.deployment_type not in {"remote_staging", "remote_cleanup", "remote_autoscaling"}:
        raise HTTPException(status_code=404, detail="Deployment job not found")
    if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return {"ok": True, "terminal": True}
    details = dict(row.details or {})
    details.pop("worker_payload", None)
    details["result"] = update.result
    details["image"] = update.result.get("image")
    details["namespace"] = update.result.get("namespace", details.get("namespace"))
    details["app_name"] = update.result.get("app_name")
    details["application_port"] = update.result.get("application_port")
    details["ingress_hostname"] = (update.result.get("smoke") or {}).get("url")
    details["smoke_test"] = update.result.get("smoke")
    details["smoke"] = update.result.get("smoke")
    details["failure_reason"] = _redact(update.failure_reason or "") if update.failure_reason else None
    details["completed_at"] = datetime.now(timezone.utc).isoformat()
    row.status = update.status
    row.details = details
    if row.deployment_type == "remote_staging":
        from app.services.release_approvals import finish_staging_tracking
        finish_staging_tracking(db, row, success=update.status == "SUCCEEDED",
                                failure_reason=details.get("failure_reason"))
    monitoring_service.process_deployment_outcome(db, row)
    heartbeat_row = db.get(WorkerHeartbeat, settings.worker_id)
    if heartbeat_row and (heartbeat_row.details or {}).get("active_job_id") == row.id:
        heartbeat_details = dict(heartbeat_row.details or {})
        heartbeat_details.pop("active_job_id", None)
        heartbeat_row.details = heartbeat_details
    project = db.get(Project, row.project_id)
    if row.deployment_type == "remote_autoscaling":
        source_id = (details.get("source_deployment_id") or (details.get("worker_payload") or {}).get("source_deployment_id"))
        source_id = source_id or (row.details or {}).get("source_deployment_id")
        if source_id:
            source = db.get(Deployment, int(source_id))
            if source and source.project_id == row.project_id:
                source_details = dict(source.details or {})
                if update.status == "SUCCEEDED":
                    source_details["autoscaling"] = update.result
                    if update.result.get("operation") in {"removed", "already-absent"}:
                        source_details.pop("autoscaling", None)
                source_details["autoscaling_operation"] = {"job_id": row.id, "status": update.status, "completed_at": details["completed_at"],
                                                            "message": update.result.get("message") or details["failure_reason"]}
                source.details = source_details
    if project:
        if row.deployment_type == "remote_autoscaling":
            pass
        elif row.deployment_type == "remote_cleanup":
            if update.status == "SUCCEEDED":
                source_id = update.result.get("source_deployment_id")
                source = db.get(Deployment, int(source_id)) if source_id else None
                if source and source.project_id == row.project_id:
                    source.status = "CLEANED_UP"
                    source_details = dict(source.details or {})
                    source_details["cleaned_up_at"] = details["completed_at"]
                    source.details = source_details
            other_success = db.query(Deployment).filter(
                Deployment.project_id == row.project_id,
                Deployment.deployment_type == "remote_staging",
                Deployment.status == "SUCCEEDED",
            ).count()
            project.status = "staging_healthy" if other_success else "ready"
        else:
            project.status = "staging_healthy" if update.status == "SUCCEEDED" else "staging_failed"
    if row.deployment_type != "remote_autoscaling":
        upsert_step(
            db,
            row.project_id,
            "Staging",
            "completed" if update.status == "SUCCEEDED" else "failed",
            result=(update.result.get("message") or "Remote deployment succeeded") if update.status == "SUCCEEDED" else None,
            error=details["failure_reason"] if update.status == "FAILED" else None,
        )
    db.commit()
    return {"ok": True}
