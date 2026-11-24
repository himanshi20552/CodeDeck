"""Read-only Kubernetes health sampling and Metrics Server integration."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

from app.core.config import settings


def _quantity(value: str | None, resource: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([numkKMGTEPi]*)", text)
    if not match:
        return None
    amount = float(match.group(1))
    suffix = match.group(2)
    if resource == "cpu":
        factors = {"n": 1e-6, "u": 1e-3, "m": 1, "": 1000, "k": 1e6, "K": 1e6, "M": 1e9, "G": 1e12}
        return amount * factors.get(suffix, 1000)
    factors = {"": 1, "k": 1000, "K": 1024, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5, "E": 1000**6,
               "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
    return amount * factors.get(suffix, 1)


def _api_error(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    if status:
        return f"Kubernetes API request failed (HTTP {status})"
    return f"Kubernetes API request failed ({type(exc).__name__})"


def _load_client():
    from kubernetes import client, config

    config_file = settings.kubeconfig or ("/etc/rancher/k3s/k3s.yaml" if settings.deployment_target == "remote" else None)
    if config_file:
        config.load_kube_config(config_file=config_file, persist_config=False)
    else:
        config.load_kube_config(persist_config=False)
    return client


def collect_cluster_snapshot(namespace_projects: dict[str, int] | None = None) -> dict:
    """Poll cluster health in a small number of API calls; never writes to Kubernetes."""
    sampled_at = datetime.now(timezone.utc).isoformat()
    try:
        client = _load_client()
        core = client.CoreV1Api()
        apps = client.AppsV1Api()
        autoscaling = client.AutoscalingV1Api()
        custom = client.CustomObjectsApi()
        namespaces = core.list_namespace(_request_timeout=15).items
        node_items = core.list_node(_request_timeout=15).items
        nodes = []
        for node in node_items:
            ready_condition = next((condition for condition in (node.status.conditions or []) if condition.type == "Ready"), None)
            ready = bool(ready_condition and ready_condition.status == "True")
            nodes.append({"name": node.metadata.name, "ready": ready,
                          "reason": getattr(ready_condition, "reason", None) if ready_condition else "Ready condition unavailable"})
        namespace_names = {ns.metadata.name for ns in namespaces if ns.metadata and ns.metadata.name}
        namespace_map = dict(namespace_projects or {})
        for name in namespace_names:
            match = re.search(r"-p(\d+)-", name)
            if match:
                namespace_map.setdefault(name, int(match.group(1)))
            elif namespace_projects is None and name == settings.k8s_namespace:
                namespace_map.setdefault(name, None)
        target_namespaces = set(namespace_map) & namespace_names

        deployment_items = apps.list_deployment_for_all_namespaces(_request_timeout=20).items
        pod_items = core.list_pod_for_all_namespaces(_request_timeout=20).items
        hpa_items = autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces(_request_timeout=20).items
        deployments = []
        pods = []
        hpas = []
        alerts = []
        deployment_selectors = {}
        for item in deployment_items:
            meta, status, spec = item.metadata, item.status, item.spec
            namespace = meta.namespace
            if namespace not in target_namespaces:
                continue
            desired = spec.replicas or 0
            ready = status.ready_replicas or 0
            available = status.available_replicas or 0
            progressing = next((condition for condition in (status.conditions or []) if condition.type == "Progressing"), None)
            reason = getattr(progressing, "reason", None)
            rollout = "healthy" if available >= desired else "unhealthy" if reason == "ProgressDeadlineExceeded" else "progressing"
            deployment = {"project_id": namespace_map.get(namespace), "namespace": namespace, "name": meta.name,
                          "status": rollout, "desired_replicas": desired, "ready_replicas": ready,
                          "available_replicas": available, "updated_replicas": status.updated_replicas or 0,
                          "reason": reason}
            deployments.append(deployment)
            deployment_selectors.setdefault(namespace, []).append((meta.name, spec.selector.match_labels or {}))
            if rollout == "unhealthy":
                alerts.append({"fingerprint": f"rollout:{namespace}:{meta.name}", "project_id": namespace_map.get(namespace),
                               "source": "kubernetes", "alert_type": "unhealthy_rollout", "severity": "critical",
                               "title": "Deployment rollout is unhealthy", "message": f"{namespace}/{meta.name}: {reason or 'progress deadline exceeded'}"})

        restart_threshold = settings.monitoring_restart_alert_threshold
        for item in pod_items:
            meta, status = item.metadata, item.status
            namespace = meta.namespace
            if namespace not in target_namespaces:
                continue
            restarts = 0
            reasons = []
            for container in status.container_statuses or []:
                restarts += container.restart_count or 0
                waiting = getattr(getattr(container, "state", None), "waiting", None)
                if waiting and waiting.reason:
                    reasons.append(waiting.reason)
            scheduling = next((condition for condition in (status.conditions or []) if condition.type == "PodScheduled" and condition.status == "False"), None)
            if scheduling:
                reasons.append(getattr(scheduling, "reason", None) or "FailedScheduling")
            reason = next((value for value in reasons if value in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "FailedScheduling", "Unschedulable"}), None)
            ready = any(condition.type == "Ready" and condition.status == "True" for condition in (status.conditions or []))
            labels = meta.labels or {}
            matched_deployments = [name for name, selector in deployment_selectors.get(namespace, []) if selector and all(labels.get(key) == value for key, value in selector.items())]
            pods.append({"project_id": namespace_map.get(namespace), "namespace": namespace, "name": meta.name,
                         "phase": status.phase, "ready": ready, "reason": reason, "restart_count": restarts,
                         "deployment_names": matched_deployments})
            if reason or restarts >= restart_threshold:
                issue = reason or "Repeated container restarts"
                alerts.append({"fingerprint": f"pod:{namespace}:{meta.name}:{issue}", "project_id": namespace_map.get(namespace),
                               "source": "kubernetes", "alert_type": "pod_failure" if reason else "pod_restarts",
                               "severity": "critical" if reason else "warning", "title": issue,
                               "message": f"{namespace}/{meta.name} · restarts: {restarts}"})

        for item in hpa_items:
            namespace = item.metadata.namespace
            if namespace not in target_namespaces:
                continue
            current_metrics = []
            for metric in (getattr(item.status, "current_metrics", None) or []):
                resource = getattr(metric, "resource", None)
                current = getattr(resource, "current", None) if resource else None
                current_metrics.append({"name": getattr(resource, "name", None),
                                       "average_utilization": getattr(current, "average_utilization", None),
                                       "average_value": getattr(current, "average_value", None)})
            targets = []
            for metric in (getattr(item.spec, "metrics", None) or []):
                resource = getattr(metric, "resource", None)
                target = getattr(resource, "target", None) if resource else None
                targets.append({"name": getattr(resource, "name", None),
                                "average_utilization": getattr(target, "average_utilization", None)})
            labels = getattr(item.metadata, "labels", None) or {}
            try:
                deployment_id = int(labels.get("codedeck.io/deployment-id"))
            except (TypeError, ValueError):
                deployment_id = None
            hpas.append({"project_id": namespace_map.get(namespace), "namespace": namespace, "name": item.metadata.name,
                         "deployment_id": deployment_id,
                         "target_name": item.spec.scale_target_ref.name,
                         "current_replicas": item.status.current_replicas, "desired_replicas": item.status.desired_replicas,
                         "min_replicas": item.spec.min_replicas or 1, "max_replicas": item.spec.max_replicas,
                         "current_metrics": current_metrics, "targets": targets,
                         "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": getattr(c, "message", None)} for c in (item.status.conditions or [])]})

        metrics = {"status": "unavailable", "message": "Metrics Server is unavailable; install/enable Metrics Server to view CPU and memory usage.", "pods": [], "nodes": []}
        try:
            pod_metrics = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods", _request_timeout=15)
            node_metrics = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes", _request_timeout=15)
            pod_usage = []
            for item in pod_metrics.get("items", []):
                ns, name = item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name")
                if ns not in target_namespaces:
                    continue
                usage = {"cpu_millicores": 0.0, "memory_bytes": 0.0}
                for container in item.get("containers", []):
                    values = container.get("usage", {})
                    usage["cpu_millicores"] += _quantity(values.get("cpu"), "cpu") or 0
                    usage["memory_bytes"] += _quantity(values.get("memory"), "memory") or 0
                usage["cpu_millicores"] = round(usage["cpu_millicores"], 2)
                usage["memory_bytes"] = int(usage["memory_bytes"])
                metric_pod = next((pod for pod in pods if pod["namespace"] == ns and pod["name"] == name), None)
                pod_usage.append({"project_id": namespace_map.get(ns), "namespace": ns, "name": name,
                                  "deployment_names": metric_pod.get("deployment_names", []) if metric_pod else [], **usage})
                if usage["cpu_millicores"] >= settings.monitoring_cpu_alert_millicores or usage["memory_bytes"] >= settings.monitoring_memory_alert_bytes:
                    alerts.append({"fingerprint": f"resource:{ns}:{name}", "project_id": namespace_map.get(ns), "source": "kubernetes",
                                   "alert_type": "high_resource_usage", "severity": "warning", "title": "High pod resource usage",
                                   "message": f"{ns}/{name} · CPU {usage['cpu_millicores']}m · memory {usage['memory_bytes']} bytes"})
            node_usage = []
            for item in node_metrics.get("items", []):
                usage = item.get("usage", {})
                node_usage.append({"name": item.get("metadata", {}).get("name"),
                                   "cpu_millicores": _quantity(usage.get("cpu"), "cpu"),
                                   "memory_bytes": _quantity(usage.get("memory"), "memory")})
            metrics = {"status": "available", "message": "Metrics Server data received", "sampled_at": sampled_at,
                       "pods": pod_usage, "nodes": node_usage}
        except Exception as exc:
            metrics["message"] = f"Metrics Server unavailable ({type(exc).__name__}); CPU and memory usage are not reported."

        return {"status": "available", "sampled_at": sampled_at, "source": "kubernetes-api", "namespaces": sorted(target_namespaces),
                "deployments": deployments, "pods": pods, "hpas": hpas, "nodes": nodes, "metrics": metrics, "alerts": alerts}
    except Exception as exc:
        return {"status": "unavailable", "sampled_at": sampled_at, "source": "kubernetes-api",
                "message": _api_error(exc), "namespaces": [], "deployments": [], "pods": [], "hpas": [], "nodes": [],
                "metrics": {"status": "unavailable", "message": "Metrics unavailable because Kubernetes sampling failed.", "pods": [], "nodes": []}, "alerts": []}


class CachedClusterSampler:
    """Rate-limit Kubernetes API polling while worker heartbeats run more often."""
    def __init__(self, interval_seconds: int | None = None):
        self.interval = interval_seconds or settings.monitoring_kubernetes_interval_seconds
        self._lock = threading.Lock()
        self._sampled_at = 0.0
        self._snapshot: dict | None = None

    def sample(self, force: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._snapshot is None or force or now - self._sampled_at >= self.interval:
                self._snapshot = collect_cluster_snapshot()
                self._sampled_at = now
            return dict(self._snapshot)
