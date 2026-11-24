from fastapi import APIRouter, Depends, HTTPException
from pathlib import Path
import shutil
from sqlalchemy.orm import Session

from app.api.schemas import ProjectCreate, ProjectOut
from app.db.database import get_db
from app.db.models import Deployment, PipelineStep, Project, ReleaseApproval
from app.core.config import settings
from app.services.github import repo_name_from_url, validate_github_url
from app.services.monitoring import monitoring_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    try:
        url = validate_github_url(body.repository_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project = Project(
        repository_url=url,
        repository_name=repo_name_from_url(url),
        status="created",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    monitoring_service.register_project(project.id)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.created_at.desc()).all()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _workspace_paths(project_id: int) -> list[Path]:
    root = Path(settings.workspace_dir).resolve()
    return [root / f"project_{project_id}", root / f"project_{project_id}_deploy", root / f"project_{project_id}_prod_deploy"]


def _active(project: Project, db: Session) -> bool:
    active_statuses = {"analyzing", "deploying", "staging_cancel_requested", "deploying_green"}
    if project.status in active_statuses:
        return True
    if db.query(PipelineStep).filter(PipelineStep.project_id == project.id, PipelineStep.status == "running").first() is not None:
        return True
    return db.query(Deployment).filter(Deployment.project_id == project.id, Deployment.status.in_(["QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"])).first() is not None


@router.post("/{project_id}/workspace/clear")
def clear_workspace(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if _active(project, db):
        raise HTTPException(status_code=409, detail="Wait for analysis and deployment operations to finish before clearing files")
    removed = []
    root = Path(settings.workspace_dir).resolve()
    for path in _workspace_paths(project_id):
        resolved = path.resolve()
        if resolved.parent != root:
            raise HTTPException(status_code=500, detail="Workspace path validation failed")
        if resolved.exists():
            shutil.rmtree(resolved)
            removed.append(resolved.name)
    return {"status": "completed", "message": "Repository and temporary deployment files cleared", "removed": removed}


@router.delete("/{project_id}")
def delete_project(project_id: int, confirmation: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if confirmation != project.repository_name:
        raise HTTPException(status_code=400, detail="Confirmation must match the project name")
    if _active(project, db):
        raise HTTPException(status_code=409, detail="Active analysis or deployment prevents project deletion")
    if any(p.exists() for p in _workspace_paths(project_id)):
        raise HTTPException(status_code=409, detail="Clear repository workspace before deleting the project")
    if db.query(ReleaseApproval.id).filter(ReleaseApproval.project_id == project_id).first() is not None:
        raise HTTPException(status_code=409, detail="Release approval audit history is retained; permanently deleting this project is blocked")
    unresolved = db.query(Deployment).filter(Deployment.project_id == project_id, Deployment.status.notin_(["CLEANED_UP", "CANCELLED"])).first()
    if unresolved:
        raise HTTPException(status_code=409, detail="Undeploy all deployments before deleting the project")
    # Deliberate retention policy: hard deletion removes project-scoped analysis,
    # pipeline, generated-file, and deployment history via existing ORM cascades.
    db.delete(project)
    db.commit()
    return {"status": "deleted", "message": "Project and its associated history were permanently deleted"}
