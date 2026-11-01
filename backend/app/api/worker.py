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
                project = db.get(Project, interrupted.project_id)
                if project:
                    project.status = "staging_failed"
                upsert_step(db, interrupted.project_id, "Staging", "failed", error=details["failure_reason"])
        row.last_seen = datetime.now(timezone.utc)
        row.details = payload.details
    db.commit()
    return {"ok": True, "last_seen": row.last_seen}


@router.post("/jobs/claim", dependencies=[Depends(_authenticate)])
def claim_job(db: Session = Depends(get_db)):
    active_count = db.scalar(
        select(func.count(Deployment.id)).where(
            Deployment.deployment_type.in_(["remote_staging", "remote_cleanup"]),
            Deployment.status.in_(["RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"]),
        )
    ) or 0
    if active_count >= settings.deployment_max_concurrent_jobs:
        db.commit()
        return {"job": None}
    row = db.scalar(
        select(Deployment)
        .where(Deployment.deployment_type.in_(["remote_staging", "remote_cleanup"]), Deployment.status == "QUEUED")
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
    row.status = "RUNNING"
    details["started_at"] = datetime.now(timezone.utc).isoformat()
    details.setdefault("logs", []).append({"time": details["started_at"], "message": "Claimed by remote worker"})
    row.details = details
    heartbeat_row = db.get(WorkerHeartbeat, settings.worker_id)
    if heartbeat_row:
        heartbeat_details = dict(heartbeat_row.details or {})
        heartbeat_details["active_job_id"] = row.id
        heartbeat_row.details = heartbeat_details
    project = db.get(Project, row.project_id)
    if project:
        project.status = "deploying"
    db.commit()
    return {"job": {"id": row.id, **payload}}


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(_authenticate)])
def update_progress(job_id: int, update: ProgressUpdate, db: Session = Depends(get_db)):
    row = db.get(Deployment, job_id)
    if row is None or row.deployment_type not in {"remote_staging", "remote_cleanup"}:
        raise HTTPException(status_code=404, detail="Deployment job not found")
    if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return {"ok": True, "terminal": True}
    details = dict(row.details or {})
    details["logs"] = (details.get("logs") or [])[-499:]
    details["logs"].append({"time": datetime.now(timezone.utc).isoformat(), "message": _redact(update.message)})
    details["phase"] = update.status
    row.status = update.status
    row.details = details
    upsert_step(db, row.project_id, "Staging", "running", result=_redact(update.message))
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(_authenticate)])
def complete_job(job_id: int, update: Completion, db: Session = Depends(get_db)):
    row = db.get(Deployment, job_id)
    if row is None or row.deployment_type not in {"remote_staging", "remote_cleanup"}:
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
    heartbeat_row = db.get(WorkerHeartbeat, settings.worker_id)
    if heartbeat_row and (heartbeat_row.details or {}).get("active_job_id") == row.id:
        heartbeat_details = dict(heartbeat_row.details or {})
        heartbeat_details.pop("active_job_id", None)
        heartbeat_row.details = heartbeat_details
    project = db.get(Project, row.project_id)
    if project:
        if row.deployment_type == "remote_cleanup":
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
