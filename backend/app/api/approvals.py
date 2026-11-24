"""Backend-only release approval API for a separately authenticated manager client."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import Deployment, Project, ReleaseApproval, ReleaseApprovalEvent
from app.services.pipeline import upsert_step
from app.services.release_approvals import decide_release, request_release_approval

router = APIRouter(prefix="/api", tags=["release-approvals"])


class ApprovalRequest(BaseModel):
    staging_deployment_id: int = Field(gt=0)


class DecisionRequest(BaseModel):
    comments: str | None = Field(default=None, max_length=4000)


def _demo_mode_enabled() -> bool:
    return settings.release_approval_demo_mode and settings.codedeck_environment == "development" and settings.deployment_target == "local"


def _manager_read_auth(authorization: str | None) -> str:
    if settings.release_approval_demo_mode:
        if not _demo_mode_enabled():
            raise HTTPException(status_code=503, detail="Approval demo mode is blocked unless CODEDECK_ENV=development and DEPLOYMENT_TARGET=local")
        return "demo"
    expected = settings.release_approval_token
    supplied = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Release approval requires configured authentication or the explicitly enabled local development demo mode")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Release approval authentication failed")
    return "token"


def _manager_decision_auth(authorization: str | None, identity: str | None) -> tuple[str, bool]:
    mode = _manager_read_auth(authorization)
    if mode == "demo":
        return "Demo Manager (demo)", True
    actor = (identity or "").strip()
    if len(actor) < 2 or len(actor) > 255:
        raise HTTPException(status_code=422, detail="X-Approver-Identity must contain 2–255 characters")
    return actor, False


def _deployment_summary(row: Deployment | None) -> dict | None:
    if row is None:
        return None
    details = row.details if isinstance(row.details, dict) else {}
    result = details.get("result") if isinstance(details.get("result"), dict) else {}
    return {"id": row.id, "environment": row.environment, "version": row.version, "status": row.status,
            "deployment_type": row.deployment_type, "created_at": row.created_at.isoformat() if row.created_at else None,
            "image": details.get("image") or result.get("image"), "namespace": details.get("namespace"),
            "smoke_test": details.get("smoke") or details.get("smoke_test") or result.get("smoke"),
            "failure_reason": details.get("failure_reason") or details.get("error")}


def _approval_detail(db: Session, approval: ReleaseApproval) -> dict:
    project = db.get(Project, approval.project_id)
    staging = db.get(Deployment, approval.staging_deployment_id) if approval.staging_deployment_id else None
    production = db.get(Deployment, approval.production_deployment_id) if approval.production_deployment_id else None
    events = db.query(ReleaseApprovalEvent).filter(ReleaseApprovalEvent.approval_id == approval.id).order_by(ReleaseApprovalEvent.id.asc()).all()
    demo_mode = _demo_mode_enabled() or any(bool((event.details or {}).get("demo_mode")) for event in events)
    return {"id": approval.id, "project_id": approval.project_id,
            "project": {"id": project.id, "name": project.repository_name, "repository_url": project.repository_url} if project else None,
            "status": approval.status, "release_version": approval.release_version, "branch": approval.branch,
            "commit_id": approval.commit_id, "commit_message": approval.commit_message,
            "requested_by": approval.requested_by, "requester_identity_verified": approval.requester_identity_verified,
            "requested_at": approval.requested_at.isoformat() if approval.requested_at else None,
            "approver_identity": approval.approver_identity, "approver_identity_verified": approval.approver_identity_verified,
            "decision": approval.decision, "decision_timestamp": approval.decision_timestamp.isoformat() if approval.decision_timestamp else None,
            "comments": approval.comments, "summary": approval.release_summary or {}, "demo_mode": demo_mode,
            "staging_deployment": _deployment_summary(staging), "production_deployment": _deployment_summary(production),
            "events": [{"id": event.id, "event_type": event.event_type, "from_status": event.from_status,
                        "to_status": event.to_status, "actor_identity": event.actor_identity,
                        "actor_identity_verified": event.actor_identity_verified,
                        "occurred_at": event.occurred_at.isoformat(), "comments": event.comments,
                        "details": event.details or {}} for event in events]}


def _ensure_production_available() -> None:
    if settings.deployment_target == "remote":
        raise HTTPException(status_code=501, detail="Production deployment is not available through the current remote worker pipeline")


@router.post("/projects/{project_id}/approvals", status_code=201)
def submit_approval_request(project_id: int, body: ApprovalRequest,
                            requester_identity: str | None = Header(default=None, alias="X-Requester-Identity"),
                            db: Session = Depends(get_db)):
    _ensure_production_available()
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    staging = db.get(Deployment, body.staging_deployment_id)
    if staging is None or staging.project_id != project_id or staging.deployment_type not in {"staging", "remote_staging"}:
        raise HTTPException(status_code=404, detail="CodeDeck staging deployment not found")
    active = db.query(ReleaseApproval).filter(ReleaseApproval.project_id == project_id,
                                               ReleaseApproval.status.in_(["Awaiting Approval", "Approved", "Production Deploying"])).first()
    if active and active.staging_deployment_id != staging.id:
        raise HTTPException(status_code=409, detail=f"Release {active.id} is already {active.status.lower()}")
    try:
        approval, created = request_release_approval(db, project, staging, requester_identity)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(approval)
    response = _approval_detail(db, approval)
    response["created"] = created
    return response


@router.get("/approvals/pending")
def list_pending_approvals(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    mode = _manager_read_auth(authorization)
    rows = db.query(ReleaseApproval).filter(ReleaseApproval.status == "Awaiting Approval").order_by(ReleaseApproval.requested_at.asc(), ReleaseApproval.id.asc()).all()
    return {"items": [_approval_detail(db, row) for row in rows], "count": len(rows), "mode": mode}


@router.get("/approvals/history")
def approval_history(project_id: int | None = None, limit: int = Query(default=100, ge=1, le=500),
                     authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    mode = _manager_read_auth(authorization)
    query = db.query(ReleaseApproval)
    if project_id is not None:
        query = query.filter(ReleaseApproval.project_id == project_id)
    rows = query.order_by(ReleaseApproval.created_at.desc(), ReleaseApproval.id.desc()).limit(limit).all()
    return {"items": [_approval_detail(db, row) for row in rows], "count": len(rows), "mode": mode}


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: int, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    _manager_read_auth(authorization)
    approval = db.get(ReleaseApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return _approval_detail(db, approval)


def _decision(approval_id: int, decision: str, body: DecisionRequest, background_tasks: BackgroundTasks,
              authorization: str | None, approver_identity: str | None, db: Session) -> dict:
    actor, demo_mode = _manager_decision_auth(authorization, approver_identity)
    approval = db.query(ReleaseApproval).filter(ReleaseApproval.id == approval_id).with_for_update().first()
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if decision == "approved":
        if not demo_mode:
            _ensure_production_available()
    if decision == "rejected" and not (body.comments and body.comments.strip()):
        raise HTTPException(status_code=422, detail="A reason is required to reject a release")
    try:
        approval = decide_release(db, approval_id, decision=decision, approver_identity=actor, comments=body.comments, demo_mode=demo_mode)
        project = db.get(Project, approval.project_id)
        if demo_mode:
            # Demo approval is a persisted review decision only. No Kubernetes
            # or worker operation is enqueued from the unauthenticated demo.
            pass
        elif decision == "approved":
            if project:
                project.status = "production_deploying"
            upsert_step(db, approval.project_id, "Production", "running", result=f"Release approved by {actor}; production rollout queued")
        else:
            if project:
                project.status = "release_rejected"
            upsert_step(db, approval.project_id, "Production", "warning", result="Release rejected; no production deployment was started")
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision == "approved" and not demo_mode:
        from app.api.deployments import _bg_deploy_production
        background_tasks.add_task(_bg_deploy_production, approval.project_id, approval.id)
    db.refresh(approval)
    response = _approval_detail(db, approval)
    response["demo_mode"] = demo_mode
    return response


@router.post("/approvals/{approval_id}/approve")
def approve_release(approval_id: int, background_tasks: BackgroundTasks, body: DecisionRequest = DecisionRequest(),
                    authorization: str | None = Header(default=None),
                    approver_identity: str | None = Header(default=None, alias="X-Approver-Identity"),
                    db: Session = Depends(get_db)):
    return _decision(approval_id, "approved", body, background_tasks, authorization, approver_identity, db)


@router.post("/approvals/{approval_id}/reject")
def reject_release(approval_id: int, background_tasks: BackgroundTasks, body: DecisionRequest = DecisionRequest(),
                   authorization: str | None = Header(default=None),
                   approver_identity: str | None = Header(default=None, alias="X-Approver-Identity"),
                   db: Session = Depends(get_db)):
    return _decision(approval_id, "rejected", body, background_tasks, authorization, approver_identity, db)
