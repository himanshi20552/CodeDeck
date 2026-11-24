import logging
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.schemas import (
    AnalysisOut,
    DeploymentContractOut,
    EvaluationOut,
    GeneratedFileOut,
    GeneratedTestRunOut,
    PipelineStepOut,
    QuestionRequest,
    RepositoryAnswerOut,
)
from app.db.database import get_db
from app.db.models import Analysis, Deployment, GeneratedFile, PipelineStep, Project, ReleaseApproval
from app.graph.workflow import run_analysis_workflow
from app.services.repository_ai import (
    answer_repository_question,
    ensure_repository_index,
    evaluate_repository_index,
    run_ai_generated_tests,
)
from app.services.deployment_contract import build_deployment_contract

router = APIRouter(prefix="/api/projects", tags=["analysis"])
logger = logging.getLogger("deploymind")

WORKFLOW_STAGES = [
    ("development", "Development / Repository Connected", [], "Developer", "Repository registration"),
    ("source_analysis", "Source Code Analysis", ["development"], "Repository Analysis agent", "Repository Analysis"),
    ("architecture", "Dependency and Architecture Analysis", ["source_analysis"], "Repository Intelligence agent", "Repository Intelligence"),
    ("quality_security", "Code Quality and Security Checks", ["source_analysis"], "Security agent / validator", "Security"),
    ("testing", "Automated Testing", ["source_analysis"], "Test agent", "Tests"),
    ("build", "Build and Containerization", ["architecture", "quality_security", "testing"], "Docker agent; image build during staging", "Docker"),
    ("staging", "Staging Deployment", ["build"], "Deployment agent / EC2 worker", "Staging"),
    ("verification", "Staging Verification / Smoke Tests", ["staging"], "Deployment agent", None),
    ("approval", "Manager Approval", ["verification"], "Release manager (manual action)", None),
    ("production", "Production Deployment / Promotion", ["approval"], "Deployment operator", "Production"),
    ("monitoring", "Post-deployment Monitoring", ["production"], "Not integrated", None),
]


def _time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(start, end):
    start, end = _as_datetime(start), _as_datetime(end)
    if not start or not end:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds()))


def _step_status(step, analysis_payload):
    if not step:
        return "Not Started"
    status = (step.status or "").lower()
    if status == "running":
        return "Running"
    if status == "failed":
        return "Failed"
    if status in {"completed", "warning"}:
        if step.name == "Tests" and (analysis_payload.get("test_result") or {}).get("skipped"):
            return "Skipped"
        return "Completed"
    if status in {"queued", "pending"}:
        return "Queued" if status == "queued" else "Not Started"
    return "Not Started"


def _deployment_times(row):
    details = row.details if isinstance(row.details, dict) else {}
    started = details.get("started_at")
    completed = details.get("completed_at")
    if not completed and row.status in {"SUCCEEDED", "healthy", "awaiting_approval", "staging_healthy", "production_healthy", "CLEANED_UP", "rolled_back"}:
        # Remote staging rows are timestamped when queued; never treat that as
        # a completion time if an older worker record omitted completed_at.
        if not (row.deployment_type == "remote_staging" or details.get("target") == "aws-ec2-k3s"):
            completed = row.created_at
    return started, completed


def _get_project(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _fail(step: str, message: str, details: str | None = None, status_code: int = 400):
    body = {"status": "failed", "step": step, "message": message, "details": details}
    return JSONResponse(status_code=status_code, content=body)


@router.post("/{project_id}/analyze", response_model=AnalysisOut)
def start_analysis(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    if project.status in {"analyzing", "deploying", "staging_cancel_requested"}:
        raise HTTPException(status_code=409, detail="An analysis or deployment operation is already active for this project")
    logger.info("analysis started for project %s", project_id)

    final = run_analysis_workflow(project_id, project.repository_url, project.repository_name)
    db.refresh(project)

    if final.get("status") == "failed" and not db.query(Analysis).filter(Analysis.project_id == project_id).first():
        errors = final.get("errors") or []
        first = errors[0] if errors else {"step": "workflow", "message": "Workflow failed"}
        return _fail(first.get("step", "workflow"), first.get("message", "Workflow failed"))

    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        return _fail("workflow", "Analysis did not produce a result", status_code=500)

    # Partial failure after analysis still returns analysis so the UI can show files/pipeline
    return analysis


@router.get("/{project_id}/analysis", response_model=AnalysisOut | None)
def get_analysis(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return db.query(Analysis).filter(Analysis.project_id == project_id).first()


@router.get("/{project_id}/pipeline", response_model=list[PipelineStepOut])
def get_pipeline(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return (
        db.query(PipelineStep)
        .filter(PipelineStep.project_id == project_id)
        .order_by(PipelineStep.id)
        .all()
    )


@router.get("/{project_id}/workflow")
def get_release_workflow(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    payload = dict(analysis.analysis_result or {}) if analysis and isinstance(analysis.analysis_result, dict) else {}
    snapshot = payload.get("repository_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {
            "branch": None, "commit_id": None, "commit_message": None, "files": [], "file_count": None,
            "comparison_available": False, "added": [], "modified": [], "deleted": [],
            "change_summary": "Git metadata was not captured for this analysis. Rerun analysis to record the current repository snapshot.",
            "snapshot_available": False,
        }
    else:
        snapshot = {**snapshot, "snapshot_available": True}

    steps = {row.name: row for row in db.query(PipelineStep).filter(PipelineStep.project_id == project_id).all()}
    rows = db.query(Deployment).filter(Deployment.project_id == project_id).order_by(Deployment.created_at.asc(), Deployment.id.asc()).all()
    details = {row.id: row.details if isinstance(row.details, dict) else {} for row in rows}
    staging_rows = [row for row in rows if row.deployment_type in {"remote_staging", "staging"}]
    production_rows = [row for row in rows if row.deployment_type in {"production_green", "traffic_switch", "rollback"}]
    latest_staging = staging_rows[-1] if staging_rows else None
    green_rows = [row for row in rows if row.deployment_type == "production_green"]
    latest_green = green_rows[-1] if green_rows else None
    switches = [row for row in rows if row.deployment_type == "traffic_switch"]
    latest_switch = switches[-1] if switches else None
    rollbacks = [row for row in rows if row.deployment_type == "rollback"]
    latest_rollback = rollbacks[-1] if rollbacks else None
    approval_query = db.query(ReleaseApproval).filter(ReleaseApproval.project_id == project_id)
    if latest_staging:
        approval_query = approval_query.filter(ReleaseApproval.staging_deployment_id == latest_staging.id)
    latest_approval = approval_query.order_by(ReleaseApproval.created_at.desc(), ReleaseApproval.id.desc()).first()

    def dep_status(row):
        if not row:
            return "Not Started"
        status = (row.status or "").lower()
        if status in {"queued", "pending", "awaiting_approval"}:
            return "Queued"
        if status in {"running", "building", "deploying", "verifying"}:
            return "Running"
        if status in {"failed", "error"}:
            return "Failed"
        if status in {"cancelled", "canceled"}:
            return "Skipped"
        if status in {"succeeded", "healthy", "staging_healthy", "production_healthy", "cleaned_up", "rolled_back"}:
            return "Completed"
        return "Not Started"

    def add_stage(key, title, dependencies, actor, status, started=None, completed=None, logs=None, output=None, waiting=None, execution=None):
        status = status or "Not Started"
        if status == "Not Started" and dependencies:
            completed_dependencies = {item["id"] for item in stages if item["status"] in {"Completed", "Skipped"}}
            blocked_on = [dependency for dependency in dependencies if dependency not in completed_dependencies]
            if blocked_on:
                status = "Blocked"
                output = output or ("Waiting for " + ", ".join(blocked_on))
        stages.append({
            "id": key, "name": title, "status": status, "started_at": _time(started), "completed_at": _time(completed),
            "duration_seconds": _seconds(started, completed), "waiting_seconds": waiting, "execution_seconds": execution,
            "dependencies": dependencies, "actor": actor, "logs": logs or [], "output": output,
        })

    stages = []
    add_stage("development", WORKFLOW_STAGES[0][1], [], "Developer", "Completed", completed=project.created_at,
              logs=["Repository URL registered in CodeDeck"], output="Repository connected; pull request integration is not connected.")

    for key, title, deps, actor, step_name in WORKFLOW_STAGES[1:6]:
        step = steps.get(step_name) if step_name else None
        status = _step_status(step, payload)
        if key == "build":
            image_row = next((row for row in reversed(rows) if details[row.id].get("image") or (details[row.id].get("result") or {}).get("image")), None)
            if image_row:
                status = dep_status(image_row)
                started, completed = _deployment_times(image_row)
                output = f"Container image: {details[image_row.id].get('image') or (details[image_row.id].get('result') or {}).get('image')}"
                logs = list(details[image_row.id].get("logs") or [])
            elif step and step.status == "failed":
                started, completed = step.started_at, step.completed_at
                output = step.error
                logs = [step.result] if step.result else []
            else:
                started = completed = None
                output = "Dockerfile and deployment configuration are generated during analysis; the container image is built when staging is manually triggered."
                logs = [step.result] if step and step.result else []
                if step and step.status == "running":
                    status = "Running"
                else:
                    status = "Not Started"
                active_build = latest_staging and latest_staging.status in {"QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"}
                active_details = details.get(latest_staging.id, {}) if active_build else {}
                phase = str(active_details.get("phase") or "").upper()
                progress_text = str((step.result if step else "") or "")
                progress_lower = progress_text.lower()
                if (active_build and phase in {"DEPLOYING", "VERIFYING"}) or any(token in progress_lower for token in ("applying", "starting staging", "waiting for application", "running staging smoke")):
                    status = "Completed"
                    output = "Container build and image preparation completed; deployment is continuing."
                elif (active_build and phase == "BUILDING") or "building container image" in progress_lower or "loading image" in progress_lower:
                    status = "Running"
                    output = progress_text or "Container image build is in progress."
                elif active_build:
                    status = "Queued"
                    output = "Container build is queued as part of the staging deployment."
                if active_build:
                    logs.extend(active_details.get("logs") or [])
        else:
            started = step.started_at if step else None
            completed = step.completed_at if step else None
            output = (step.error or step.result) if step else None
            logs = [value for value in ([step.result, step.error] if step else []) if value]
            if key == "quality_security":
                security = payload.get("security_result") or {}
                validation = payload.get("validation_result") or {}
                if security.get("summary"):
                    logs.append(f"Security scan: {security['summary']}")
                if validation.get("valid") is False:
                    status = "Failed"
                    validation_errors = validation.get("errors") or []
                    output = "; ".join(str(item) for item in validation_errors[:5]) or "Deployment configuration validation failed."
                    logs.extend(str(item) for item in validation_errors[:10])
                elif validation.get("valid") is True and validation.get("message"):
                    logs.append(f"Deployment configuration validation: {validation['message']}")
            if key == "testing" and status == "Skipped":
                output = output or "The configured test stage reported that tests were skipped."
        add_stage(key, title, deps, actor, status, started, completed, logs, output,
                  execution=_seconds(started, completed))

    staging_step = steps.get("Staging")
    staging_status = dep_status(latest_staging) if latest_staging else _step_status(staging_step, payload)
    deployment_started, deployment_completed = _deployment_times(latest_staging) if latest_staging else (None, None)
    staging_started = deployment_started or (staging_step.started_at if staging_step else None)
    staging_completed = deployment_completed or (staging_step.completed_at if staging_step else None)
    staging_details = details.get(latest_staging.id, {}) if latest_staging else {}
    staging_logs = list(staging_details.get("logs") or [])
    if staging_step and staging_step.result:
        staging_logs.append(staging_step.result)
    staging_execution = _seconds(deployment_started, deployment_completed) if deployment_started and deployment_completed else None
    if staging_execution is None and staging_step and (latest_staging is None or latest_staging.deployment_type == "staging"):
        staging_execution = _seconds(staging_step.started_at, staging_step.completed_at)
    add_stage("staging", WORKFLOW_STAGES[6][1], ["build"], WORKFLOW_STAGES[6][3], staging_status,
              staging_started, staging_completed, staging_logs, staging_details.get("failure_reason") or staging_details.get("message") or (staging_step.error if staging_step else None),
              waiting=_seconds(latest_staging.created_at, deployment_started) if latest_staging and deployment_started else None,
              execution=staging_execution)

    smoke = staging_details.get("smoke") or staging_details.get("smoke_test") or (staging_details.get("result") or {}).get("smoke")
    smoke_status = (smoke or {}).get("status") if isinstance(smoke, dict) else None
    verification_status = "Completed" if smoke_status in {"passed", "success", "healthy"} else "Failed" if smoke_status in {"failed", "error"} else "Blocked" if staging_status != "Completed" else "Blocked"
    verification_output = (smoke or {}).get("message") if isinstance(smoke, dict) else None
    if not smoke and staging_status == "Completed":
        verification_output = "No persisted smoke-test result is available for this deployment."
    elif not smoke:
        verification_output = "Staging deployment must complete successfully before smoke verification can run."
    add_stage("verification", WORKFLOW_STAGES[7][1], ["staging"], WORKFLOW_STAGES[7][3], verification_status,
              completed=staging_completed if smoke_status else None, logs=[verification_output] if verification_output else [], output=verification_output)

    candidate_wait_start = _deployment_times(latest_green)[1] if latest_green else None
    if latest_approval:
        approval_status = latest_approval.status
        approval_completed = latest_approval.decision_timestamp
        candidate_wait_start = latest_approval.requested_at
        approval_output = latest_approval.comments or {
            "Awaiting Approval": "Release candidate is waiting for an approval decision.",
            "Production Deploying": "Approval recorded; production rollout is in progress.",
            "Production Healthy": "Approved production release is healthy.",
            "Rejected": "Release was rejected; production was not queued.",
            "Rolled Back": "Release was rolled back after production promotion.",
            "Failed": "Release workflow failed; inspect deployment history for details.",
        }.get(latest_approval.status, latest_approval.status)
    elif latest_switch:
        approval_status = "Completed"
        approval_completed = latest_switch.created_at
        approval_output = "Change approval was recorded by a manual traffic-switch action in CodeDeck."
    elif verification_status != "Completed":
        approval_status = "Blocked"
        approval_completed = None
        approval_output = "Requires successful staging verification before a production release candidate can be approved."
    elif latest_green and latest_green.status == "awaiting_approval":
        approval_status = "Queued"
        approval_completed = None
        approval_output = "Release candidate is awaiting a manual approval action."
    else:
        approval_status = "Not Started"
        approval_completed = None
        approval_output = "No production approval event has been recorded."
    approval_wait = _seconds(candidate_wait_start, approval_completed) if candidate_wait_start and approval_completed else (
        _seconds(candidate_wait_start, datetime.now(timezone.utc)) if candidate_wait_start and approval_status in {"Queued", "Awaiting Approval"} else None
    )
    add_stage("approval", WORKFLOW_STAGES[8][1], ["verification"], WORKFLOW_STAGES[8][3], approval_status,
              started=candidate_wait_start, completed=approval_completed, logs=[approval_output], output=approval_output, waiting=approval_wait)

    if latest_approval and latest_approval.status == "Production Healthy":
        production_status = "Completed"
        production_output = "Approved release is healthy in production."
        prod_time = latest_approval.updated_at
    elif latest_approval and latest_approval.status == "Production Deploying":
        production_status = "Running"
        production_output = "Production deployment started after approval."
        prod_time = None
    elif latest_approval and latest_approval.status == "Failed":
        production_status = "Failed"
        production_output = "Production deployment failed; inspect the release and deployment history."
        prod_time = latest_approval.updated_at
    elif latest_approval and latest_approval.status == "Rolled Back":
        production_status = "Rolled Back"
        production_output = "Production traffic was rolled back."
        prod_time = latest_approval.updated_at
    elif latest_approval and latest_approval.status == "Rejected":
        production_status = "Blocked"
        production_output = "Production promotion is blocked because the release was rejected."
        prod_time = None
    elif latest_switch:
        production_status = "Completed"
        production_output = "Production traffic was promoted to GREEN."
        prod_time = latest_switch.created_at
    elif latest_green and latest_green.status == "failed":
        production_status = "Failed"
        production_output = details[latest_green.id].get("failure_reason") or details[latest_green.id].get("error")
        prod_time = _deployment_times(latest_green)[1] or (steps.get("Production").completed_at if steps.get("Production") else None)
    elif latest_green:
        production_status = "Queued" if approval_status == "Queued" else "Blocked"
        production_output = "GREEN release candidate is deployed; production traffic awaits approval." if approval_status == "Queued" else "Production promotion requires an approved release candidate."
        prod_time = _deployment_times(latest_green)[1]
    else:
        production_status = "Blocked"
        production_output = "Production promotion has not been manually triggered."
        prod_time = None
    prod_logs = list(details.get(latest_green.id, {}).get("logs") or []) if latest_green else []
    if latest_rollback:
        prod_logs.append("Rollback recorded: " + str(details[latest_rollback.id].get("message") or latest_rollback.status))
        rollback_at = _as_datetime(latest_rollback.created_at)
        switch_at = _as_datetime(latest_switch.created_at) if latest_switch else None
        if rollback_at and (switch_at is None or rollback_at > switch_at):
            production_output = "A rollback was recorded after production promotion."
    add_stage("production", WORKFLOW_STAGES[9][1], ["approval"], WORKFLOW_STAGES[9][3], production_status,
              completed=prod_time, logs=prod_logs, output=production_output)
    add_stage("monitoring", WORKFLOW_STAGES[10][1], ["production"], WORKFLOW_STAGES[10][3], "Skipped",
              logs=["No post-deployment monitoring integration is connected."], output="Not integrated; no monitoring event or result is claimed.")

    completed_times = []
    for stage in stages:
        for field in ("started_at", "completed_at"):
            if stage[field]:
                try:
                    timestamp = datetime.fromisoformat(stage[field].replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    completed_times.append(timestamp)
                except ValueError:
                    pass
    total = _seconds(project.created_at, max(completed_times)) if completed_times else None
    if any(stage["status"] == "Running" for stage in stages):
        total = _seconds(project.created_at, datetime.now(timezone.utc))

    latest = rows[-1] if rows else None
    return {
        "project_id": project_id,
        "release": {
            "repository_name": project.repository_name,
            "repository_url": project.repository_url,
            "branch": snapshot.get("branch"),
            "commit_id": snapshot.get("commit_id"),
            "commit_message": snapshot.get("commit_message"),
            "environment": latest.environment if latest else None,
            "version": latest.version if latest else None,
            "status": project.status,
            "approval_status": approval_status,
            "approval_id": latest_approval.id if latest_approval else None,
            "approval_requested_at": _time(latest_approval.requested_at) if latest_approval else None,
            "approval_decision": latest_approval.decision if latest_approval else None,
            "approval_decision_timestamp": _time(latest_approval.decision_timestamp) if latest_approval else None,
        },
        "stages": stages,
        "repository_snapshot": snapshot,
        "deployment_history": [
            {"id": row.id, "environment": row.environment, "version": row.version, "status": row.status,
             "deployment_type": row.deployment_type, "created_at": _time(row.created_at), "details": details[row.id]}
            for row in reversed(rows)
        ],
        "analytics": {"total_duration_seconds": total, "duration_basis": "Persisted project, pipeline, and deployment timestamps; missing start times remain unknown."},
    }


@router.get("/{project_id}/files", response_model=list[GeneratedFileOut])
def get_files(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()


@router.get("/{project_id}/deployment-contract", response_model=DeploymentContractOut)
def get_deployment_contract(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        raise HTTPException(status_code=400, detail="Run analysis before checking deployment readiness")

    files = {
        row.filename: row.content
        for row in db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()
    }
    return build_deployment_contract(project_id, dict(analysis.analysis_result or {}), files)


@router.post("/{project_id}/qa", response_model=RepositoryAnswerOut)
def ask_repository_question(project_id: int, body: QuestionRequest, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        raise HTTPException(status_code=400, detail="Run analysis before asking repository questions")

    payload = dict(analysis.analysis_result or {})
    payload["repository_url"] = project.repository_url
    ensure_repository_index(project_id, project.repository_url)
    return answer_repository_question(project_id, body.question.strip(), payload)


@router.get("/{project_id}/evaluation", response_model=EvaluationOut)
def get_evaluation(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        raise HTTPException(status_code=400, detail="Run analysis before evaluation")

    payload = dict(analysis.analysis_result or {})
    payload["repository_url"] = project.repository_url
    ensure_repository_index(project_id, project.repository_url)
    report = evaluate_repository_index(project_id, payload)
    payload["evaluation_result"] = report.model_dump()
    analysis.analysis_result = payload
    db.commit()
    return report


@router.post("/{project_id}/ai-tests", response_model=GeneratedTestRunOut)
def run_ai_tests(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        raise HTTPException(status_code=400, detail="Run analysis before AI-generated tests")

    payload = dict(analysis.analysis_result or {})
    payload["repository_url"] = project.repository_url
    ensure_repository_index(project_id, project.repository_url)
    result = run_ai_generated_tests(project_id, project.repository_url, payload)
    payload["ai_test_result"] = result.model_dump()
    analysis.analysis_result = payload
    db.commit()
    return result
