import subprocess

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.database import Base
from app.db.models import Analysis, Deployment, GeneratedFile, PipelineStep, Project
from app.api import deployments, projects
from app.services import kubernetes
from app.worker import runner


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autoflush=False)


def make_project(db, status="ready"):
    row = Project(repository_url="https://github.com/acme/demo", repository_name="demo", status=status)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_workspace_clear_preserves_database_artifacts_and_rejects_active(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    db = Session()
    p = make_project(db)
    db.add(Analysis(project_id=p.id, language="Python"))
    db.add(GeneratedFile(project_id=p.id, filename="Dockerfile", content="FROM scratch"))
    db.commit()
    source = tmp_path / f"project_{p.id}"
    source.mkdir()
    (source / "repo.py").write_text("source")
    assert projects.clear_workspace(p.id, db)["removed"] == [source.name]
    assert db.query(Analysis).filter_by(project_id=p.id).count() == 1
    assert db.query(GeneratedFile).filter_by(project_id=p.id).count() == 1
    p.status = "analyzing"
    db.commit()
    source.mkdir()
    with pytest.raises(HTTPException) as error:
        projects.clear_workspace(p.id, db)
    assert error.value.status_code == 409 and source.exists()
    db.close()


def test_project_delete_requires_cleanup_confirmation_and_cleared_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    db = Session()
    p = make_project(db)
    workspace = tmp_path / f"project_{p.id}"
    workspace.mkdir()
    with pytest.raises(HTTPException, match="Clear repository"):
        projects.delete_project(p.id, "demo", db)
    workspace.rmdir()
    assert projects.delete_project(p.id, "demo", db)["status"] == "deleted"
    assert db.get(Project, p.id) is None
    db.close()


def test_delete_rejects_queued_and_running_jobs():
    db = Session()
    p = make_project(db)
    row = Deployment(project_id=p.id, environment="staging", version="blue", status="QUEUED", deployment_type="remote_staging", details={})
    db.add(row); db.commit()
    with pytest.raises(HTTPException, match="Active"):
        projects.delete_project(p.id, "demo", db)
    row.status = "RUNNING"; db.commit()
    with pytest.raises(HTTPException, match="Wait for analysis"):
        projects.clear_workspace(p.id, db)
    db.close()


def test_queued_remote_deployment_can_be_cancelled_before_deletion():
    db = Session(); p = make_project(db, status="deploying")
    row = Deployment(project_id=p.id, environment="staging", version="blue", status="QUEUED", deployment_type="remote_staging", details={"worker_payload": {"repository_url": "secret"}})
    db.add(row); db.commit()
    result = deployments.cancel_staging(p.id, db)
    assert result["status"] == "CANCELLED"
    assert row.status == "CANCELLED" and "worker_payload" not in row.details
    db.close()


def test_cleanup_preserves_pvcs_and_is_idempotent(monkeypatch):
    seen = []
    monkeypatch.setattr(kubernetes, "_run", lambda args, timeout: (seen.append(args) or subprocess.CompletedProcess(args, 0, "deleted", "")))
    result = kubernetes.cleanup_namespace_resources("demo-p2-abc")
    assert result["persistent_volume_claims_preserved"] is True
    assert "persistentvolumeclaims" not in seen[0][2]
    assert kubernetes.cleanup_namespace_resources("demo-p2-abc")["namespace"] == "demo-p2-abc"
    assert len(seen) == 2


def test_local_kind_cleanup_records_success_and_failure(monkeypatch):
    db = Session(); p = make_project(db)
    row = Deployment(project_id=p.id, environment="staging", version="blue", status="healthy", deployment_type="staging", details={"target": "local-kind", "namespace": "demo-p1-a1"})
    db.add(row); db.commit()
    monkeypatch.setattr(deployments.k8s_svc, "cleanup_namespace_resources", lambda ns: {"namespace": ns, "removed_kinds": ["deployments"], "persistent_volume_claims_preserved": True, "message": "removed"})
    response = deployments.cleanup_remote_deployment(p.id, row.id, db)
    assert response["status"] == "CLEANED_UP"
    assert row.status == "CLEANED_UP"
    assert deployments.cleanup_remote_deployment(p.id, row.id, db)["status"] == "CLEANED_UP"
    db.close()


def test_remote_cleanup_job_is_bounded_and_queued(monkeypatch):
    db = Session(); p = make_project(db)
    row = Deployment(project_id=p.id, environment="staging", version="blue", status="SUCCEEDED", deployment_type="remote_staging", details={"namespace": "demo-p3-a1", "target": "aws-ec2-k3s"})
    db.add(row); db.commit(); monkeypatch.setattr(settings, "remote_worker_token", "secret")
    result = deployments.cleanup_remote_deployment(p.id, row.id, db)
    job = db.get(Deployment, result["deployment_id"])
    assert job.status == "QUEUED" and job.deployment_type == "remote_cleanup"
    assert job.details["worker_payload"] == {"operation": "cleanup", "project_id": p.id, "namespace": "demo-p3-a1", "source_deployment_id": row.id}
    db.close()


@pytest.mark.parametrize("failure", [False, True])
def test_remote_worker_cleanup_reports_completion_and_partial_failure(monkeypatch, failure):
    import threading
    calls = []

    class Client:
        _state_lock = threading.Lock()
        active_job_id = None
        def post(self, path, payload):
            calls.append((path, payload))
            return {"ok": True}

    def clean(namespace):
        if failure:
            raise RuntimeError("some resources remained")
        return {"namespace": namespace, "removed_kinds": ["services"], "persistent_volume_claims_preserved": True, "message": "clean"}

    monkeypatch.setattr(kubernetes, "cleanup_namespace_resources", clean)
    runner._execute(Client(), {"id": 77, "operation": "cleanup", "project_id": 9, "namespace": "demo-p9-a1", "source_deployment_id": 2})
    completion = [payload for path, payload in calls if path.endswith("/complete")][-1]
    assert completion["status"] == ("FAILED" if failure else "SUCCEEDED")
    if failure:
        assert "some resources remained" in completion["failure_reason"]
    else:
        assert completion["result"]["source_deployment_id"] == 2
