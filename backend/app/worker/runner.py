"""Poll the CodeDeck control plane and execute bounded staging jobs on EC2."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import random
import shutil
import subprocess
import threading
import time
from threading import Lock

import httpx

from app.core.config import settings
from app.core.deployment_context import use_deployment_target

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [worker] %(message)s")
logger = logging.getLogger("codedeck.worker")


class WorkerClient:
    def __init__(self) -> None:
        self.base_url = settings.remote_worker_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {settings.remote_worker_token}"},
            timeout=httpx.Timeout(20.0),
        )
        self._state_lock = Lock()
        self.active_job_id: int | None = None

    def post(self, path: str, payload: dict) -> dict:
        for attempt in range(5):
            try:
                response = self.client.post(path, json=payload)
                if response.status_code < 500 and response.status_code != 429:
                    response.raise_for_status()
                    return response.json()
                if attempt == 4:
                    response.raise_for_status()
                request_id = response.headers.get("x-request-id") or response.headers.get("x-correlation-id")
                logger.warning(
                    "control-plane POST %s returned HTTP %s (request_id=%s); retry %s/4",
                    path, response.status_code, request_id or "unavailable", attempt + 1,
                )
            except httpx.HTTPStatusError:
                raise
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise
                logger.warning(
                    "control-plane POST %s transport failure (%s); retry %s/4",
                    path, type(exc).__name__, attempt + 1,
                )
            time.sleep(min(30.0, (2 ** attempt) + random.random()))
        raise RuntimeError("Control-plane request retries exhausted")

    def health_details(self) -> dict:
        disk = shutil.disk_usage(settings.workspace_dir)
        details = {"docker": False, "docker_buildx": False, "k3s": False, "disk_free_bytes": disk.free}
        try:
            details["memory_available_bytes"] = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            details["memory_available_bytes"] = None
        try:
            from app.services.docker import _docker_cli, _docker_env

            docker_cli = _docker_cli()
            details["docker_cli"] = docker_cli
            with use_deployment_target("remote"):
                docker_env = _docker_env()
            docker_commands = (
                ([docker_cli, "info"], "docker"),
                ([docker_cli, "buildx", "version"], "docker_buildx"),
            )
            for command, key in docker_commands:
                try:
                    proc = subprocess.run(command, capture_output=True, text=True, timeout=8, env=docker_env)
                    details[key] = proc.returncode == 0
                    if key == "docker_buildx" and proc.returncode == 0:
                        details["docker_buildx_version"] = (proc.stdout or proc.stderr).strip()[:200]
                except Exception:
                    details[key] = False
        except Exception as exc:
            details["docker_cli_error"] = type(exc).__name__
        for command, key in ((["k3s", "kubectl", "get", "nodes", "--no-headers"], "k3s"),):
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=8)
                details[key] = proc.returncode == 0
                if key == "k3s":
                    details["node_status"] = proc.stdout.strip()[-500:]
            except Exception:
                details[key] = False
        return details

    def heartbeat(self) -> None:
        with self._state_lock:
            active_job_id = self.active_job_id
        details = self.health_details()
        details["active_job_id"] = active_job_id
        self.post("/api/worker/heartbeat", {"worker_id": settings.worker_id, "details": details})


def _phase(message: str) -> str:
    text = message.lower()
    if "build" in text or "image" in text:
        return "BUILDING"
    if "rollout" in text or "smoke" in text or "healthy" in text or "wait" in text:
        return "VERIFYING"
    if "appl" in text or "service" in text or "namespace" in text:
        return "DEPLOYING"
    return "RUNNING"


def _execute(client: WorkerClient, job: dict) -> None:
    job_id = int(job["id"])
    with client._state_lock:
        client.active_job_id = job_id
    logger.info("claimed deployment job %s", job_id)
    def report(message: str) -> None:
        try:
            client.post(
                f"/api/worker/jobs/{job_id}/progress",
                {"status": _phase(message), "message": message[:1000]},
            )
        except Exception as exc:
            logger.warning("progress update for job %s failed (%s)", job_id, type(exc).__name__)

    payload = job
    execution_succeeded = False
    try:
        if payload.get("operation") == "cleanup":
            import re
            from app.services import kubernetes as k8s_svc

            namespace = str(payload.get("namespace") or "")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
                raise ValueError("Invalid deployment namespace")
            report("Deleting deployment namespace")
            proc = k8s_svc._run(["kubectl", "delete", "namespace", namespace, "--wait=false", "--ignore-not-found=true"], timeout=60)
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout or "Namespace cleanup failed")[-2000:])
            execution_succeeded = True
            client.post(
                f"/api/worker/jobs/{job_id}/complete",
                {"status": "SUCCEEDED", "result": {"message": f"Namespace {namespace} cleanup requested", "namespace": namespace, "source_deployment_id": payload.get("source_deployment_id")}},
            )
            return

        from app.agents.deployment_agent import deploy_staging
        with use_deployment_target("remote"):
            result = deploy_staging(
                project_id=int(payload["project_id"]),
                repository_url=payload["repository_url"],
                repository_name=payload["repository_name"],
                analysis=payload.get("analysis") or {},
                dockerfile="",
                k8s_files=payload.get("k8s_files") or {},
                progress=report,
                namespace=payload["namespace"],
                image_tag=f"d{job_id}",
            )
        execution_succeeded = True
        client.post(
            f"/api/worker/jobs/{job_id}/complete",
            {"status": "SUCCEEDED", "result": result.model_dump(mode="json")},
        )
        logger.info("deployment job %s succeeded", job_id)
    except Exception as exc:
        if execution_succeeded:
            logger.exception("job %s executed successfully but its final status could not be confirmed by the backend", job_id)
            return
        message = str(exc).replace(settings.github_token, "[REDACTED]") if settings.github_token else str(exc)
        logger.exception("deployment job %s failed", job_id)
        try:
            client.post(
                f"/api/worker/jobs/{job_id}/complete",
                {"status": "FAILED", "failure_reason": message[-4000:], "result": {}},
            )
        except Exception:
            logger.exception("could not persist failure for job %s", job_id)
    finally:
        with client._state_lock:
            client.active_job_id = None


def main() -> None:
    if not settings.remote_worker_url or not settings.remote_worker_token:
        raise SystemExit("REMOTE_WORKER_URL and REMOTE_WORKER_TOKEN must be configured for the worker")
    client = WorkerClient()
    Path(settings.workspace_dir).mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    # Establish the heartbeat row before claiming work. The claim endpoint uses
    # it to associate an in-flight deployment with this worker for recovery.
    client.heartbeat()

    def pulse() -> None:
        while not stop.is_set():
            try:
                client.heartbeat()
            except Exception as exc:
                logger.warning("heartbeat failed: %s", type(exc).__name__)
            stop.wait(settings.worker_heartbeat_interval)

    thread = threading.Thread(target=pulse, name="worker-heartbeat", daemon=True)
    thread.start()
    logger.info("worker %s polling %s", settings.worker_id, client.base_url)
    try:
        while True:
            try:
                response = client.post("/api/worker/jobs/claim", {})
                job = response.get("job")
                if job:
                    _execute(client, job)
                else:
                    time.sleep(settings.worker_poll_interval)
            except Exception as exc:
                logger.warning("poll failed: %s", type(exc).__name__)
                time.sleep(settings.worker_poll_interval)
    except KeyboardInterrupt:
        logger.info("stopping worker")
    finally:
        stop.set()
        thread.join(timeout=2)
        client.client.close()


if __name__ == "__main__":
    main()
