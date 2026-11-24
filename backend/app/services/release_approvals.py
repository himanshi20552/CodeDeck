"""Persistent release approval state and immutable transition audit helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Analysis, Deployment, PipelineStep, Project, ReleaseApproval, ReleaseApprovalEvent


RELEASE_STATES = {
    "Draft", "Staging", "Verification Pending", "Awaiting Approval", "Approved", "Rejected",
    "Production Deploying", "Production Healthy", "Failed", "Rolled Back",
}
ALLOWED_TRANSITIONS = {
    "Draft": {"Staging", "Failed"},
    "Staging": {"Verification Pending", "Failed"},
    "Verification Pending": {"Awaiting Approval", "Failed"},
    "Awaiting Approval": {"Approved", "Rejected", "Failed"},
    "Approved": {"Production Deploying", "Failed"},
    "Production Deploying": {"Production Healthy", "Failed"},
    "Production Healthy": {"Rolled Back", "Failed"},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def record_event(db: Session, approval: ReleaseApproval, *, event_type: str, from_status: str | None,
                 to_status: str, actor_identity: str, actor_identity_verified: bool = False,
                 comments: str | None = None, details: dict | None = None,
                 occurred_at: datetime | None = None) -> ReleaseApprovalEvent:
    if to_status not in RELEASE_STATES:
        raise ValueError(f"Unsupported release status: {to_status}")
    if from_status is not None and to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        raise ValueError(f"Release transition {from_status} -> {to_status} is not allowed")
    timestamp = occurred_at or now_utc()
    event = ReleaseApprovalEvent(approval_id=approval.id, event_type=event_type, from_status=from_status,
                                 to_status=to_status, actor_identity=actor_identity[:255],
                                 actor_identity_verified=actor_identity_verified, occurred_at=timestamp,
                                 comments=comments[:4000] if comments else None, details=details or None)
    db.add(event)
    approval.status = to_status
    approval.updated_at = timestamp
    return event


def transition_release(db: Session, approval: ReleaseApproval, to_status: str, *, event_type: str,
                       actor_identity: str = "CodeDeck system", actor_identity_verified: bool = False,
                       comments: str | None = None, details: dict | None = None,
                       occurred_at: datetime | None = None) -> ReleaseApprovalEvent:
    return record_event(db, approval, event_type=event_type, from_status=approval.status, to_status=to_status,
                        actor_identity=actor_identity, actor_identity_verified=actor_identity_verified,
                        comments=comments, details=details, occurred_at=occurred_at)


def _analysis_payload(db: Session, project_id: int) -> dict:
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    return dict(analysis.analysis_result or {}) if analysis and isinstance(analysis.analysis_result, dict) else {}


def start_release_tracking(db: Session, project: Project, *, requested_by: str | None,
                           staging_deployment_id: int | None = None) -> ReleaseApproval:
    payload = _analysis_payload(db, project.id)
    snapshot = payload.get("repository_snapshot") if isinstance(payload.get("repository_snapshot"), dict) else {}
    row = ReleaseApproval(project_id=project.id, staging_deployment_id=staging_deployment_id,
                          status="Draft", requested_by=(requested_by or "unidentified API caller")[:255],
                          requester_identity_verified=False, branch=snapshot.get("branch"),
                          commit_id=snapshot.get("commit_id"), commit_message=(snapshot.get("commit_message") or "")[:1000] or None,
                          release_version=snapshot.get("commit_id") or None,
                          release_summary={"repository_snapshot": snapshot})
    db.add(row)
    db.flush()
    record_event(db, row, event_type="release_drafted", from_status=None, to_status="Draft",
                 actor_identity=row.requested_by or "unidentified API caller", actor_identity_verified=False,
                 details={"staging_deployment_id": staging_deployment_id})
    transition_release(db, row, "Staging", event_type="staging_started", actor_identity="CodeDeck system",
                       details={"staging_deployment_id": staging_deployment_id})
    return row


def finish_staging_tracking(db: Session, deployment: Deployment, *, success: bool,
                            failure_reason: str | None = None) -> ReleaseApproval | None:
    approval = db.query(ReleaseApproval).filter(ReleaseApproval.staging_deployment_id == deployment.id).with_for_update().first()
    if approval is None:
        return None
    if approval.status != "Staging":
        return approval
    passed, reason, _ = staging_verification(db, deployment)
    if not success or not passed:
        transition_release(db, approval, "Failed", event_type="staging_failed",
                           comments=(failure_reason or reason)[:4000])
        return approval
    project = db.get(Project, deployment.project_id)
    if not project:
        return None
    approval.release_summary = build_release_summary(db, project, deployment)
    snapshot = (approval.release_summary or {}).get("release", {})
    approval.release_version = snapshot.get("version") or approval.release_version
    approval.branch = snapshot.get("branch") or approval.branch
    approval.commit_id = snapshot.get("commit_id") or approval.commit_id
    approval.commit_message = (snapshot.get("commit_message") or approval.commit_message or "")[:1000] or None
    transition_release(db, approval, "Verification Pending", event_type="staging_verified",
                       details={"staging_deployment_id": deployment.id, "smoke_test": approval.release_summary.get("staging", {}).get("smoke_test")})
    return approval


def request_release_approval(db: Session, project: Project, staging: Deployment, requester: str | None) -> tuple[ReleaseApproval, bool]:
    existing = db.query(ReleaseApproval).filter(ReleaseApproval.staging_deployment_id == staging.id).with_for_update().first()
    if existing:
        if existing.status == "Awaiting Approval":
            return existing, False
        if existing.status != "Verification Pending":
            raise ValueError(f"This staging release is already in state {existing.status}")
        approval = existing
    else:
        active = db.query(ReleaseApproval).filter(
            ReleaseApproval.project_id == project.id,
            ReleaseApproval.status.in_(["Awaiting Approval", "Approved", "Production Deploying"]),
        ).first()
        if active:
            raise ValueError(f"Release {active.id} is already {active.status.lower()}")
        if project.status in {"analyzing", "deploying", "staging_cancel_requested", "deploying_green", "production_deploying"}:
            raise ValueError("An active analysis or deployment must finish before requesting release approval")
        active_deployment = db.query(Deployment).filter(
            Deployment.project_id == project.id,
            Deployment.status.in_(["QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"]),
        ).first()
        if active_deployment:
            raise ValueError("An active deployment must finish before requesting release approval")
        approval = start_release_tracking(db, project, requested_by=requester, staging_deployment_id=staging.id)
        transition_release(db, approval, "Verification Pending", event_type="staging_verification_recorded",
                           details={"staging_deployment_id": staging.id, "recorded_from_persisted_deployment": True})

    verified, reason, smoke = staging_verification(db, staging)
    if not verified:
        raise ValueError(reason)
    approval.release_summary = build_release_summary(db, project, staging)
    release = approval.release_summary.get("release", {})
    approval.release_version = release.get("version") or approval.release_version
    approval.branch = release.get("branch") or approval.branch
    approval.commit_id = release.get("commit_id") or approval.commit_id
    approval.commit_message = (release.get("commit_message") or approval.commit_message or "")[:1000] or None
    approval.requested_by = (requester or approval.requested_by or "unidentified API caller")[:255]
    approval.requester_identity_verified = False
    approval.requested_at = now_utc()
    transition_release(db, approval, "Awaiting Approval", event_type="approval_requested",
                       actor_identity=approval.requested_by, comments=None,
                       details={"staging_deployment_id": staging.id, "smoke_test": smoke})
    return approval, True


def decide_release(db: Session, approval_id: int, *, decision: str, approver_identity: str,
                   comments: str | None = None, demo_mode: bool = False) -> ReleaseApproval:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected")
    approval = db.query(ReleaseApproval).filter(ReleaseApproval.id == approval_id).with_for_update().first()
    if approval is None:
        raise LookupError("Approval request not found")
    if approval.status != "Awaiting Approval" or approval.decision is not None:
        raise ValueError(f"Decision is already completed or release is in state {approval.status}")
    if decision == "rejected" and not (comments and comments.strip()):
        raise ValueError("A rejection reason is required")
    staging = db.get(Deployment, approval.staging_deployment_id) if approval.staging_deployment_id else None
    if not staging or staging.project_id != approval.project_id:
        raise ValueError("The verified staging deployment is no longer available")
    verified, reason, smoke = staging_verification(db, staging)
    if not verified:
        raise ValueError(reason)
    timestamp = now_utc()
    approval.approver_identity = approver_identity[:255]
    approval.approver_identity_verified = False
    approval.decision = decision
    approval.decision_timestamp = timestamp
    recorded_comments = comments.strip() if comments and comments.strip() else None
    if demo_mode:
        recorded_comments = f"[DEMO MODE] {recorded_comments}" if recorded_comments else "[DEMO MODE] Demonstration approval action."
    approval.comments = recorded_comments[:4000] if recorded_comments else None
    to_status = "Approved" if decision == "approved" else "Rejected"
    transition_release(db, approval, to_status, event_type=f"approval_{decision}", actor_identity=approver_identity,
                       comments=recorded_comments, details={"identity_verified": False, "demo_mode": demo_mode, "smoke_test": smoke}, occurred_at=timestamp)
    # A development demonstration records the manager decision, but never queues a
    # production rollout. Demo identity is deliberately unverified.
    if decision == "approved" and not demo_mode:
        transition_release(db, approval, "Production Deploying", event_type="production_deployment_queued",
                           actor_identity=approver_identity, comments="Production rollout queued after approval.",
                           details={"staging_deployment_id": staging.id})
    return approval


def staging_verification(db: Session, deployment: Deployment) -> tuple[bool, str, dict]:
    details = deployment.details if isinstance(deployment.details, dict) else {}
    result = details.get("result") if isinstance(details.get("result"), dict) else {}
    smoke = details.get("smoke") or details.get("smoke_test") or result.get("smoke")
    smoke_status = smoke.get("status") if isinstance(smoke, dict) else None
    accepted_status = (deployment.status or "").lower() in {"succeeded", "healthy", "staging_healthy"}
    if not accepted_status:
        return False, "Staging deployment has not completed successfully", smoke if isinstance(smoke, dict) else {}
    if smoke_status not in {"passed", "success", "healthy"}:
        return False, "A persisted successful staging smoke test is required", smoke if isinstance(smoke, dict) else {}
    failed_step = db.query(PipelineStep).filter(PipelineStep.project_id == deployment.project_id,
                                                 PipelineStep.name == "Tests", PipelineStep.status == "failed").first()
    analysis = _analysis_payload(db, deployment.project_id)
    test_status = (analysis.get("test_result") or {}).get("status") if isinstance(analysis.get("test_result"), dict) else None
    if failed_step or test_status in {"failed", "error"}:
        return False, "Repository tests have a persisted failure", smoke
    validation = analysis.get("validation_result") or {}
    if isinstance(validation, dict) and validation.get("valid") is False:
        return False, "Deployment validation has a persisted failure", smoke
    return True, "Staging and smoke verification passed", smoke


def build_release_summary(db: Session, project: Project, staging: Deployment) -> dict:
    payload = _analysis_payload(db, project.id)
    analysis = db.query(Analysis).filter(Analysis.project_id == project.id).first()
    snapshot = payload.get("repository_snapshot") if isinstance(payload.get("repository_snapshot"), dict) else {}
    details = staging.details if isinstance(staging.details, dict) else {}
    passed, _, smoke = staging_verification(db, staging)
    test_result = payload.get("test_result") if isinstance(payload.get("test_result"), dict) else None
    security = payload.get("security_result") if isinstance(payload.get("security_result"), dict) else None
    findings = security.get("findings", []) if security else []
    high_findings = [item for item in findings if isinstance(item, dict) and str(item.get("severity", "")).upper() == "HIGH"]
    dependencies = payload.get("staging_dependencies") or payload.get("dependencies") or []
    risk_indicators = []
    if test_result and test_result.get("status") == "skipped":
        risk_indicators.append("Repository tests were skipped")
    if high_findings:
        risk_indicators.append(f"{len(high_findings)} high-severity security finding(s)")
    if security is None:
        risk_indicators.append("Security scan result is unavailable")
    if not snapshot.get("comparison_available"):
        risk_indicators.append("Historical Git comparison is unavailable; only the captured repository snapshot is available")
    return {
        "project": {"id": project.id, "name": project.repository_name, "repository_url": project.repository_url},
        "release": {"version": staging.version or snapshot.get("commit_id"), "branch": snapshot.get("branch"),
                    "commit_id": snapshot.get("commit_id"), "commit_message": snapshot.get("commit_message")},
        "what_changed": {"summary": snapshot.get("change_summary") or "No repository change summary was recorded.",
                         "comparison_available": bool(snapshot.get("comparison_available")),
                         "added": snapshot.get("added", []), "modified": snapshot.get("modified", []),
                         "deleted": snapshot.get("deleted", [])},
        "tests": test_result,
        "security": {"score": analysis.security_score if analysis else None, "result": security,
                     "high_severity_findings": high_findings},
        "dependencies": dependencies,
        "staging": {"deployment_id": staging.id, "environment": staging.environment,
                    "status": staging.status, "smoke_test": smoke, "verified": passed,
                    "namespace": details.get("namespace")},
        "deployment_risks": risk_indicators,
        "rollback": {"available": True, "target": "blue",
                     "description": "The existing blue-green rollback path can be used after production promotion."},
    }
