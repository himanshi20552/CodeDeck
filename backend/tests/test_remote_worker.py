import subprocess
import io
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.deployment_context import deployment_target, use_deployment_target
from app.services import docker, kubernetes
from app.worker import runner
from app.worker.runner import _phase
from app.api import worker as worker_api
from app.api.worker import _authenticate
from app.db.models import Deployment, Project


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
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[1:3] == ["save", "-o"]:
            open(args[3], "wb").write(b"tar")
        if args[-3:] == ["images", "list", "-q"]:
            return subprocess.CompletedProcess(args, 0, "docker.io/deploymind/demo:d9\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(docker, "shutil_which", lambda name: name == "k3s")
    monkeypatch.setattr(docker, "_docker_cli", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker, "run_cmd", fake_run)
    docker._k3s_load_image("deploymind/demo:d9")

    assert commands[0][1:3] == ["image", "inspect"]
    assert commands[1][1:3] == ["save", "-o"]
    assert commands[0][-1] == "deploymind/demo:d9"
    assert commands[2] == ["k3s", "ctr", "--namespace", "k8s.io", "images", "import", commands[2][-1]]
    assert commands[3][-3:] == ["images", "list", "-q"]


def test_docker_build_uses_resolved_cli_and_inspects_built_image(monkeypatch, tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    launched = []
    inspected = []

    class FakeProcess:
        stdout = io.StringIO("Successfully built test image\n")
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(args, **kwargs):
        launched.append((args, kwargs))
        return FakeProcess()

    def fake_run(args, **kwargs):
        if args[1:3] == ["image", "inspect"]:
            inspected.append(args)
        return subprocess.CompletedProcess(args, 0, "[]", "")

    monkeypatch.setattr(docker, "_docker_cli", lambda: "/usr/bin/docker")
    monkeypatch.setattr(settings, "deployment_target", "local")
    monkeypatch.setattr(docker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(docker.subprocess, "run", fake_run)
    assert docker.build_image(context, dockerfile, "codedeck-test:v1") == "codedeck-test:v1"
    assert launched[0][0] == ["/usr/bin/docker", "build", "-t", "codedeck-test:v1", "-f", str(dockerfile), str(context)]
    assert launched[0][1].get("shell", False) is False
    assert inspected == [["/usr/bin/docker", "image", "inspect", "codedeck-test:v1"]]


def test_remote_docker_environment_avoids_protected_home(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspaces"))
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    env = docker._docker_env()
    config_dir = tmp_path / "docker-config"
    assert env["DOCKER_CONFIG"] == str(config_dir)
    assert config_dir.is_dir()
    assert not (config_dir / "config.json").exists()


def test_remote_kubeconfig_must_exist_and_be_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deployment_target", "remote")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    configured = tmp_path / "config"
    monkeypatch.setattr(settings, "kubeconfig", str(configured))
    with pytest.raises(RuntimeError, match="not found"):
        kubernetes.prepare_kubeconfig()
    configured.write_text("apiVersion: v1\n", encoding="utf-8")
    assert kubernetes.prepare_kubeconfig() == str(configured)


@pytest.mark.parametrize("image", ["", "UPPER/name:tag", "name:bad/tag", "name:tag with space", "../escape:tag"])
def test_docker_image_reference_validation_rejects_invalid_names(image):
    with pytest.raises(ValueError):
        docker._validate_image_reference(image)


@pytest.mark.parametrize("image", ["deploymind/demo:d9", "localhost:5000/demo:v1", "demo"])
def test_docker_image_reference_validation_accepts_valid_names(image):
    docker._validate_image_reference(image)


def test_worker_forces_remote_deployment_target(monkeypatch):
    import app.agents.deployment_agent as deployment_agent

    monkeypatch.setattr(settings, "deployment_target", "local")
    seen = []

    class FakeResult:
        def model_dump(self, **kwargs):
            return {"message": "ok"}

    def fake_deploy(**kwargs):
        seen.append(deployment_target())
        return FakeResult()

    class FakeClient:
        _state_lock = threading.Lock()
        active_job_id = None

        def post(self, path, payload):
            return {"ok": True}

    monkeypatch.setattr(deployment_agent, "deploy_staging", fake_deploy)
    runner._execute(FakeClient(), {
        "id": 21, "project_id": 4, "repository_url": "https://example.com/repo.git",
        "repository_name": "repo", "namespace": "codedeck-test",
    })
    assert seen == ["remote"]
    assert deployment_target() == "local"


def test_worker_client_retries_transient_503(monkeypatch):
    client = runner.WorkerClient.__new__(runner.WorkerClient)
    responses = [
        __import__("httpx").Response(503, request=__import__("httpx").Request("POST", "https://example.test")),
        __import__("httpx").Response(200, json={"ok": True}, request=__import__("httpx").Request("POST", "https://example.test")),
    ]
    calls = []

    def fake_post(path, json):
        calls.append(path)
        return responses.pop(0)

    client.client = SimpleNamespace(post=fake_post)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner.random, "random", lambda: 0.0)
    assert client.post("/api/worker/heartbeat", {}) == {"ok": True}
    assert calls == ["/api/worker/heartbeat"] * 2


def test_stale_worker_heartbeat_fails_and_releases_active_job(monkeypatch):
    now = datetime.now(timezone.utc)
    heartbeat = SimpleNamespace(
        details={"active_job_id": 42}, last_seen=now - timedelta(minutes=5)
    )
    deployment = SimpleNamespace(
        status="BUILDING", details={"worker_payload": {"secret": "x"}},
        project_id=7, deployment_type="remote_staging",
    )
    project = SimpleNamespace(status="deploying")

    class FakeDB:
        def get(self, model, key):
            return deployment if model is Deployment else project

    monkeypatch.setattr(worker_api, "upsert_step", lambda *args, **kwargs: None)
    worker_api._fail_stale_worker_job(FakeDB(), heartbeat)
    assert deployment.status == "FAILED"
    assert "worker_payload" not in deployment.details
    assert project.status == "staging_failed"
    assert "active_job_id" not in heartbeat.details
