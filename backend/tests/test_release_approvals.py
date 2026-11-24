from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import approvals, deployments, projects
from app.core.config import settings
from app.db.database import Base
from app.db.models import Analysis, Deployment, Project, ReleaseApproval, ReleaseApprovalEvent
from app.services.release_approvals import decide_release, request_release_approval
from app.api import worker as worker_api


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def release_fixture(db, *, status="healthy", smoke="passed"):
    project = Project(repository_url="https://github.com/acme/demo", repository_name="demo", status="staging_healthy")
    db.add(project); db.flush()
    db.add(Analysis(project_id=project.id, analysis_result={"repository_snapshot": {
        "branch": "main", "commit_id": "abc123", "commit_message": "Ship release",
        "comparison_available": False, "added": [], "modified": [], "deleted": [],
    }, "test_result": {"status": "passed"}, "security_result": {"findings": []}}))
    staging = Deployment(project_id=project.id, environment="staging", version="blue", status=status,
                         deployment_type="staging", details={"namespace": "demo-stage", "smoke": {"status": smoke}})
    db.add(staging); db.commit()
    return project, staging


def test_request_requires_successful_staging_and_persisted_smoke():
    db = Session(); project, staging = release_fixture(db, smoke="failed")
    with pytest.raises(ValueError, match="smoke test"):
        request_release_approval(db, project, staging, "developer")
    db.rollback()
    project = db.get(Project, project.id); staging = db.get(Deployment, staging.id)
    staging.status = "failed"; staging.details = {"smoke": {"status": "passed"}}; db.commit()
    with pytest.raises(ValueError, match="completed successfully"):
        request_release_approval(db, project, staging, "developer")
    db.close()


def test_approval_decisions_are_single_and_audited_unverified():
    db = Session(); project, staging = release_fixture(db)
    approval, created = request_release_approval(db, project, staging, "release requester")
    assert created and approval.status == "Awaiting Approval"
    assert approval.commit_id == "abc123"
    assert len(approval.release_summary["deployment_risks"]) == 1
    approval, created = request_release_approval(db, project, staging, "release requester")
    assert not created
    decided = decide_release(db, approval.id, decision="approved", approver_identity="manager@example.test", comments="Reviewed")
    assert decided.status == "Production Deploying" and decided.decision == "approved"
    assert decided.approver_identity_verified is False
    db.flush()
    assert [event.to_status for event in db.query(ReleaseApprovalEvent).filter_by(approval_id=approval.id).all()] == ["Draft", "Staging", "Verification Pending", "Awaiting Approval", "Approved", "Production Deploying"]
    with pytest.raises(ValueError, match="already completed"):
        decide_release(db, approval.id, decision="rejected", approver_identity="another manager")
    db.close()


def test_rejected_release_cannot_queue_production(monkeypatch):
    db = Session(); project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, None)
    decide_release(db, approval.id, decision="rejected", approver_identity="manager", comments="Hold")
    called = []
    monkeypatch.setattr(deployments, "SessionLocal", lambda: db)
    monkeypatch.setattr(deployments, "run_production_green", lambda **kwargs: called.append(kwargs))
    deployments._bg_deploy_production(project.id, approval.id)
    assert called == []
    assert approval.status == "Rejected"
    db.close()


def test_remote_staging_worker_completion_records_verification(monkeypatch):
    db = Session(); project, staging = release_fixture(db, status="RUNNING")
    staging.deployment_type = "remote_staging"
    staging.details = {"namespace": "demo-stage", "worker_payload": {"project_id": project.id}}
    from app.services.release_approvals import start_release_tracking
    approval = start_release_tracking(db, project, requested_by="developer", staging_deployment_id=staging.id)
    monkeypatch.setattr("app.services.monitoring.monitoring_service.process_deployment_outcome", lambda *args: None)
    result = worker_api.complete_job(staging.id, worker_api.Completion(status="SUCCEEDED", result={
        "namespace": "demo-stage", "app_name": "demo", "smoke": {"status": "passed"},
    }), db)
    assert result["ok"] is True
    assert approval.status == "Verification Pending"
    assert "worker_payload" not in staging.details
    db.close()


def test_approved_release_runs_only_after_persisted_decision_and_completes(monkeypatch):
    db = Session(); project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, None)
    decide_release(db, approval.id, decision="approved", approver_identity="manager")
    db.query(Analysis).filter_by(project_id=project.id).update({"analysis_result": {"repository_snapshot": {"branch": "main"}}})
    db.commit()
    class Result:
        def model_dump(self): return {"namespace": "demo", "app_name": "demo", "smoke": {"status": "passed"}}
    monkeypatch.setattr(deployments, "SessionLocal", lambda: db)
    monkeypatch.setattr(deployments, "run_production_green", lambda **kwargs: Result())
    monkeypatch.setattr(deployments, "run_switch_traffic", lambda app: {"status": "healthy", "message": "switched", "smoke": {"status": "passed"}})
    deployments._bg_deploy_production(project.id, approval.id)
    assert approval.status == "Production Healthy"
    assert approval.production_deployment_id is not None
    assert any(e.event_type == "production_deployment_healthy" for e in db.query(ReleaseApprovalEvent).filter_by(approval_id=approval.id))
    db.close()


def test_partial_production_failure_is_audited(monkeypatch):
    db = Session(); project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, None)
    decide_release(db, approval.id, decision="approved", approver_identity="manager")
    class Result:
        def model_dump(self): return {"namespace": "demo", "app_name": "demo", "smoke": {"status": "passed"}}
    monkeypatch.setattr(deployments, "SessionLocal", lambda: db)
    monkeypatch.setattr(deployments, "run_production_green", lambda **kwargs: Result())
    monkeypatch.setattr(deployments, "run_switch_traffic", lambda app: (_ for _ in ()).throw(RuntimeError("traffic switch failed")))
    monkeypatch.setattr(deployments, "analyze_deployment_failure", lambda *args: {"summary": "switch failed"})
    deployments._bg_deploy_production(project.id, approval.id)
    assert approval.status == "Failed"
    assert db.query(ReleaseApprovalEvent).filter_by(approval_id=approval.id, event_type="production_deployment_failed").count() == 1
    db.close()


def test_backend_token_required_for_manager_api_and_identity_is_not_verified(monkeypatch):
    db = Session(); project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, None); db.commit()
    monkeypatch.setattr(settings, "release_approval_token", "secret-token")
    monkeypatch.setattr(settings, "deployment_target", "local")
    with pytest.raises(HTTPException) as missing:
        approvals.get_approval(approval.id, None, db)
    assert missing.value.status_code == 401
    response = approvals._decision(approval.id, "approved", approvals.DecisionRequest(), SimpleNamespace(add_task=lambda *args: None), "Bearer secret-token", "manager", db)
    assert response["approver_identity_verified"] is False
    db.close()


def test_project_deletion_retains_release_audit():
    db = Session(); project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, None); db.commit()
    with pytest.raises(HTTPException, match="audit history is retained"):
        projects.delete_project(project.id, "demo", db)
    assert db.get(ReleaseApproval, approval.id) is not None
    db.close()


def test_legacy_production_approval_route_cannot_switch_traffic():
    db = Session()
    with pytest.raises(HTTPException) as error:
        deployments.approve_production(1, db)
    assert error.value.status_code == 409
    db.close()
