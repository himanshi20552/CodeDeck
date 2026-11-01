import subprocess

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.deployment_context import deployment_target, use_deployment_target
from app.services import docker, kubernetes
from app.worker.runner import _phase
from app.api.worker import _authenticate


def test_remote_manifest_guard_rejects_host_mount_and_privileged_pod():
    with pytest.raises(ValueError, match="hostPath"):
        kubernetes._validate_remote_manifests({
            "deployment.yaml": """apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      volumes:
        - name: host
          hostPath:
            path: /var/run/docker.sock
"""
        })
    with pytest.raises(ValueError, match="privileged"):
        kubernetes._validate_remote_manifests({
            "deployment.yaml": """apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          securityContext:
            privileged: true
"""
        })


def test_remote_manifest_guard_rejects_cluster_scoped_kinds():
    with pytest.raises(ValueError, match="unsupported"):
        kubernetes._validate_remote_manifests({
            "role.yaml": "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\n"
        })


def test_worker_phase_uses_reported_stage_not_percentage():
    assert _phase("Building container image: step 2") == "BUILDING"
    assert _phase("Applying Kubernetes manifests") == "DEPLOYING"
    assert _phase("Waiting for application rollout") == "VERIFYING"
    assert _phase("Preparing repository source") == "RUNNING"


def test_worker_authentication_requires_the_shared_bearer_secret(monkeypatch):
    monkeypatch.setattr(settings, "remote_worker_token", "test-worker-secret")
    _authenticate("Bearer test-worker-secret")
    with pytest.raises(HTTPException) as error:
        _authenticate("Bearer wrong-secret")
    assert error.value.status_code == 401


def test_deployment_target_override_is_scoped_to_the_operation(monkeypatch):
    monkeypatch.setattr(settings, "deployment_target", "remote")
    assert deployment_target() == "remote"
    with use_deployment_target("local"):
        assert deployment_target() == "local"
    assert deployment_target() == "remote"


def test_remote_image_import_explicitly_uses_k3s_containerd(monkeypatch, tmp_path):
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        if args[:3] == ["docker", "save", "-o"]:
            open(args[3], "wb").write(b"tar")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(docker, "shutil_which", lambda name: name == "k3s")
    monkeypatch.setattr(docker, "run_cmd", fake_run)
    docker._k3s_load_image("deploymind/demo:d9")

    assert commands[0][:3] == ["docker", "save", "-o"]
    assert commands[0][-1] == "deploymind/demo:d9"
    assert commands[1] == ["k3s", "ctr", "--namespace", "k8s.io", "images", "import", commands[1][-1]]
