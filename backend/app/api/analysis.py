import logging
from pathlib import Path

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
from app.db.models import Analysis, GeneratedFile, PipelineStep, Project
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
