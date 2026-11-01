import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.agents.deployment_agent import (
    deploy_green as run_production_green,
    deploy_staging as run_staging,
    rollback as run_rollback,
    switch_traffic as run_switch_traffic,
)
from app.agents.kubernetes_agent import sanitize_name, staging_namespace
from app.api.schemas import DeploymentOut
from app.core.config import settings
from app.core.deployment_context import use_deployment_target
from app.db.database import SessionLocal, get_db
from app.db.models import Analysis, Deployment, GeneratedFile, Project
from app.services import kubernetes as k8s_svc
from app.services.pipeline import upsert_step
from app.services.repository_ai import analyze_deployment_failure
from app.services.deployment_contract import build_deployment_contract
from app.services.deployment_control import DeploymentCancelled

router = APIRouter(prefix="/api/projects", tags=["deployments"])
logger = logging.getLogger("deploymind")


class DeploymentTargetRequest(BaseModel):
    target: str = "remote"


def _get_project(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _fail(step: str, message: str, details: str | None = None, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={"status": "failed", "step": step, "message": message, "details": details},
    )


def _report_staging_progress(project_id: int, message: str) -> None:
    """Persist background deployment phases for the polling frontend."""
    db = SessionLocal()
    try:
        upsert_step(db, project_id, "Staging", "running", result=message)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not persist staging progress", exc_info=True)
    finally:
        db.close()


def _staging_cancel_requested(project_id: int) -> bool:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        return bool(project and project.status == "staging_cancel_requested")
    finally:
        db.close()


def _cleanup_canceled_staging(app_name: str, namespace: str) -> None:
    resources = [
        *(f"deployment/{name}" for name in (f"{app_name}-staging", f"{app_name}-db", f"{app_name}-redis", f"{app_name}-kafka")),
        *(f"service/{name}" for name in (app_name, f"{app_name}-nodeport", f"{app_name}-db", f"{app_name}-redis", f"{app_name}-kafka")),
    ]
    try:
        proc = k8s_svc._run(
            [
                "kubectl", "delete", *resources,
                "-n", namespace,
                "--ignore-not-found=true", "--wait=false", "--timeout=10s",
            ],
            timeout=15,
        )
        if proc.returncode != 0:
            logger.warning("Could not clean up all canceled staging resources: %s", (proc.stderr or proc.stdout or "unknown error")[:1000])
    except Exception:
        logger.warning("Could not clean up canceled staging resources", exc_info=True)


def _bg_deploy_staging(project_id: int, namespace: str | None = None, target: str = "local"):
    db = SessionLocal()
    project = None
    try:
        project = db.get(Project, project_id)
        if not project:
            return
        namespace = namespace or staging_namespace(settings.k8s_namespace, project_id, "legacy")
        analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
        files = {
            f.filename: f.content
            for f in db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()
        }
        dockerfile = files.get("Dockerfile", "")
        k8s_files = {k: v for k, v in files.items() if k != "Dockerfile"}

        with use_deployment_target(target):
            result = run_staging(
                project_id=project_id,
                repository_url=project.repository_url,
                repository_name=project.repository_name,
                analysis=analysis.analysis_result or {} if analysis else {},
                dockerfile=dockerfile,
                k8s_files=k8s_files,
                progress=lambda message: _report_staging_progress(project_id, message),
                cancel_check=lambda: _staging_cancel_requested(project_id),
                namespace=namespace,
            )

        result_details = result.model_dump()
        result_details["target"] = "local-kind"
        row = Deployment(
            project_id=project_id,
            environment="staging",
            version="blue",
            status=result.status,
            deployment_type="staging",
            details=result_details,
        )
        db.add(row)
        project.status = "staging_healthy"
        upsert_step(db, project_id, "Staging", "completed", result=result.message)
        db.commit()
    except DeploymentCancelled as exc:
        logger.info("staging deployment canceled for project %s", project_id)
        project = db.get(Project, project_id)
        if project:
            upsert_step(db, project_id, "Staging", "running", result="Cleaning up staging resources")
            with use_deployment_target(target):
                _cleanup_canceled_staging(sanitize_name(project.repository_name or "app"), namespace)
            project.status = "ready"
            upsert_step(db, project_id, "Staging", "warning", result="Staging deployment canceled")
            db.add(
                Deployment(
                    project_id=project_id,
                    environment="staging",
                    version="blue",
                    status="canceled",
                    deployment_type="staging",
                    details={"message": str(exc), "namespace": namespace},
                )
            )
            db.commit()
    except Exception as exc:
        logger.exception("staging background deploy failed")
        msg = str(exc)
        upsert_step(db, project_id, "Staging", "failed", error=msg)
        project = db.get(Project, project_id)
        if project:
            failure_analysis = analyze_deployment_failure(
                sanitize_name(project.repository_name or "app"),
                namespace,
                msg,
            )
            project.status = "staging_failed"
            db.add(
                Deployment(
                    project_id=project_id,
                    environment="staging",
                    version="blue",
                    status="failed",
                    deployment_type="staging",
                    details={"error": msg, "namespace": namespace, "failure_analysis": failure_analysis},
                )
            )
            db.commit()
    finally:
        db.close()


def _bg_deploy_production(project_id: int):
    db = SessionLocal()
    project = None
    try:
        project = db.get(Project, project_id)
        if not project:
            return
        analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
        files = {
            f.filename: f.content
            for f in db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()
        }
        dockerfile = files.get("Dockerfile", "")
        k8s_files = {k: v for k, v in files.items() if k != "Dockerfile"}

        result = run_production_green(
            project_id=project_id,
            repository_url=project.repository_url,
            repository_name=project.repository_name,
            analysis=analysis.analysis_result or {} if analysis else {},
            dockerfile=dockerfile,
            k8s_files=k8s_files,
        )

        row = Deployment(
            project_id=project_id,
            environment="production",
            version="green",
            status="awaiting_approval",
            deployment_type="production_green",
            details=result.model_dump(),
        )
        db.add(row)
        project.status = "production_awaiting_approval"
        upsert_step(db, project_id, "Production", "running", result="GREEN version deployed and healthy; awaiting approval")
        db.commit()
    except Exception as exc:
        logger.exception("production background deploy failed")
        msg = str(exc)
        upsert_step(db, project_id, "Production", "failed", error=msg)
        project = db.get(Project, project_id)
        if project:
            failure_analysis = analyze_deployment_failure(
                sanitize_name(project.repository_name or "app"),
                settings.k8s_namespace,
                msg,
            )
            project.status = "production_failed"
            db.add(
                Deployment(
                    project_id=project_id,
                    environment="production",
                    version="green",
                    status="failed",
                    deployment_type="production_green",
                    details={"error": msg, "failure_analysis": failure_analysis},
                )
            )
            db.commit()
    finally:
        db.close()


@router.post("/{project_id}/deploy/staging", status_code=202)
def deploy_staging(
    project_id: int,
    background_tasks: BackgroundTasks,
    body: DeploymentTargetRequest = Body(default=DeploymentTargetRequest()),
    db: Session = Depends(get_db),
):
    project = _get_project(project_id, db)
    if project.status in {"deploying", "staging_cancel_requested"}:
        raise HTTPException(status_code=409, detail="A staging operation is already active for this project")
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        return _fail("staging", "Run analysis before deploying")

    files = {
        f.filename: f.content
        for f in db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()
    }
    dockerfile = files.get("Dockerfile", "")
    k8s_files = {k: v for k, v in files.items() if k != "Dockerfile"}
    if not dockerfile or not k8s_files:
        return _fail("staging", "Generated Dockerfile/Kubernetes files are missing")
    contract = build_deployment_contract(project_id, dict(analysis.analysis_result or {}), files)
    if contract.status == "blocked":
        return _fail("staging", contract.summary, "; ".join(check.message for check in contract.checks if check.status == "blocked"))

    target = body.target
    if target not in {"remote", "local"}:
        raise HTTPException(status_code=422, detail="Deployment target must be remote or local")
    if target == "local" and not (settings.local_kind_enabled or settings.deployment_target == "local"):
        raise HTTPException(status_code=403, detail="Local kind is disabled by the backend configuration")

    if target == "remote":
        if not settings.remote_worker_token:
            raise HTTPException(status_code=503, detail="Remote deployment is selected but REMOTE_WORKER_TOKEN is not configured on the backend")
        namespace = staging_namespace(settings.k8s_namespace, project_id, uuid4().hex[:8])
        row = Deployment(
            project_id=project_id,
            environment="staging",
            version="blue",
            status="QUEUED",
            deployment_type="remote_staging",
            details={
                "target": "aws-ec2-k3s",
                "namespace": namespace,
                "logs": [{"message": "Deployment queued for the EC2 worker"}],
                "worker_payload": {
                    "project_id": project_id,
                    "repository_url": project.repository_url,
                    "repository_name": project.repository_name,
                    "analysis": dict(analysis.analysis_result or {}),
                    "k8s_files": {name: content for name, content in files.items() if name.lower().endswith((".yaml", ".yml"))},
                    "namespace": namespace,
                },
            },
        )
        db.add(row)
        project.status = "deploying"
        upsert_step(db, project_id, "Staging", "running", result="Queued for remote EC2 / K3s worker")
        db.commit()
        db.refresh(row)
        return {"status": "QUEUED", "deployment_id": row.id, "message": "Deployment queued for the remote K3s worker"}

    upsert_step(db, project_id, "Staging", "running")
    project.status = "deploying"
    db.commit()

    namespace = staging_namespace(settings.k8s_namespace, project_id, uuid4().hex[:8])
    background_tasks.add_task(_bg_deploy_staging, project_id, namespace, target)
    return {"status": "deploying", "target": "local-kind", "message": "Local kind staging deployment started in background"}


@router.post("/{project_id}/deploy/staging/cancel")
def cancel_staging(project_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    queued_job = (
        db.query(Deployment)
        .filter(Deployment.project_id == project_id, Deployment.deployment_type == "remote_staging", Deployment.status == "QUEUED")
        .order_by(Deployment.created_at.desc())
        .first()
    )
    if queued_job:
        details = dict(queued_job.details or {})
        details.pop("worker_payload", None)
        details["failure_reason"] = "Cancelled while queued"
        details["completed_at"] = datetime.now(timezone.utc).isoformat()
        queued_job.details = details
        queued_job.status = "CANCELLED"
        project.status = "ready"
        upsert_step(db, project_id, "Staging", "warning", result="Queued remote deployment cancelled")
        db.commit()
        return {"status": "CANCELLED", "message": "Queued deployment cancelled"}
    running_remote = db.query(Deployment).filter(
        Deployment.project_id == project_id,
        Deployment.deployment_type == "remote_staging",
        Deployment.status.in_(["RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"]),
    ).first()
    if running_remote:
        raise HTTPException(status_code=409, detail="The EC2 worker cannot interrupt a build already in progress; wait for it to finish or fail")
    if project.status not in {"deploying", "staging_cancel_requested"}:
        return _fail("staging_cancel", "There is no active staging deployment to cancel")
    if project.status == "staging_cancel_requested":
        return {"status": "cancel_requested", "message": "Cancellation is already in progress."}
    project.status = "staging_cancel_requested"
    upsert_step(db, project_id, "Staging", "warning", result="Cancellation requested")
    db.commit()
    return {"status": "cancel_requested", "message": "Cancellation requested. The active build or rollout will stop shortly."}


@router.post("/{project_id}/deploy/production", status_code=202)
def deploy_production(project_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if settings.deployment_target == "remote":
        raise HTTPException(status_code=501, detail="Production blue-green operations have not been migrated to the remote worker")
    project = _get_project(project_id, db)
    analysis = db.query(Analysis).filter(Analysis.project_id == project_id).first()
    if not analysis:
        return _fail("production", "Run analysis before deploying")

    files = {
        f.filename: f.content
        for f in db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).all()
    }
    dockerfile = files.get("Dockerfile", "")
    k8s_files = {k: v for k, v in files.items() if k != "Dockerfile"}
    if not dockerfile or not k8s_files:
        return _fail("production", "Generated Dockerfile/Kubernetes files are missing")

    upsert_step(db, project_id, "Production", "running")
    project.status = "deploying_green"
    db.commit()

    background_tasks.add_task(_bg_deploy_production, project_id)
    return {"status": "deploying_green", "message": "Production GREEN deployment started in background"}



@router.post("/{project_id}/deploy/production/approve")
def approve_production(project_id: int, db: Session = Depends(get_db)):
    if settings.deployment_target == "remote":
        raise HTTPException(status_code=501, detail="Production traffic switching is not available through the remote worker yet")
    project = _get_project(project_id, db)
    app_name = sanitize_name(project.repository_name or "app")

    try:
        res = run_switch_traffic(app_name)
        row = Deployment(
            project_id=project_id,
            environment="production",
            version="green",
            status="healthy",
            deployment_type="traffic_switch",
            details=res,
        )
        db.add(row)
        project.status = "production_healthy"
        db.commit()
        db.refresh(row)

        upsert_step(db, project_id, "Production", "completed", result="Traffic switched to GREEN")
        upsert_step(db, project_id, "Completed", "completed", result="Production active on GREEN")
        return {
            "status": "healthy",
            "message": "Production approved. Live traffic routed to GREEN.",
            "deployment": DeploymentOut.model_validate(row).model_dump(mode="json"),
        }
    except Exception as exc:
        logger.exception("approve production failed")
        msg = str(exc)
        upsert_step(db, project_id, "Production", "failed", error=msg)
        return _fail("production_approval", "Failed to switch traffic to GREEN", msg, status_code=500)


@router.post("/{project_id}/rollback")
def rollback(project_id: int, db: Session = Depends(get_db)):
    if settings.deployment_target == "remote":
        raise HTTPException(status_code=501, detail="Production rollback is not available through the remote worker yet")
    project = _get_project(project_id, db)
    app_name = sanitize_name(project.repository_name or "app")

    try:
        res = run_rollback(app_name)
        row = Deployment(
            project_id=project_id,
            environment="production",
            version="blue",
            status="rolled_back",
            deployment_type="rollback",
            details=res,
        )
        db.add(row)
        project.status = "rolled_back"
        db.commit()
        db.refresh(row)

        upsert_step(db, project_id, "Production", "warning", result="Rolled back traffic to BLUE")
        upsert_step(db, project_id, "Completed", "warning", result="Rolled back to BLUE version")
        return {
            "status": "rolled_back",
            "message": "Rollback complete. Live traffic switched back to BLUE.",
            "deployment": DeploymentOut.model_validate(row).model_dump(mode="json"),
        }
    except Exception as exc:
        logger.exception("rollback failed")
        msg = str(exc)
        upsert_step(db, project_id, "Production", "failed", error=msg)
        return _fail("rollback", "Rollback failed", msg, status_code=500)


@router.get("/{project_id}/deployments", response_model=list[DeploymentOut])
def list_deployments(project_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return (
        db.query(Deployment)
        .filter(Deployment.project_id == project_id)
        .order_by(Deployment.created_at.desc())
        .all()
    )


@router.get("/{project_id}/deployments/{deployment_id}/logs")
def deployment_logs(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    row = db.get(Deployment, deployment_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    details = row.details or {}
    return {"deployment_id": row.id, "status": row.status, "logs": details.get("logs", [])}


@router.get("/{project_id}/deployments/{deployment_id}", response_model=DeploymentOut)
def get_deployment(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    row = db.get(Deployment, deployment_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return row


@router.post("/{project_id}/deployments/{deployment_id}/cleanup", status_code=202)
def cleanup_remote_deployment(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or deployment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if deployment.deployment_type != "remote_staging" or deployment.status != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="Only a completed remote staging deployment can be cleaned up")
    if project.status in {"deploying", "staging_cancel_requested"}:
        raise HTTPException(status_code=409, detail="Wait for the active staging operation before cleaning up a deployment")
    namespace = (deployment.details or {}).get("namespace")
    if not namespace:
        raise HTTPException(status_code=409, detail="Deployment has no recorded namespace")
    if not settings.remote_worker_token:
        raise HTTPException(status_code=503, detail="Remote worker is not configured")
    row = Deployment(
        project_id=project_id,
        environment="staging",
        version="cleanup",
        status="QUEUED",
        deployment_type="remote_cleanup",
        details={
            "target": "aws-ec2-k3s",
            "namespace": namespace,
            "logs": [{"message": "Namespace cleanup queued"}],
            "source_deployment_id": deployment.id,
            "worker_payload": {"operation": "cleanup", "namespace": namespace, "source_deployment_id": deployment.id},
        },
    )
    db.add(row)
    project.status = "deploying"
    db.commit()
    db.refresh(row)
    return {"status": "QUEUED", "deployment_id": row.id, "message": "Remote namespace cleanup queued"}


@router.api_route("/{project_id}/preview", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/{project_id}/preview/{subpath:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def preview_app(project_id: int, request: Request, subpath: str = "", db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    latest_remote = db.query(Deployment).filter(
        Deployment.project_id == project_id,
        Deployment.deployment_type == "remote_staging",
        Deployment.status == "SUCCEEDED",
    ).order_by(Deployment.created_at.desc()).first()
    if latest_remote and settings.deployment_target == "remote":
        raise HTTPException(status_code=501, detail="Remote applications use their configured ingress URL; the control-plane preview proxy is local-kind only")
    app_name = sanitize_name(project.repository_name or "app")
    latest_healthy_staging = (
        db.query(Deployment)
        .filter(
            Deployment.project_id == project_id,
            Deployment.deployment_type == "staging",
            Deployment.status == "healthy",
        )
        .order_by(Deployment.created_at.desc())
        .first()
    )
    details = (latest_healthy_staging.details or {}) if latest_healthy_staging else {}
    namespace = details.get("namespace") or settings.k8s_namespace
    preview_prefix = f"/api/projects/{project_id}/preview"

    try:
        local_port = k8s_svc.port_forward_manager.get_port(app_name, namespace)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to Kubernetes service: {exc}")

    cleaned_subpath = subpath.lstrip("/")
    url = f"http://127.0.0.1:{local_port}/{cleaned_subpath}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body if body else None,
                follow_redirects=False,
            )

            # If user requested root / and it returned 404 (very common for APIs), check /docs
            if cleaned_subpath in ("", "/") and resp.status_code == 404 and request.method == "GET":
                try:
                    docs_check = await client.get(f"http://127.0.0.1:{local_port}/docs", timeout=2.0)
                    if docs_check.status_code < 400:
                        return RedirectResponse(url=f"{preview_prefix}/docs", status_code=307)
                except Exception:
                    pass

            # Filter hop-by-hop headers
            excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
            resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

            # Rewrite redirects
            if "location" in resp_headers:
                loc = resp_headers["location"]
                if loc.startswith("/") and not loc.startswith(preview_prefix):
                    resp_headers["location"] = f"{preview_prefix}{loc}"

            content_type = resp.headers.get("content-type", "")
            response_bytes = resp.content

            # Rewrite HTML so Swagger UI / ReDoc and relative links point to the preview proxy
            if "text/html" in content_type:
                try:
                    html_text = resp.text
                    base_tag = f'<base href="{preview_prefix}/">'
                    if "<head>" in html_text:
                        html_text = html_text.replace("<head>", f"<head>\n    {base_tag}", 1)
                    elif "<HEAD>" in html_text:
                        html_text = html_text.replace("<HEAD>", f"<HEAD>\n    {base_tag}", 1)

                    # Rewrite Swagger UI / ReDoc URL configs
                    html_text = re.sub(
                        r"""(\burl\s*:\s*['"])/(?!api/projects/\d+/preview/)""",
                        rf"\1{preview_prefix}/",
                        html_text,
                    )
                    html_text = re.sub(
                        r"""(\boauth2RedirectUrl\s*:\s*['"])/(?!api/projects/\d+/preview/)""",
                        rf"\1{preview_prefix}/",
                        html_text,
                    )
                    response_bytes = html_text.encode("utf-8")
                except Exception:
                    pass

            resp_headers["content-length"] = str(len(response_bytes))
            return Response(
                content=response_bytes,
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=content_type,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxy error: {exc}")


