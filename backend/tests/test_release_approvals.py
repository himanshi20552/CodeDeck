from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import analysis as analysis_api, approvals, deployments, projects
from app.core.config import settings
from app.db.database import Base
from app.db.models import Analysis, Deployment, GeneratedFile, Project, ReleaseApproval, ReleaseApprovalEvent
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


def test_approval_demo_is_development_only_and_allows_remote_target(monkeypatch):
    monkeypatch.setattr(settings, "release_approval_demo_mode", True)
    monkeypatch.setattr(settings, "codedeck_environment", "development")
    monkeypatch.setattr(settings, "deployment_target", "remote")
    assert approvals._manager_read_auth(None) == "demo"
    monkeypatch.setattr(settings, "codedeck_environment", "production")
    with pytest.raises(HTTPException) as exc:
        approvals._manager_read_auth(None)
    assert exc.value.status_code == 503
    monkeypatch.setattr(settings, "codedeck_environment", "test")
    with pytest.raises(HTTPException) as exc:
        approvals._manager_read_auth(None)
    assert exc.value.status_code == 503


def test_approval_demo_is_disabled_without_explicit_flag(monkeypatch):
    monkeypatch.setattr(settings, "release_approval_demo_mode", False)
    monkeypatch.setattr(settings, "release_approval_token", "")
    with pytest.raises(HTTPException) as exc:
        approvals._manager_read_auth(None)
    assert exc.value.status_code == 503


def test_demo_approval_is_audited_without_starting_production(monkeypatch):
    from fastapi import BackgroundTasks

    monkeypatch.setattr(settings, "release_approval_demo_mode", True)
    monkeypatch.setattr(settings, "codedeck_environment", "development")
    monkeypatch.setattr(settings, "deployment_target", "remote")
    db = Session()
    project, staging = release_fixture(db)
    staging.deployment_type = "remote_staging"
    staging.status = "SUCCEEDED"
    staging.details = {"namespace": "demo-stage", "smoke": {"status": "passed"}}
    db.commit()
    approval, _ = request_release_approval(db, project, staging, "developer")
    db.commit()
    assert approvals._approval_detail(db, approval)["demo_mode"] is True
    pending = approvals.list_pending_approvals(None, db)
    assert pending["mode"] == "demo" and pending["count"] == 1
    assert pending["items"][0]["staging_deployment"]["deployment_type"] == "remote_staging"
    assert approvals.get_approval(approval.id, None, db)["demo_mode"] is True
    queued = BackgroundTasks()
    result = approvals._decision(approval.id, "approved", approvals.DecisionRequest(comments="Reviewed in demo"),
                                queued, None, "untrusted browser label", db)
    db.refresh(approval)
    events = db.query(ReleaseApprovalEvent).filter_by(approval_id=approval.id).all()
    decision_event = next(event for event in events if event.event_type == "approval_approved")
    assert result["demo_mode"] is True
    assert approval.status == "Approved"
    assert approval.approver_identity == "Demo Manager (demo)"
    assert approval.approver_identity_verified is False
    assert approval.comments.startswith("[DEMO MODE]")
    assert decision_event.details["demo_mode"] is True
    assert decision_event.details["identity_verified"] is False
    assert not queued.tasks
    assert project.status == "staging_healthy"
    workflow = analysis_api.get_release_workflow(project.id, db)
    assert workflow["release"]["approval_status"] == "Approved"
    production_stage = next(stage for stage in workflow["stages"] if stage["id"] == "production")
    assert production_stage["status"] == "Skipped"
    assert "not started" in production_stage["output"]
    assert db.query(Deployment).filter(Deployment.project_id == project.id,
                                       Deployment.environment == "production").count() == 0
    assert db.query(Deployment).filter(Deployment.project_id == project.id,
                                       Deployment.deployment_type == "remote_staging").count() == 1
    assert db.query(Deployment).filter(Deployment.project_id == project.id,
                                       Deployment.deployment_type.in_(["remote_cleanup", "remote_autoscaling"])).count() == 0
    history = approvals.approval_history(None, 100, None, db)
    assert history["mode"] == "demo" and history["items"][0]["status"] == "Approved"
    db.close()


def test_remote_staging_can_be_submitted_for_approval_without_promotion(monkeypatch):
    monkeypatch.setattr(settings, "deployment_target", "remote")
    db = Session()
    project, staging = release_fixture(db)
    staging.deployment_type = "remote_staging"
    staging.status = "SUCCEEDED"
    staging.details = {"namespace": "demo-stage", "smoke": {"status": "passed"}}
    db.commit()

    response = deployments.deploy_production(project.id, "developer", db)
    assert response["status"] == "Awaiting Approval"
    assert response["approval_id"]
    assert "no production deployment was started" in response["message"].lower()
    approval = db.get(ReleaseApproval, response["approval_id"])
    assert approval.staging_deployment_id == staging.id
    assert approval.status == "Awaiting Approval"
    assert db.query(Deployment).filter(Deployment.project_id == project.id,
                                       Deployment.environment == "production").count() == 0

    # The explicit approval request API accepts the same verified remote staging record.
    result = approvals.submit_approval_request(project.id, approvals.ApprovalRequest(staging_deployment_id=staging.id), None, db)
    assert result["id"] == approval.id
    assert result["created"] is False
    db.close()


def test_remote_staging_still_queues_authenticated_worker_job(monkeypatch):
    from fastapi import BackgroundTasks

    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(settings, "remote_worker_token", "worker-secret-for-test")
    monkeypatch.setattr(deployments, "build_deployment_contract", lambda *args: SimpleNamespace(status="ready", summary="ready", checks=[]))
    db = Session()
    project, _ = release_fixture(db)
    db.add_all([
        GeneratedFile(project_id=project.id, filename="Dockerfile", content="FROM python:3.11-slim"),
        GeneratedFile(project_id=project.id, filename="deployment.yaml", content="apiVersion: apps/v1"),
    ])
    db.commit()
    tasks = BackgroundTasks()
    response = deployments.deploy_staging(project.id, tasks, deployments.DeploymentTargetRequest(target="remote"), None, db)
    row = db.get(Deployment, response["deployment_id"])
    assert response["status"] == "QUEUED"
    assert response["message"] == "Deployment queued for the remote K3s worker"
    assert row.deployment_type == "remote_staging" and row.status == "QUEUED"
    assert row.details["target"] == "aws-ec2-k3s"
    assert row.details["worker_payload"]["project_id"] == project.id
    assert not tasks.tasks
    db.close()


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"])
def test_remote_staging_must_be_successful_before_approval(status):
    db = Session()
    project, staging = release_fixture(db, status=status, smoke="passed")
    staging.deployment_type = "remote_staging"
    db.commit()
    with pytest.raises(HTTPException) as error:
        approvals.submit_approval_request(project.id, approvals.ApprovalRequest(staging_deployment_id=staging.id), None, db)
    assert error.value.status_code == 409
    assert db.query(ReleaseApproval).filter_by(staging_deployment_id=staging.id).count() == 0
    db.close()


def test_demo_rejection_requires_a_reason(monkeypatch):
    monkeypatch.setattr(settings, "release_approval_demo_mode", True)
    monkeypatch.setattr(settings, "codedeck_environment", "development")
    monkeypatch.setattr(settings, "deployment_target", "local")
    db = Session()
    project, staging = release_fixture(db)
    approval, _ = request_release_approval(db, project, staging, "developer")
    with pytest.raises(HTTPException) as exc:
        approvals._decision(approval.id, "rejected", approvals.DecisionRequest(comments="  "),
                            __import__("fastapi").BackgroundTasks(), None, None, db)
    assert exc.value.status_code == 422
    assert approval.status == "Awaiting Approval"
    db.close()


def test_remote_demo_rejection_is_persisted_without_production_work(monkeypatch):
    from fastapi import BackgroundTasks

    monkeypatch.setattr(settings, "release_approval_demo_mode", True)
    monkeypatch.setattr(settings, "codedeck_environment", "development")
    monkeypatch.setattr(settings, "deployment_target", "remote")
    db = Session()
    project, staging = release_fixture(db)
    staging.deployment_type = "remote_staging"
    staging.status = "SUCCEEDED"
    staging.details = {"namespace": "demo-stage", "smoke": {"status": "passed"}}
    db.commit()
    approval, _ = request_release_approval(db, project, staging, "developer")
    queued = BackgroundTasks()
    result = approvals._decision(approval.id, "rejected", approvals.DecisionRequest(comments="Hold this release"), queued, None, None, db)
    assert result["status"] == "Rejected" and result["demo_mode"] is True
    assert result["approver_identity"] == "Demo Manager (demo)"
    assert result["comments"].startswith("[DEMO MODE] Hold this release")
    assert not queued.tasks
    assert db.query(Deployment).filter(Deployment.project_id == project.id, Deployment.environment == "production").count() == 0
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
    with pytest.raises(HTTPException) as incorrect:
        approvals.get_approval(approval.id, "Bearer incorrect", db)
    assert incorrect.value.status_code == 401
    response = approvals._decision(approval.id, "approved", approvals.DecisionRequest(), SimpleNamespace(add_task=lambda *args: None), "Bearer secret-token", "manager", db)
    assert response["approver_identity_verified"] is False
    db.close()


def test_normal_remote_approval_cannot_promote_to_production(monkeypatch):
    db = Session()
    project, staging = release_fixture(db)
    staging.deployment_type = "remote_staging"
    staging.status = "SUCCEEDED"
    staging.details = {"namespace": "demo-stage", "smoke": {"status": "passed"}}
    db.commit()
    approval, _ = request_release_approval(db, project, staging, "developer")
    monkeypatch.setattr(settings, "release_approval_demo_mode", False)
    monkeypatch.setattr(settings, "release_approval_token", "configured-token")
    monkeypatch.setattr(settings, "deployment_target", "remote")
    with pytest.raises(HTTPException) as error:
        approvals._decision(approval.id, "approved", approvals.DecisionRequest(),
                            __import__("fastapi").BackgroundTasks(), "Bearer configured-token", "manager", db)
    assert error.value.status_code == 501
    assert approval.status == "Awaiting Approval"
    assert db.query(Deployment).filter(Deployment.project_id == project.id, Deployment.environment == "production").count() == 0
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
