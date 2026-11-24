from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal, get_db
from app.db.models import Deployment, MonitoringAlert, MonitoringEvent, Project, WorkerHeartbeat
from app.services.monitoring import monitoring_service
from app.services.observability import redact_text

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


def _time(value):
    return value.isoformat() if value else None


def _deployment_event(row: Deployment) -> dict:
    info = row.details if isinstance(row.details, dict) else {}
    logs = []
    for item in (info.get("logs") or [])[-10:]:
        if isinstance(item, dict):
            logs.append({"time": item.get("time"), "message": redact_text(str(item.get("message", "")), 500)})
    return {"id": row.id, "project_id": row.project_id, "environment": row.environment,
            "version": row.version, "status": row.status, "deployment_type": row.deployment_type,
            "created_at": _time(row.created_at), "logs": logs}


def _snapshot() -> tuple[dict, bool, str | None]:
    if settings.deployment_target == "local":
        snapshot = monitoring_service.latest_snapshot()
        return snapshot or {"status": "unavailable", "message": "Waiting for the first Kubernetes API sample.", "deployments": [], "pods": [], "hpas": [], "metrics": {"status": "unavailable", "message": "Metrics sample has not arrived.", "pods": [], "nodes": []}, "alerts": []}, True, None
    db = SessionLocal()
    try:
        heartbeat = db.get(WorkerHeartbeat, settings.worker_id)
        if heartbeat is None:
            return {"status": "unavailable", "message": "No remote worker heartbeat has been received.", "deployments": [], "pods": [], "hpas": [], "metrics": {"status": "unavailable", "message": "Remote worker has not reported Metrics Server data.", "pods": [], "nodes": []}, "alerts": []}, False, None
        details = heartbeat.details or {}
        snapshot = details.get("kubernetes_monitoring")
        last_seen = heartbeat.last_seen
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        online = bool(last_seen and (datetime.now(timezone.utc) - last_seen).total_seconds() <= settings.worker_heartbeat_interval * 3)
        if not isinstance(snapshot, dict):
            snapshot = {"status": "unavailable", "message": "The remote worker has not sent a Kubernetes monitoring sample.", "deployments": [], "pods": [], "hpas": [], "metrics": {"status": "unavailable", "message": "Metrics Server data is unavailable.", "pods": [], "nodes": []}, "alerts": []}
        return snapshot, online, _time(last_seen)
    finally:
        db.close()


def _filter_snapshot(snapshot: dict, project_id: int | None, deployment: Deployment | None, project_namespaces: set[str]) -> dict:
    if not project_id and not deployment:
        return snapshot
    details = deployment.details if deployment and isinstance(deployment.details, dict) else {}
    namespace = details.get("namespace") if deployment else None
    app_name = details.get("app_name") if deployment else None
    if not namespace and project_namespaces:
        allowed = project_namespaces
    elif namespace:
        allowed = {namespace}
    else:
        allowed = set()
    result = dict(snapshot)
    def in_scope(item):
        if deployment:
            if item.get("namespace") not in allowed:
                return False
            if not app_name:
                return True
            if item.get("name"):
                return str(item["name"]).startswith(str(app_name))
            return any(str(name).startswith(str(app_name)) for name in item.get("deployment_names", [])) or item.get("target_name", "").startswith(str(app_name))
        return item.get("namespace") in allowed or (project_id is not None and item.get("project_id") == project_id)
    result["deployments"] = [item for item in snapshot.get("deployments", []) if in_scope(item)]
    result["pods"] = [item for item in snapshot.get("pods", []) if in_scope(item)]
    result["hpas"] = [item for item in snapshot.get("hpas", []) if in_scope(item)]
    metrics = dict(snapshot.get("metrics") or {})
    metrics["pods"] = [item for item in metrics.get("pods", []) if in_scope(item)]
    result["metrics"] = metrics
    result["alerts"] = [item for item in snapshot.get("alerts", []) if item.get("project_id") in {None, project_id} and (not deployment or bool(namespace and namespace in item.get("fingerprint", "")))]
    result["namespaces"] = sorted(allowed)
    return result


@router.get("")
def monitoring_dashboard(
    project_id: int | None = None,
    deployment_id: int | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id) if project_id is not None else None
    if project_id is not None and project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    deployment = db.get(Deployment, deployment_id) if deployment_id is not None else None
    if deployment_id is not None and (deployment is None or (project_id is not None and deployment.project_id != project_id)):
        raise HTTPException(status_code=404, detail="Deployment not found")
    effective_project_id = project.id if project else deployment.project_id if deployment else None

    events_query = db.query(MonitoringEvent)
    alerts_query = db.query(MonitoringAlert)
    deployments_query = db.query(Deployment)
    if effective_project_id is not None:
        events_query = events_query.filter(MonitoringEvent.project_id == effective_project_id)
        alerts_query = alerts_query.filter((MonitoringAlert.project_id == effective_project_id) | (MonitoringAlert.project_id.is_(None)))
        deployments_query = deployments_query.filter(Deployment.project_id == effective_project_id)
    if deployment_id is not None:
        events_query = events_query.filter(MonitoringEvent.deployment_id == deployment_id)
        alerts_query = alerts_query.filter((MonitoringAlert.deployment_id == deployment_id) | (MonitoringAlert.deployment_id.is_(None)))
        deployments_query = deployments_query.filter(Deployment.id == deployment_id)
    if since is not None:
        events_query = events_query.filter(MonitoringEvent.occurred_at >= since)
        alerts_query = alerts_query.filter(MonitoringAlert.last_seen >= since)
        deployments_query = deployments_query.filter(Deployment.created_at >= since)

    events = events_query.order_by(MonitoringEvent.occurred_at.desc()).limit(limit).all()
    alerts = alerts_query.order_by(MonitoringAlert.last_seen.desc()).limit(limit).all()
    deployments = deployments_query.order_by(Deployment.created_at.desc()).limit(limit).all()
    namespace_rows = db.query(Deployment.details).filter(Deployment.project_id == effective_project_id).all() if effective_project_id is not None else []
    namespaces = {item[0].get("namespace") for item in namespace_rows if isinstance(item[0], dict) and item[0].get("namespace")}
    snapshot, worker_online, worker_last_seen = _snapshot()
    snapshot = _filter_snapshot(snapshot, effective_project_id, deployment, namespaces)

    try:
        db.execute(text("SELECT 1"))
        database_health = {"status": "available", "message": "Database connected"}
    except Exception:
        database_health = {"status": "unavailable", "message": "Database is unavailable"}
    monitor_status = monitoring_service.service_status()
    if not monitor_status["enabled"]:
        monitor_state = "disabled"
    elif monitor_status["observer_running"] and monitor_status["event_processor_running"]:
        monitor_state = "available"
    elif monitor_status["last_error"]:
        monitor_state = "degraded"
    else:
        monitor_state = "starting"

    return {
        "filters": {"project_id": effective_project_id, "deployment_id": deployment_id, "since": _time(since), "limit": limit},
        "system_health": {
            "backend": {"status": "available", "message": "Monitoring API is responding"},
            "database": database_health,
            "remote_worker": {"status": "available" if worker_online else "unavailable", "last_seen": worker_last_seen,
                              "message": "Worker heartbeat current" if worker_online else "Remote worker is offline"} if settings.deployment_target == "remote" else {"status": "not_required", "message": "Local Kubernetes target"},
            "kubernetes": {"status": snapshot.get("status", "unavailable"), "message": snapshot.get("message") or "Kubernetes API sample received", "sampled_at": snapshot.get("sampled_at")},
        },
        "monitoring_service": {**monitor_status, "status": monitor_state},
        "kubernetes": snapshot,
        "file_activity": [{"id": event.id, "project_id": event.project_id, "deployment_id": event.deployment_id,
                           "analysis_id": event.analysis_id, "path": event.relative_path, "event_type": event.event_type,
                           "workflow_context": event.workflow_context, "processing_status": event.processing_status,
                           "occurred_at": _time(event.occurred_at), "summary": event.summary} for event in events],
        "deployment_events": [_deployment_event(row) for row in deployments],
        "alerts": [{"id": alert.id, "project_id": alert.project_id, "deployment_id": alert.deployment_id,
                    "source": alert.source, "type": alert.alert_type, "severity": alert.severity,
                    "title": alert.title, "message": alert.message, "occurrence_count": alert.occurrence_count,
                    "is_resolved": alert.is_resolved, "first_seen": _time(alert.first_seen),
                    "last_seen": _time(alert.last_seen), "resolved_at": _time(alert.resolved_at)} for alert in alerts],
    }
