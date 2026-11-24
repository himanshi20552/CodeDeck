import subprocess
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.analysis import get_release_workflow
from app.db.database import Base
from app.db.models import Analysis, Deployment, PipelineStep, Project, ReleaseApproval
from app.services.github import repository_snapshot_metadata


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autoflush=False)


def project(db):
    row = Project(repository_url="https://github.com/acme/release-demo", repository_name="release-demo", status="ready")
    db.add(row); db.commit(); db.refresh(row)
    return row


def test_workflow_uses_persisted_times_and_reports_missing_values():
    db = Session(); row = project(db)
    begin = datetime.now(timezone.utc) - timedelta(minutes=3)
    end = begin + timedelta(seconds=90)
    db.add(PipelineStep(project_id=row.id, name="Repository Analysis", status="completed", result="Parsed source", started_at=begin, completed_at=end))
    db.add(Analysis(project_id=row.id, analysis_result={"repository_snapshot": {"branch": "main", "commit_id": "abc123", "commit_message": "release", "files": ["src/main.py"], "file_count": 1, "comparison_available": False, "added": [], "modified": [], "deleted": [], "change_summary": "Snapshot only"}}))
    db.commit()
    payload = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in payload["stages"]}
    assert by_id["source_analysis"]["status"] == "Completed"
    assert by_id["source_analysis"]["duration_seconds"] == 90
    assert by_id["source_analysis"]["started_at"] is not None
    assert by_id["architecture"]["status"] == "Not Started"
    assert by_id["architecture"]["duration_seconds"] is None
    assert by_id["monitoring"]["status"] == "Skipped"
    assert payload["repository_snapshot"]["files"] == ["src/main.py"]
    db.close()


def test_failed_stage_blocks_downstream_and_approval_wait_is_real():
    db = Session(); row = project(db)
    now = datetime.now(timezone.utc)
    db.add(PipelineStep(project_id=row.id, name="Repository Analysis", status="failed", error="Unsupported source", started_at=now - timedelta(seconds=12), completed_at=now))
    db.add(Deployment(project_id=row.id, environment="staging", version="blue", status="FAILED", deployment_type="remote_staging", created_at=now, details={"namespace": "demo-p1-a", "started_at": (now - timedelta(seconds=30)).isoformat(), "completed_at": now.isoformat(), "failure_reason": "Rollout failed", "logs": []}))
    db.commit()
    payload = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in payload["stages"]}
    assert by_id["source_analysis"]["status"] == "Failed"
    assert by_id["architecture"]["status"] == "Blocked"
    assert by_id["staging"]["status"] == "Failed"
    assert by_id["staging"]["execution_seconds"] == 30
    assert by_id["approval"]["status"] == "Blocked"
    assert payload["release"]["approval_status"] == "Blocked"
    db.close()


def test_remote_completion_is_not_guessed_when_worker_timestamp_is_missing():
    db = Session(); row = project(db)
    started = datetime.now(timezone.utc) - timedelta(seconds=20)
    db.add(Deployment(project_id=row.id, environment="staging", version="blue", status="SUCCEEDED", deployment_type="remote_staging", created_at=started, details={"target": "aws-ec2-k3s", "namespace": "demo-p1-a", "started_at": started.isoformat()}))
    db.commit()
    result = get_release_workflow(row.id, db)
    staging = next(stage for stage in result["stages"] if stage["id"] == "staging")
    assert staging["status"] == "Completed"
    assert staging["completed_at"] is None
    assert staging["duration_seconds"] is None
    db.close()


def test_remote_worker_phase_drives_build_stage_status():
    db = Session(); row = project(db)
    db.add(PipelineStep(project_id=row.id, name="Repository Analysis", status="completed", started_at=datetime.now(timezone.utc) - timedelta(minutes=2), completed_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    db.add(PipelineStep(project_id=row.id, name="Repository Intelligence", status="completed", started_at=datetime.now(timezone.utc) - timedelta(minutes=1), completed_at=datetime.now(timezone.utc)))
    db.add(Deployment(project_id=row.id, environment="staging", version="blue", status="BUILDING", deployment_type="remote_staging", details={"target": "aws-ec2-k3s", "namespace": "demo-p1-a", "phase": "BUILDING", "logs": [{"message": "Building container image"}]}))
    db.commit()
    result = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in result["stages"]}
    assert by_id["build"]["status"] == "Running"
    assert by_id["staging"]["status"] == "Running"
    assert by_id["build"]["completed_at"] is None
    db.close()


def test_queued_release_and_skipped_test_statuses_are_distinct():
    db = Session(); row = project(db)
    db.add(PipelineStep(project_id=row.id, name="Repository Analysis", status="completed", result="Source parsed"))
    db.add(PipelineStep(project_id=row.id, name="Tests", status="completed", result="No test command configured"))
    db.add(Analysis(project_id=row.id, analysis_result={"test_result": {"skipped": True}}))
    db.add(Deployment(project_id=row.id, environment="staging", version="blue", status="QUEUED", deployment_type="remote_staging", details={"target": "aws-ec2-k3s", "namespace": "demo-p1-a"}))
    db.commit()
    result = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in result["stages"]}
    assert by_id["testing"]["status"] == "Skipped"
    assert by_id["staging"]["status"] == "Queued"
    assert by_id["verification"]["status"] == "Blocked"
    db.close()


def test_workflow_uses_persisted_release_approval_state():
    db = Session(); row = project(db)
    staging = Deployment(project_id=row.id, environment="staging", version="blue", status="healthy",
                         deployment_type="staging", details={"smoke": {"status": "passed"}})
    db.add(staging); db.flush()
    db.add(ReleaseApproval(project_id=row.id, staging_deployment_id=staging.id,
                           status="Awaiting Approval", requested_by="release owner",
                           requested_at=datetime.now(timezone.utc)))
    db.commit()
    payload = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in payload["stages"]}
    assert payload["release"]["approval_status"] == "Awaiting Approval"
    assert by_id["approval"]["status"] == "Awaiting Approval"
    assert "waiting for an approval" in by_id["approval"]["output"]
    assert by_id["production"]["status"] == "Blocked"
    db.close()


def test_approval_status_and_waiting_duration_come_from_deployment_events():
    db = Session(); row = project(db)
    candidate_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    approval_at = candidate_at + timedelta(minutes=4)
    db.add(Deployment(project_id=row.id, environment="staging", version="blue", status="SUCCEEDED", deployment_type="remote_staging", created_at=candidate_at - timedelta(minutes=2), details={"target": "aws-ec2-k3s", "namespace": "demo-p1-a", "completed_at": (candidate_at - timedelta(minutes=2)).isoformat(), "smoke": {"status": "passed", "message": "Smoke checks passed"}}))
    db.add(Deployment(project_id=row.id, environment="production", version="green", status="awaiting_approval", deployment_type="production_green", created_at=candidate_at, details={"image": "app:green"}))
    db.add(Deployment(project_id=row.id, environment="production", version="green", status="healthy", deployment_type="traffic_switch", created_at=approval_at, details="Traffic switched"))
    db.commit()
    payload = get_release_workflow(row.id, db)
    by_id = {stage["id"]: stage for stage in payload["stages"]}
    assert by_id["approval"]["status"] == "Completed"
    assert by_id["approval"]["waiting_seconds"] == 240
    assert by_id["production"]["status"] == "Completed"
    db.close()


def test_repository_snapshot_reads_actual_git_revision_and_file_inventory(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial snapshot"], check=True, capture_output=True)
    snapshot = repository_snapshot_metadata(tmp_path)
    assert snapshot["commit_id"]
    assert snapshot["commit_message"] == "initial snapshot"
    assert "main.py" in snapshot["files"]
    assert snapshot["comparison_available"] is False
    assert snapshot["added"] == snapshot["modified"] == snapshot["deleted"] == []
