import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

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
from app.db.models import Analysis, AutoscalingEvent, Deployment, GeneratedFile, Project, ReleaseApproval, WorkerHeartbeat
from app.services import kubernetes as k8s_svc
from app.services.pipeline import upsert_step
from app.services.repository_ai import analyze_deployment_failure
from app.services.deployment_contract import build_deployment_contract
from app.services.deployment_control import DeploymentCancelled
from app.services.monitoring import monitoring_service
from app.services.release_approvals import finish_staging_tracking, request_release_approval, start_release_tracking, transition_release

router = APIRouter(prefix="/api/projects", tags=["deployments"])
logger = logging.getLogger("deploymind")


class DeploymentTargetRequest(BaseModel):
    target: str = "remote"


class AutoscalingRequest(BaseModel):
    min_replicas: int = Field(ge=1, le=8)
    max_replicas: int = Field(ge=1, le=8)
    target_cpu_utilization: int = Field(ge=10, le=95)
    target_memory_utilization: int | None = Field(default=None, ge=10, le=95)
    behavior: dict = Field(default_factory=lambda: {"scale_up_stabilization_seconds": 0, "scale_down_stabilization_seconds": 300})


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


def _bg_deploy_staging(project_id: int, namespace: str | None = None, target: str = "local", release_id: int | None = None):
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
        db.flush()
        release = db.get(ReleaseApproval, release_id) if release_id else db.query(ReleaseApproval).filter(
            ReleaseApproval.project_id == project_id, ReleaseApproval.status == "Staging",
            ReleaseApproval.staging_deployment_id.is_(None),
        ).order_by(ReleaseApproval.id.desc()).first()
        if release:
            release.staging_deployment_id = row.id
            finish_staging_tracking(db, row, success=True)
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
            db.flush()
            release = db.get(ReleaseApproval, release_id) if release_id else db.query(ReleaseApproval).filter(
                ReleaseApproval.project_id == project_id, ReleaseApproval.status == "Staging",
                ReleaseApproval.staging_deployment_id.is_(None),
            ).order_by(ReleaseApproval.id.desc()).first()
            if release:
                release.staging_deployment_id = db.query(Deployment).filter(Deployment.project_id == project_id, Deployment.deployment_type == "staging").order_by(Deployment.id.desc()).first().id
                finish_staging_tracking(db, release_staging := db.get(Deployment, release.staging_deployment_id), success=False, failure_reason=str(exc))
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
            failed_deployment = Deployment(
                    project_id=project_id,
                    environment="staging",
                    version="blue",
                    status="failed",
                    deployment_type="staging",
                    details={"error": msg, "namespace": namespace, "failure_analysis": failure_analysis},
                )
            db.add(failed_deployment)
            db.flush()
            release = db.get(ReleaseApproval, release_id) if release_id else db.query(ReleaseApproval).filter(
                ReleaseApproval.project_id == project_id, ReleaseApproval.status == "Staging",
                ReleaseApproval.staging_deployment_id.is_(None),
            ).order_by(ReleaseApproval.id.desc()).first()
            if release:
                release.staging_deployment_id = failed_deployment.id
                finish_staging_tracking(db, failed_deployment, success=False, failure_reason=msg)
            monitoring_service.process_deployment_outcome(db, failed_deployment)
            db.commit()
    finally:
        db.close()


def _bg_deploy_production(project_id: int, approval_id: int):
    db = SessionLocal()
    project = None
    approval = None
    try:
        approval = db.query(ReleaseApproval).filter(ReleaseApproval.id == approval_id).with_for_update().first()
        if approval is None or approval.project_id != project_id or approval.decision != "approved" or approval.status != "Production Deploying":
            logger.error("production rollout refused without a current approved release (project=%s approval=%s)", project_id, approval_id)
            return
        staging = db.get(Deployment, approval.staging_deployment_id) if approval.staging_deployment_id else None
        from app.services.release_approvals import staging_verification
        verified, reason, _ = staging_verification(db, staging) if staging else (False, "Verified staging deployment is missing", {})
        if not verified:
            transition_release(db, approval, "Failed", event_type="production_gate_revalidation_failed", comments=reason)
            project = db.get(Project, project_id)
            if project:
                project.status = "production_failed"
            upsert_step(db, project_id, "Production", "failed", error=reason)
            db.commit()
            return
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
        green_result = result.model_dump()
        green_smoke = green_result.get("smoke") or {}
        if green_smoke.get("status") not in {"passed", "success", "healthy"}:
            raise RuntimeError("Production GREEN smoke verification did not pass; traffic was not switched")

        row = Deployment(
            project_id=project_id,
            environment="production",
            version="green",
            status="healthy",
            deployment_type="production_green",
            details={**green_result, "approval_id": approval.id, "promoted": False},
        )
        db.add(row)
        db.flush()
        switch_result = run_switch_traffic(sanitize_name(project.repository_name or "app"))
        switch_smoke = switch_result.get("smoke") if isinstance(switch_result, dict) else None
        if (not isinstance(switch_result, dict) or switch_result.get("status") != "healthy"
                or not isinstance(switch_smoke, dict) or switch_smoke.get("status") not in {"passed", "success", "healthy"}):
            raise RuntimeError("Production traffic switch or post-switch smoke verification did not pass")
        switch = Deployment(project_id=project_id, environment="production", version="green", status="healthy",
                            deployment_type="traffic_switch", details={**(switch_result if isinstance(switch_result, dict) else {}), "approval_id": approval.id})
        db.add(switch)
        row_details = dict(row.details or {})
        row_details["promoted"] = True
        row_details["traffic_switch_deployment_id"] = None
        row.details = row_details
        db.flush()
        row_details["traffic_switch_deployment_id"] = switch.id
        row.details = row_details
        approval.production_deployment_id = row.id
        transition_release(db, approval, "Production Healthy", event_type="production_deployment_healthy",
                           details={"production_deployment_id": row.id, "traffic_switch_deployment_id": switch.id})
        project.status = "production_healthy"
        upsert_step(db, project_id, "Production", "completed", result="Production GREEN rollout and traffic switch completed")
        upsert_step(db, project_id, "Completed", "completed", result="Production active on GREEN")
        db.commit()
    except Exception as exc:
        logger.exception("production background deploy failed")
        msg = str(exc)
        upsert_step(db, project_id, "Production", "failed", error=msg)
        project = db.get(Project, project_id)
        approval = db.get(ReleaseApproval, approval_id)
        if project:
            failure_analysis = analyze_deployment_failure(
                sanitize_name(project.repository_name or "app"),
                settings.k8s_namespace,
                msg,
            )
            project.status = "production_failed"
            failed_deployment = Deployment(
                    project_id=project_id,
                    environment="production",
                    version="green",
                    status="failed",
                    deployment_type="production_green",
                    details={"error": msg, "failure_analysis": failure_analysis},
                )
            db.add(failed_deployment)
            db.flush()
            if approval and approval.status == "Production Deploying":
                approval.production_deployment_id = failed_deployment.id
                transition_release(db, approval, "Failed", event_type="production_deployment_failed", comments=msg[:4000],
                                   details={"production_deployment_id": failed_deployment.id})
            monitoring_service.process_deployment_outcome(db, failed_deployment)
            db.commit()
    finally:
        db.close()


@router.post("/{project_id}/deploy/staging", status_code=202)
def deploy_staging(
    project_id: int,
    background_tasks: BackgroundTasks,
    body: DeploymentTargetRequest = Body(default=DeploymentTargetRequest()),
    requester_identity: str | None = Header(default=None, alias="X-Requester-Identity"),
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
        db.flush()
        release = start_release_tracking(db, project, requested_by=requester_identity, staging_deployment_id=row.id)
        project.status = "deploying"
        upsert_step(db, project_id, "Staging", "running", result="Queued for remote EC2 / K3s worker")
        db.commit()
        db.refresh(row)
        db.refresh(release)
        return {"status": "QUEUED", "deployment_id": row.id, "message": "Deployment queued for the remote K3s worker"}

    upsert_step(db, project_id, "Staging", "running")
    project.status = "deploying"
    release = start_release_tracking(db, project, requested_by=requester_identity)
    db.commit()

    namespace = staging_namespace(settings.k8s_namespace, project_id, uuid4().hex[:8])
    background_tasks.add_task(_bg_deploy_staging, project_id, namespace, target, release.id)
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
        finish_staging_tracking(db, queued_job, success=False, failure_reason="Cancelled while queued")
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
def deploy_production(project_id: int, requester_identity: str | None = Header(default=None, alias="X-Requester-Identity"), db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    if not db.query(Analysis).filter(Analysis.project_id == project_id).first():
        return _fail("production", "Run analysis before deploying")
    staging = db.query(Deployment).filter(
        Deployment.project_id == project_id,
        Deployment.deployment_type.in_(["staging", "remote_staging"]),
        Deployment.status.in_(["SUCCEEDED", "healthy", "staging_healthy"]),
    ).order_by(Deployment.created_at.desc(), Deployment.id.desc()).first()
    if staging is None:
        raise HTTPException(status_code=409, detail="A successful staging deployment is required before requesting production approval")
    from app.services.release_approvals import request_release_approval
    try:
        approval, created = request_release_approval(db, project, staging, requester_identity)
        project.status = "release_awaiting_approval"
        upsert_step(db, project_id, "Production", "pending", result="Awaiting release approval")
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(approval)
    return {"status": approval.status, "approval_id": approval.id, "created": created,
            "message": "Release approval request submitted; no production deployment was started"}



@router.post("/{project_id}/deploy/production/approve")
def approve_production(project_id: int, db: Session = Depends(get_db)):
    raise HTTPException(status_code=409, detail="This legacy endpoint cannot approve or deploy releases. Use the authenticated /api/approvals decision endpoint.")


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
        approval = db.query(ReleaseApproval).filter(ReleaseApproval.project_id == project_id,
                                                    ReleaseApproval.status == "Production Healthy").order_by(ReleaseApproval.id.desc()).first()
        if approval:
            transition_release(db, approval, "Rolled Back", event_type="production_rolled_back",
                               details={"rollback_deployment_id": row.id})
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


def _autoscaling_target(project_id: int, deployment_id: int, db: Session):
    project = _get_project(project_id, db)
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or deployment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if deployment.deployment_type not in {"remote_staging", "staging", "production_green"} or deployment.status not in {"SUCCEEDED", "healthy", "staging_healthy", "production_healthy", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail="Autoscaling is available only for a completed CodeDeck-managed deployment")
    details = deployment.details if isinstance(deployment.details, dict) else {}
    namespace = details.get("namespace")
    app_name = details.get("app_name")
    if not namespace or not app_name:
        raise HTTPException(status_code=409, detail="Deployment namespace or application name is missing")
    target_name = f"{app_name}-green" if deployment.deployment_type == "production_green" else f"{app_name}-staging"
    return project, deployment, details, namespace, target_name


def _autoscaling_snapshot(deployment: Deployment, db: Session) -> dict:
    details = deployment.details if isinstance(deployment.details, dict) else {}
    remote = deployment.deployment_type == "remote_staging" or details.get("target") == "aws-ec2-k3s" or (deployment.deployment_type == "production_green" and settings.deployment_target != "local")
    if not remote:
        snapshot = monitoring_service.latest_snapshot() or {}
        fresh = False
        sampled_at = snapshot.get("sampled_at")
        if sampled_at:
            try:
                observed = datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
                fresh = (datetime.now(timezone.utc) - observed).total_seconds() <= settings.monitoring_kubernetes_interval_seconds * 3
            except (TypeError, ValueError):
                fresh = False
        message = None if fresh else "Local Kubernetes sample is missing or stale; displayed values may be outdated."
    else:
        heartbeat_row = db.get(WorkerHeartbeat, settings.worker_id)
        snapshot = ((heartbeat_row.details or {}).get("kubernetes_monitoring") or {}) if heartbeat_row else {}
        last_seen = heartbeat_row.last_seen if heartbeat_row else None
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        fresh = bool(last_seen and (datetime.now(timezone.utc) - last_seen).total_seconds() <= settings.worker_heartbeat_interval * 3)
        message = None if fresh else "Remote worker heartbeat is stale; displayed Kubernetes values may be outdated."
    hpa = next((row for row in snapshot.get("hpas", []) if row.get("deployment_id") == deployment.id), None)
    target_name = f"{details.get('app_name')}-green" if deployment.deployment_type == "production_green" else f"{details.get('app_name')}-staging"
    rollout = next((row for row in snapshot.get("deployments", []) if row.get("namespace") == details.get("namespace") and row.get("name") == target_name), None)
    monitoring_status = snapshot.get("status", "unavailable") if fresh else "unavailable"
    metrics = snapshot.get("metrics", {"status": "unavailable", "message": "No Kubernetes metrics sample is available."})
    if not fresh:
        metrics = {"status": "unavailable", "message": message, "pods": [], "nodes": []}
    return {"monitoring_status": monitoring_status, "sampled_at": snapshot.get("sampled_at"), "message": message,
            "metrics": metrics, "hpa": hpa, "rollout": rollout}


@router.get("/{project_id}/deployments/{deployment_id}/autoscaling")
def get_autoscaling(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    _, deployment, details, _, _ = _autoscaling_target(project_id, deployment_id, db)
    status = _autoscaling_snapshot(deployment, db)
    events = list(reversed(db.query(AutoscalingEvent).filter(AutoscalingEvent.deployment_id == deployment.id).order_by(AutoscalingEvent.occurred_at.desc(), AutoscalingEvent.id.desc()).limit(200).all()))
    latest_job = next((job for job in db.query(Deployment).filter(Deployment.project_id == deployment.project_id, Deployment.deployment_type == "remote_autoscaling").order_by(Deployment.created_at.desc()).all() if (job.details or {}).get("source_deployment_id") == deployment.id), None)
    operation = details.get("autoscaling_operation")
    if latest_job:
        job_details = latest_job.details if isinstance(latest_job.details, dict) else {}
        result = job_details.get("result") or {}
        operation = {"job_id": latest_job.id, "status": latest_job.status,
                     "completed_at": job_details.get("completed_at"),
                     "message": result.get("message") or job_details.get("failure_reason") or "Autoscaling operation is queued or running."}
    return {"project_id": project_id, "deployment_id": deployment.id, "namespace": details["namespace"], "deployment_name": f"{details['app_name']}-green" if deployment.deployment_type == "production_green" else f"{details['app_name']}-staging",
            "configuration": details.get("autoscaling"), "operation": operation,
            **status,
            "events": [{"id": event.id, "event_type": event.event_type, "occurred_at": event.occurred_at.isoformat(),
                        "current_replicas": event.current_replicas, "desired_replicas": event.desired_replicas,
                        "details": event.details or {}} for event in events]}


def _submit_autoscaling(project_id: int, deployment_id: int, config: dict | None, remove: bool, db: Session):
    _, deployment, details, namespace, deployment_name = _autoscaling_target(project_id, deployment_id, db)
    try:
        normalized = k8s_svc.validate_autoscaling_config(config) if config is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    target = details.get("target")
    remote = deployment.deployment_type == "remote_staging" or target == "aws-ec2-k3s" or (deployment.deployment_type == "production_green" and settings.deployment_target != "local")
    action = "remove_autoscaling" if remove else "configure_autoscaling"
    if remote:
        if not settings.remote_worker_token:
            raise HTTPException(status_code=503, detail="Remote K3s worker is not configured")
        active = next((job for job in db.query(Deployment).filter(Deployment.deployment_type == "remote_autoscaling", Deployment.status.in_(["QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING"])).all() if (job.details or {}).get("source_deployment_id") == deployment_id), None)
        if active:
            raise HTTPException(status_code=409, detail=f"Autoscaling operation {active.id} is already {active.status.lower()}")
        previous = details.get("autoscaling") or {}
        payload = {"operation": action, "project_id": project_id, "source_deployment_id": deployment_id,
                   "namespace": namespace, "deployment_name": deployment_name,
                   "original_replicas": previous.get("original_replicas")}
        if normalized is not None:
            payload["config"] = normalized
        job = Deployment(project_id=project_id, environment=deployment.environment, version="autoscaling",
                         status="QUEUED", deployment_type="remote_autoscaling",
                         details={"target": "aws-ec2-k3s", "namespace": namespace, "source_deployment_id": deployment_id,
                                  "operation": action, "logs": [{"message": "Autoscaling operation queued for the EC2 worker"}],
                                  "worker_payload": payload})
        db.add(job)
        db.commit()
        db.refresh(job)
        return {"status": "QUEUED", "job_id": job.id, "message": "Autoscaling operation queued for the remote K3s worker"}
    previous = details.get("autoscaling") or {}
    try:
        with use_deployment_target("local"):
            if remove:
                result = k8s_svc.remove_autoscaler(namespace, deployment_name, deployment_id, previous.get("original_replicas"))
            else:
                result = k8s_svc.apply_autoscaler(namespace, deployment_name, deployment_id, normalized, previous.get("original_replicas"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:1000]) from exc
    details = dict(details)
    if remove:
        details.pop("autoscaling", None)
        event_type = "disabled"
    else:
        details["autoscaling"] = result
        event_type = "configured"
    details["autoscaling_operation"] = {"status": "SUCCEEDED", "completed_at": datetime.now(timezone.utc).isoformat(), "message": result["message"]}
    deployment.details = details
    db.add(AutoscalingEvent(project_id=project_id, deployment_id=deployment_id, event_type=event_type,
                            occurred_at=datetime.now(timezone.utc), details={"config": normalized, "message": result["message"]}))
    db.commit()
    return {"status": "SUCCEEDED", "message": result["message"], "result": result}


@router.post("/{project_id}/deployments/{deployment_id}/autoscaling")
def configure_autoscaling(project_id: int, deployment_id: int, body: AutoscalingRequest, db: Session = Depends(get_db)):
    if body.min_replicas > body.max_replicas:
        raise HTTPException(status_code=422, detail="min_replicas cannot exceed max_replicas")
    return _submit_autoscaling(project_id, deployment_id, body.model_dump(), False, db)


@router.delete("/{project_id}/deployments/{deployment_id}/autoscaling")
def disable_autoscaling(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    return _submit_autoscaling(project_id, deployment_id, None, True, db)


@router.post("/{project_id}/deployments/{deployment_id}/cleanup", status_code=202)
def cleanup_remote_deployment(project_id: int, deployment_id: int, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or deployment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if deployment.status == "CLEANED_UP":
        return {"status": "CLEANED_UP", "message": "Deployment resources were already removed"}
    if deployment.deployment_type not in {"remote_staging", "staging", "production_green"} or deployment.status in {"QUEUED", "RUNNING", "BUILDING", "DEPLOYING", "VERIFYING", "CANCELLED", "CLEANED_UP"}:
        raise HTTPException(status_code=409, detail="This deployment is active, already cleaned, or has no supported cleanup path")
    if project.status in {"deploying", "staging_cancel_requested"}:
        raise HTTPException(status_code=409, detail="Wait for the active staging operation before cleaning up a deployment")
    namespace = (deployment.details or {}).get("namespace")
    if not namespace:
        raise HTTPException(status_code=409, detail="Deployment has no recorded namespace")
    target = (deployment.details or {}).get("target")
    if target == "local-kind" or deployment.deployment_type == "staging":
        if deployment.deployment_type != "staging" and target != "local-kind":
            raise HTTPException(status_code=409, detail="Deployment target cannot be identified safely")
        if deployment.deployment_type == "production_green":
            raise HTTPException(status_code=409, detail="Production shares a namespace and has no safe per-deployment inventory")
        with use_deployment_target("local"):
            try:
                result = k8s_svc.cleanup_namespace_resources(namespace)
            except Exception as exc:
                details = dict(deployment.details or {})
                details["cleanup"] = {"status": "FAILED", "error": str(exc)[:2000]}
                deployment.details = details
                db.commit()
                raise HTTPException(status_code=502, detail="Local Kubernetes cleanup was incomplete; retry after checking resource results") from exc
        details = dict(deployment.details or {})
        details["cleanup"] = {"status": "SUCCEEDED", **result}
        deployment.details = details
        deployment.status = "CLEANED_UP"
        db.commit()
        return {"status": "CLEANED_UP", "message": result["message"], "result": result}
    if deployment.deployment_type != "remote_staging":
        raise HTTPException(status_code=409, detail="This deployment has no isolated, verified resource inventory for safe cleanup")
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
            "worker_payload": {"operation": "cleanup", "project_id": project_id, "namespace": namespace, "source_deployment_id": deployment.id},
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


