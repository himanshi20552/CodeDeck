import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deployments, worker
from app.api.deployments import AutoscalingRequest
from app.api.worker import Completion
from app.core.config import settings
from app.db.database import Base
from app.db.models import AutoscalingEvent, Deployment, Project, WorkerHeartbeat
from app.services import kubernetes
from app.services.monitoring import MonitoringService


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autoflush=False)


class FakeApiException(Exception):
    def __init__(self, status):
        self.status = status


def fake_kubernetes_rest(monkeypatch):
    package = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    client.__path__ = []
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = FakeApiException
    package.client = client
    monkeypatch.setitem(sys.modules, "kubernetes", package)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest)


def default_config(**overrides):
    return {"min_replicas": 1, "max_replicas": 4, "target_cpu_utilization": 65,
            "target_memory_utilization": None,
            "behavior": {"scale_up_stabilization_seconds": 0, "scale_down_stabilization_seconds": 300}, **overrides}


def new_deployment(db, *, deployment_type="remote_staging", target="aws-ec2-k3s", status="SUCCEEDED"):
    project = Project(repository_url="https://github.com/acme/demo", repository_name="demo", status="ready")
    db.add(project); db.commit(); db.refresh(project)
    row = Deployment(project_id=project.id, environment="staging", version="blue", status=status,
                     deployment_type=deployment_type,
                     details={"target": target, "namespace": f"codedeck-p{project.id}-demo", "app_name": "demo"})
    db.add(row); db.commit(); db.refresh(row)
    return project, row


def test_autoscaling_configuration_validation():
    normalized = kubernetes.validate_autoscaling_config(default_config(target_memory_utilization=75))
    assert normalized["target_memory_utilization"] == 75
    for invalid in (default_config(min_replicas=0), default_config(min_replicas=5, max_replicas=4),
                    default_config(target_cpu_utilization=100), default_config(behavior={"scale_up_stabilization_seconds": 4000})):
        with pytest.raises(ValueError):
            kubernetes.validate_autoscaling_config(invalid)


def test_apply_autoscaler_creates_v2_hpa_after_metrics_and_requests_checks(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    container = SimpleNamespace(name="web", resources=SimpleNamespace(requests={"cpu": "100m", "memory": "128Mi"}))
    workload = SimpleNamespace(spec=SimpleNamespace(replicas=2, template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))))
    created = {}
    class Apps:
        def read_namespaced_deployment(self, *args, **kwargs): return workload
    class Core:
        def read_namespaced_resource_quota(self, *args, **kwargs): raise FakeApiException(404)
    class Autoscaling:
        def get_api_resources(self, **kwargs): return object()
        def read_namespaced_horizontal_pod_autoscaler(self, *args, **kwargs): raise FakeApiException(404)
        def create_namespaced_horizontal_pod_autoscaler(self, namespace, manifest, **kwargs): created.update(manifest)
    class Custom:
        def list_namespaced_custom_object(self, *args, **kwargs): return {"items": []}
    fake_client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV2Api=Autoscaling, CustomObjectsApi=Custom)
    monkeypatch.setattr(kubernetes, "prepare_kubeconfig", lambda: None)
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    result = kubernetes.apply_autoscaler("demo-ns", "demo-staging", 7, default_config(target_memory_utilization=80))
    assert result["operation"] == "created" and result["original_replicas"] == 2
    assert created["apiVersion"] == "autoscaling/v2"
    assert created["metadata"]["labels"]["codedeck.io/deployment-id"] == "7"
    assert created["spec"]["metrics"][1]["resource"]["target"]["averageUtilization"] == 80
    assert created["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300


def test_apply_autoscaler_updates_owned_hpa_without_losing_manual_baseline(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    workload = SimpleNamespace(spec=SimpleNamespace(replicas=4, template=SimpleNamespace(spec=SimpleNamespace(containers=[
        SimpleNamespace(name="web", resources=SimpleNamespace(requests={"cpu": "100m"}))]))))
    hpa = SimpleNamespace(metadata=SimpleNamespace(labels={"app.kubernetes.io/managed-by": "codedeck", "codedeck.io/deployment-id": "11"}, annotations={"codedeck.io/original-replicas": "2"}))
    patched = {}
    class Apps:
        def read_namespaced_deployment(self, *args, **kwargs): return workload
    class Core:
        def read_namespaced_resource_quota(self, *args, **kwargs): raise FakeApiException(404)
    class Autoscaling:
        def get_api_resources(self, **kwargs): return object()
        def read_namespaced_horizontal_pod_autoscaler(self, *args, **kwargs): return hpa
        def patch_namespaced_horizontal_pod_autoscaler(self, name, namespace, manifest, **kwargs): patched.update(manifest)
    class Custom:
        def list_namespaced_custom_object(self, *args, **kwargs): return {"items": []}
    fake_client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV2Api=Autoscaling, CustomObjectsApi=Custom)
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    result = kubernetes.apply_autoscaler("demo-ns", "demo-staging", 11, default_config(max_replicas=5), None)
    assert result["operation"] == "updated" and result["original_replicas"] == 2
    assert patched["metadata"]["annotations"]["codedeck.io/original-replicas"] == "2"
    assert patched["spec"]["maxReplicas"] == 5


def test_apply_autoscaler_rejects_missing_metrics_or_requests(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    workload = SimpleNamespace(spec=SimpleNamespace(replicas=1, template=SimpleNamespace(spec=SimpleNamespace(
        containers=[SimpleNamespace(name="web", resources=SimpleNamespace(requests={}))]))))
    class Apps:
        def read_namespaced_deployment(self, *args, **kwargs): return workload
    class Core:
        def read_namespaced_resource_quota(self, *args, **kwargs): raise FakeApiException(404)
    class Autoscaling:
        def get_api_resources(self, **kwargs): return object()
    class CustomUnavailable:
        def list_namespaced_custom_object(self, *args, **kwargs): raise RuntimeError("API missing")
    fake_client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV2Api=Autoscaling, CustomObjectsApi=CustomUnavailable)
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    with pytest.raises(RuntimeError, match="Metrics Server is unavailable"):
        kubernetes.apply_autoscaler("demo-ns", "demo-staging", 8, default_config())
    class CustomAvailable:
        def list_namespaced_custom_object(self, *args, **kwargs): return {"items": []}
    fake_client.CustomObjectsApi = CustomAvailable
    class AutoscalingMissing(Autoscaling):
        def get_api_resources(self, **kwargs): raise RuntimeError("no group")
    fake_client.AutoscalingV2Api = AutoscalingMissing
    with pytest.raises(RuntimeError, match="autoscaling/v2 API is unavailable"):
        kubernetes.apply_autoscaler("demo-ns", "demo-staging", 8, default_config())
    fake_client.AutoscalingV2Api = Autoscaling
    with pytest.raises(ValueError, match="Resource requests are required"):
        kubernetes.apply_autoscaler("demo-ns", "demo-staging", 8, default_config())


def test_apply_autoscaler_respects_namespace_pod_quota(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    container = SimpleNamespace(name="web", resources=SimpleNamespace(requests={"cpu": "100m"}))
    workload = SimpleNamespace(spec=SimpleNamespace(replicas=1, selector=SimpleNamespace(match_labels={"app": "demo"}),
        template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))))
    pods = [SimpleNamespace(metadata=SimpleNamespace(labels={"app": "demo"}), status=SimpleNamespace(phase="Running"))] + [
        SimpleNamespace(metadata=SimpleNamespace(labels={"app": f"dependency-{i}"}), status=SimpleNamespace(phase="Running")) for i in range(3)]
    class Core:
        def read_namespaced_resource_quota(self, *args, **kwargs): return SimpleNamespace(status=SimpleNamespace(hard={"pods": "10"}))
        def list_namespaced_pod(self, *args, **kwargs): return SimpleNamespace(items=pods)
    class Apps:
        def read_namespaced_deployment(self, *args, **kwargs): return workload
    class Autoscaling:
        def get_api_resources(self, **kwargs): return object()
    class Custom:
        def list_namespaced_custom_object(self, *args, **kwargs): return {"items": []}
    fake_client = SimpleNamespace(CoreV1Api=Core, AppsV1Api=Apps, AutoscalingV2Api=Autoscaling, CustomObjectsApi=Custom)
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    with pytest.raises(ValueError, match="pod quota"):
        kubernetes.apply_autoscaler("demo-ns", "demo-staging", 8, default_config(max_replicas=8))


def test_remove_autoscaler_checks_ownership_and_restores_replica_count(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    hpa = SimpleNamespace(metadata=SimpleNamespace(labels={"app.kubernetes.io/managed-by": "codedeck", "codedeck.io/deployment-id": "9"}, annotations={"codedeck.io/original-replicas": "2"}))
    calls = []
    class Autoscaling:
        def read_namespaced_horizontal_pod_autoscaler(self, *args, **kwargs): return hpa
        def delete_namespaced_horizontal_pod_autoscaler(self, *args, **kwargs): calls.append(("delete", args))
    class Apps:
        def patch_namespaced_deployment_scale(self, *args, **kwargs): calls.append(("scale", args[2]))
    fake_client = SimpleNamespace(AutoscalingV2Api=Autoscaling, AppsV1Api=Apps, V1DeleteOptions=lambda: object())
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    result = kubernetes.remove_autoscaler("demo-ns", "demo-staging", 9, None)
    assert result["operation"] == "removed" and result["restored_replicas"] == 2
    assert calls[1][1] == {"spec": {"replicas": 2}}
    hpa.metadata.labels["codedeck.io/deployment-id"] = "other"
    with pytest.raises(RuntimeError, match="not owned"):
        kubernetes.remove_autoscaler("demo-ns", "demo-staging", 9, None)


def test_remove_missing_autoscaler_is_idempotent(monkeypatch):
    fake_kubernetes_rest(monkeypatch)
    class Autoscaling:
        def read_namespaced_horizontal_pod_autoscaler(self, *args, **kwargs): raise FakeApiException(404)
    restored = []
    class Apps:
        def patch_namespaced_deployment_scale(self, *args, **kwargs): restored.append(args[2])
    fake_client = SimpleNamespace(AutoscalingV2Api=Autoscaling, AppsV1Api=Apps)
    monkeypatch.setattr(kubernetes, "_autoscaling_client", lambda: fake_client)
    result = kubernetes.remove_autoscaler("demo-ns", "demo-staging", 12, 2)
    assert result["operation"] == "already-absent" and result["restored_replicas"] == 2
    assert restored == [{"spec": {"replicas": 2}}]


def test_local_configuration_and_disable_record_actual_results(monkeypatch):
    db = Session(); project, source = new_deployment(db, deployment_type="staging", target="local-kind", status="healthy")
    monkeypatch.setattr(settings, "deployment_target", "local")
    monkeypatch.setattr(kubernetes, "apply_autoscaler", lambda *args, **kwargs: {"name": "codedeck-1-autoscaler", "operation": "created", "original_replicas": 2, "config": args[3], "message": "HPA created"})
    result = deployments.configure_autoscaling(project.id, source.id, AutoscalingRequest(**default_config()), db)
    assert result["status"] == "SUCCEEDED"
    db.refresh(source)
    assert source.details["autoscaling"]["original_replicas"] == 2
    assert db.query(AutoscalingEvent).filter_by(deployment_id=source.id, event_type="configured").count() == 1
    monkeypatch.setattr(kubernetes, "remove_autoscaler", lambda *args, **kwargs: {"operation": "removed", "restored_replicas": 2, "message": "HPA removed"})
    assert deployments.disable_autoscaling(project.id, source.id, db)["status"] == "SUCCEEDED"
    db.refresh(source)
    assert "autoscaling" not in source.details
    db.close()


def test_remote_autoscaling_uses_validated_worker_job_and_completion(monkeypatch):
    db = Session(); project, source = new_deployment(db)
    heartbeat = WorkerHeartbeat(worker_id="worker-test", details={"docker": True})
    db.add(heartbeat); db.commit()
    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(settings, "remote_worker_token", "test-token")
    monkeypatch.setattr(settings, "worker_id", "worker-test")
    response = deployments.configure_autoscaling(project.id, source.id, AutoscalingRequest(**default_config()), db)
    assert response["status"] == "QUEUED"
    claimed = worker.claim_job(db)
    assert claimed["job"]["operation"] == "configure_autoscaling"
    job_id = claimed["job"]["id"]
    worker.complete_job(job_id, Completion(status="SUCCEEDED", result={"operation": "created", "name": f"codedeck-{source.id}-autoscaler", "original_replicas": 1, "config": default_config(), "message": "HPA created"}), db)
    db.refresh(source)
    assert source.details["autoscaling"]["operation"] == "created"
    assert project.status == "ready"
    db.close()


def test_cleanup_removes_hpa_kind_without_touching_pvcs(monkeypatch):
    observed = {}
    monkeypatch.setattr(kubernetes, "_run", lambda args, timeout=180: (observed.update({"args": args}) or SimpleNamespace(returncode=0, stdout="removed", stderr="")))
    result = kubernetes.cleanup_namespace_resources("demo-ns")
    assert "horizontalpodautoscalers" in observed["args"][2]
    assert result["persistent_volume_claims_preserved"] is True


def test_replica_change_samples_are_persisted_once_per_observed_change():
    db = Session(); project, source = new_deployment(db)
    service = MonitoringService()
    first = {"status": "available", "sampled_at": "2026-11-23T10:00:00+00:00", "alerts": [], "hpas": [{
        "deployment_id": source.id, "project_id": project.id, "name": f"codedeck-{source.id}-autoscaler",
        "current_replicas": 1, "desired_replicas": 1, "current_metrics": [{"name": "cpu", "average_utilization": 72}],
        "targets": [{"name": "cpu", "average_utilization": 65}], "conditions": []}]}
    service.ingest_kubernetes_snapshot(db, first)
    service.ingest_kubernetes_snapshot(db, first)
    assert db.query(AutoscalingEvent).filter_by(deployment_id=source.id).count() == 1
    scaled = {**first, "sampled_at": "2026-11-23T10:00:30+00:00", "hpas": [{**first["hpas"][0], "current_replicas": 2, "desired_replicas": 3}]}
    service.ingest_kubernetes_snapshot(db, scaled)
    events = db.query(AutoscalingEvent).filter_by(deployment_id=source.id).order_by(AutoscalingEvent.id).all()
    assert [event.event_type for event in events] == ["replica_sample", "replica_change"]
    assert events[-1].current_replicas == 2 and events[-1].desired_replicas == 3
    db.close()


def test_autoscaling_status_endpoint_returns_hpa_metrics_and_persisted_events(monkeypatch):
    db = Session(); project, source = new_deployment(db)
    db.add(AutoscalingEvent(project_id=project.id, deployment_id=source.id, event_type="replica_change",
                            occurred_at=datetime.now(timezone.utc), current_replicas=2, desired_replicas=3,
                            details={"current_metrics": [{"name": "cpu", "average_utilization": 72}]})); db.commit()
    monkeypatch.setattr(settings, "deployment_target", "remote")
    heartbeat = WorkerHeartbeat(worker_id=settings.worker_id, details={"kubernetes_monitoring": {
        "status": "available", "sampled_at": "2026-11-23T10:00:30+00:00", "metrics": {"status": "available", "message": "Metrics available"},
        "hpas": [{"deployment_id": source.id, "name": f"codedeck-{source.id}-autoscaler", "current_replicas": 2, "desired_replicas": 3,
                  "min_replicas": 1, "max_replicas": 4, "current_metrics": [{"name": "cpu", "average_utilization": 72}],
                  "targets": [{"name": "cpu", "average_utilization": 65}], "conditions": []}]}})
    db.add(heartbeat); db.commit()
    result = deployments.get_autoscaling(project.id, source.id, db)
    assert result["hpa"]["desired_replicas"] == 3
    assert result["hpa"]["current_metrics"][0]["average_utilization"] == 72
    assert len(result["events"]) == 1 and result["events"][0]["current_replicas"] == 2
    db.close()
