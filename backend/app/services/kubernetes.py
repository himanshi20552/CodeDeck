import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from collections.abc import Callable

import httpx

from app.core.config import settings
from app.services.deployment_control import DeploymentCancelled
from app.services.observability import measured

logger = logging.getLogger("deploymind")


def _run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if settings.kubeconfig:
        env["KUBECONFIG"] = settings.kubeconfig
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def prepare_kubeconfig() -> str | None:
    """Make kubectl work from inside the backend container against a local kind cluster."""
    cluster = settings.kind_cluster_name
    out = Path(settings.workspace_dir) / "kubeconfig"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Preferred: join kind network and use internal API URL (works reliably in Docker)
    if shutil.which("kind") and Path("/var/run/docker.sock").exists():
        container = os.environ.get("HOSTNAME", "agent-backend-1")
        connect = subprocess.run(
            ["docker", "network", "connect", "kind", container],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if connect.returncode != 0 and "already exists" not in (connect.stderr or "").lower():
            logger.warning("could not connect to kind network: %s", (connect.stderr or "")[:200])

        internal = subprocess.run(
            ["kind", "get", "kubeconfig", "--internal", "--name", cluster],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if internal.returncode == 0 and internal.stdout.strip():
            out.write_text(internal.stdout, encoding="utf-8")
            os.environ["KUBECONFIG"] = str(out)
            settings.kubeconfig = str(out)
            logger.info("kubeconfig prepared (kind internal) at %s", out)
            return str(out)

    # Fallback: rewrite host kubeconfig localhost -> host.docker.internal
    candidates = [Path("/host-kube/config"), Path.home() / ".kube" / "config"]
    if settings.kubeconfig:
        candidates.insert(0, Path(settings.kubeconfig))
    src = next((p for p in candidates if p.is_file()), None)
    if not src:
        return None

    text = src.read_text(encoding="utf-8")
    fixed = text
    for host in ("127.0.0.1", "localhost", "0.0.0.0"):
        fixed = fixed.replace(f"https://{host}:", "https://host.docker.internal:")
    out.write_text(fixed, encoding="utf-8")
    os.environ["KUBECONFIG"] = str(out)
    settings.kubeconfig = str(out)
    logger.info("kubeconfig prepared (host rewrite) at %s", out)
    return str(out)


def cluster_reachable() -> bool:
    prepare_kubeconfig()
    try:
        proc = _run(["kubectl", "cluster-info"], timeout=10)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("kubectl cluster-info timed out after 10 seconds")
        return False



def ensure_namespace(namespace: str) -> None:
    proc = _run(["kubectl", "get", "ns", namespace], timeout=30)
    if proc.returncode == 0:
        return
    create = _run(["kubectl", "create", "namespace", namespace], timeout=30)
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create namespace: {create.stderr or create.stdout}")


def apply_manifests(files: dict[str, str], namespace: str) -> None:
    ensure_namespace(namespace)
    with tempfile.TemporaryDirectory(prefix="deploymind-k8s-") as tmp:
        tmp_path = Path(tmp)
        # The application may connect on boot, so make its ephemeral database
        # available before applying RBAC, application, and public Service.
        order = sorted(
            files.keys(),
            key=lambda n: 0 if "database" in n else 1 if "rbac" in n else 2,
        )
        for name in order:
            path = tmp_path / name
            path.write_text(files[name], encoding="utf-8")
            proc = _run(["kubectl", "apply", "-n", namespace, "-f", str(path)], timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"kubectl apply failed for {name}: {proc.stderr or proc.stdout}")
            logger.info("applied %s", name)


@measured("kubernetes_rollout")
def wait_rollout(
    deployment: str,
    namespace: str,
    timeout_s: int = 300,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """Wait for rollout to complete, aborting early if pods crash-loop."""
    import time

    TERMINAL_STATES = {"CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
    deadline = time.time() + timeout_s
    poll_interval = 3
    started_at = time.monotonic()
    last_progress_at = 0.0

    while time.time() < deadline:
        if cancel_check and cancel_check():
            raise DeploymentCancelled("Staging deployment canceled while waiting for Kubernetes rollout.")
        # Check pod status for early exit on crash
        pod_proc = _run(
            ["kubectl", "get", "pods", "-n", namespace, "-l", f"app={deployment.removesuffix('-staging').removesuffix('-green').removesuffix('-blue')}",
             "-o", "jsonpath={range .items[*]}{.metadata.name}{'|'}{.status.containerStatuses[0].state.waiting.reason}{'|'}{.status.containerStatuses[0].state.terminated.reason}{'\\n'}{end}"],
            timeout=30,
        )
        pod_output = (pod_proc.stdout or "").strip()
        for line in pod_output.splitlines():
            parts = line.split("|")
            pod_name = parts[0].strip() if parts else ""
            waiting_reason = parts[1].strip() if len(parts) > 1 else ""
            terminated_reason = parts[2].strip() if len(parts) > 2 else ""
            crash_reason = waiting_reason or terminated_reason
            if crash_reason in TERMINAL_STATES:
                # Collect logs for actionable error
                log_proc = _run(["kubectl", "logs", "-n", namespace, pod_name, "--tail=60"], timeout=20)
                prev_log_proc = _run(["kubectl", "logs", "-n", namespace, pod_name, "--previous", "--tail=40"], timeout=20)
                logs = (log_proc.stdout or log_proc.stderr or "").strip()
                prev_logs = (prev_log_proc.stdout or prev_log_proc.stderr or "").strip()
                detail = f"Pod {pod_name} is in {crash_reason}.\nLogs:\n{logs}"
                if prev_logs:
                    detail += f"\n\nPrevious container logs:\n{prev_logs}"
                raise RuntimeError(f"Rollout failed (early exit): {detail[-3000:]}")

        # Try the standard rollout status check
        proc = _run(
            ["kubectl", "rollout", "status", f"deployment/{deployment}", "-n", namespace, "--timeout=1s"],
            timeout=30,
        )
        if proc.returncode == 0:
            logger.info("rollout complete for %s", deployment)
            return (proc.stdout or "").strip() or "rollout complete"

        remaining = int(deadline - time.time())
        logger.info("waiting for rollout of %s (%ds remaining)...", deployment, remaining)
        elapsed = time.monotonic() - started_at
        if progress_callback and elapsed - last_progress_at >= 10:
            progress_callback(f"{deployment} is still starting ({int(elapsed)}s elapsed)")
            last_progress_at = elapsed
        # Keep cancellation responsive while waiting for the next rollout poll.
        for _ in range(poll_interval):
            if cancel_check and cancel_check():
                raise DeploymentCancelled("Staging deployment canceled while waiting for Kubernetes rollout.")
            time.sleep(1)

    # Do not start another full-length rollout wait here. The polling deadline
    # has already elapsed, and a second timeout-sized kubectl call made
    # cancellation appear hung for several more minutes.
    proc = _run(
        ["kubectl", "rollout", "status", f"deployment/{deployment}", "-n", namespace, "--timeout=1s"],
        timeout=5,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        pod_proc = _run(
            ["kubectl", "get", "pods", "-n", namespace, "-l", f"app={deployment.removesuffix('-staging').removesuffix('-green').removesuffix('-blue')}", "-o", "wide"],
            timeout=20,
        )
        pods = (pod_proc.stdout or pod_proc.stderr or "").strip()
        pod_names = re.findall(r"^([^\s]+)", pods, flags=re.MULTILINE)
        logs = ""
        if len(pod_names) > 1:
            log_proc = _run(["kubectl", "logs", "-n", namespace, pod_names[1], "--tail=80"], timeout=20)
            logs = (log_proc.stdout or log_proc.stderr or "").strip()
        detail = f"Rollout failed: {out[-1600:]}"
        if pods:
            detail += f"\nPods:\n{pods[-1200:]}"
        if logs:
            detail += f"\nContainer logs:\n{logs[-1800:]}"
        raise RuntimeError(detail)
    logger.info("rollout complete for %s", deployment)
    return out.strip() or "rollout complete"


def get_deployment_status(deployment: str, namespace: str) -> dict:
    proc = _run(
        [
            "kubectl",
            "get",
            "deployment",
            deployment,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.readyReplicas}/{.status.replicas}|{.status.conditions[-1].type}={.status.conditions[-1].status}",
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        return {
            "name": deployment,
            "ready": False,
            "detail": (proc.stderr or proc.stdout or "not found")[:500],
        }
    text = (proc.stdout or "").strip()
    ready_part, _, cond = text.partition("|")
    ready_s, _, desired_s = ready_part.partition("/")
    try:
        ready_n = int(ready_s or 0)
        desired_n = int(desired_s or 0)
    except ValueError:
        ready_n, desired_n = 0, 0
    return {
        "name": deployment,
        "ready": ready_n > 0 and ready_n >= desired_n,
        "ready_replicas": ready_n,
        "replicas": desired_n,
        "condition": cond,
        "detail": text,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_active_service(service: str, namespace: str) -> str:
    """Find the exact existing service name in the namespace (handling -staging or -green suffixes)."""
    for candidate in (service, f"{service}-staging", f"{service}-green", f"{service}-nodeport"):
        proc = _run(["kubectl", "get", "svc", candidate, "-n", namespace, "-o", "name"], timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return candidate
    return service


def get_service_port(service: str, namespace: str) -> int:
    proc = _run(
        [
            "kubectl", "get", "svc", service, "-n", namespace,
            "-o", "jsonpath={.spec.ports[0].port}"
        ], timeout=30
    )
    if proc.returncode == 0 and proc.stdout.strip().isdigit():
        return int(proc.stdout.strip())
    return 80


class PortForwardManager:
    def __init__(self):
        self._processes: dict[str, tuple[int, subprocess.Popen]] = {}

    def get_port(self, service: str, namespace: str) -> int:
        active_service = find_active_service(service, namespace)
        key = f"{namespace}/{active_service}"
        if key in self._processes:
            port, proc = self._processes[key]
            if proc.poll() is None:
                for _ in range(3):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.settimeout(0.5)
                            if s.connect_ex(("127.0.0.1", port)) == 0:
                                return port
                    except Exception:
                        pass
                    time.sleep(0.2)
                try:
                    proc.terminate()
                except Exception:
                    pass

        local_port = _free_port()
        svc_port = get_service_port(active_service, namespace)
        proc = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                "-n",
                namespace,
                f"svc/{active_service}",
                f"{local_port}:{svc_port}",
                "--address=0.0.0.0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **({"KUBECONFIG": settings.kubeconfig} if settings.kubeconfig else {})},
        )
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.3)
                    if s.connect_ex(("127.0.0.1", local_port)) == 0:
                        break
            except Exception:
                pass
            time.sleep(0.2)

        self._processes[key] = (local_port, proc)
        return local_port


port_forward_manager = PortForwardManager()

@measured("smoke_test")
def smoke_test(
    service: str,
    namespace: str,
    paths: list[str] | None = None,
    timeout_s: int = 60,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    paths = paths or ["/docs", "/health", "/api/v1/utils/health-check", "/api/v1", "/", "/openapi.json"]
    local_port = _free_port()
    svc_port = get_service_port(service, namespace)
    pf = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "-n",
            namespace,
            f"svc/{service}",
            f"{local_port}:{svc_port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **({"KUBECONFIG": settings.kubeconfig} if settings.kubeconfig else {})},
        start_new_session=(os.name != "nt"),
    )
    try:
        deadline = time.time() + timeout_s
        started_at = time.monotonic()
        last_progress_at = 0.0
        last_error = "smoke test did not succeed"
        while time.time() < deadline:
            if cancel_check and cancel_check():
                raise DeploymentCancelled("Staging deployment canceled while running smoke tests.")
            if pf.poll() is not None:
                err = pf.stderr.read() if pf.stderr else ""
                raise RuntimeError(f"port-forward exited early: {err}")
            time.sleep(1.5)
            elapsed = time.monotonic() - started_at
            if progress_callback and elapsed - last_progress_at >= 10:
                progress_callback(f"Still checking the staging endpoint ({int(elapsed)}s elapsed)")
                last_progress_at = elapsed
            for path in paths:
                url = f"http://127.0.0.1:{local_port}{path}"
                try:
                    res = httpx.get(url, timeout=3.0, follow_redirects=True)
                    if res.status_code < 500:
                        logger.info("smoke test passed: %s -> %s", url, res.status_code)
                        return {
                            "status": "passed",
                            "url": url,
                            "status_code": res.status_code,
                            "message": f"Smoke test passed ({path} -> {res.status_code})",
                        }
                    last_error = f"{url} returned {res.status_code}"
                except Exception as exc:
                    last_error = str(exc)
        raise RuntimeError(last_error)
    finally:
        if pf.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(pf.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                pf.terminate()
            try:
                pf.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(pf.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    pf.kill()


def patch_manifests_for_local(
    files: dict[str, str],
    image: str,
    app_name: str,
    replicas: int = 1,
    env_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    """Rewrite image and pull policy for kind; inject environment variables; use TCP probes with numeric ports."""
    import yaml
    patched: dict[str, str] = {}
    for name, content in files.items():
        if name.startswith("deployment"):
            docs = list(yaml.safe_load_all(content))
            for doc in docs:
                if not doc or doc.get("kind") != "Deployment":
                    continue
                spec = doc.get("spec", {})
                spec["replicas"] = replicas

                # For staging: remove version from selector so the renamed deployment
                # (-staging suffix) can still match pods without a version label conflict.
                selector = spec.get("selector", {})
                match_labels = selector.get("matchLabels", {})
                match_labels.pop("version", None)

                template = spec.get("template", {})
                template_meta = template.get("metadata", {})
                template_labels = template_meta.get("labels", {})
                template_labels.pop("version", None)

                template_spec = template.get("spec", {})

                # Relax pod-level security context so apps that write files on startup don't crash.
                pod_sec = template_spec.get("securityContext", {})
                pod_sec.pop("readOnlyRootFilesystem", None)
                # Keep runAsNonRoot / runAsUser only if the generated Dockerfile actually uses appuser
                pod_sec["runAsNonRoot"] = True
                pod_sec["runAsUser"] = 1000
                pod_sec["fsGroup"] = 1000
                template_spec["securityContext"] = pod_sec

                containers = template_spec.get("containers", [])
                for container in containers:
                    container["image"] = image
                    container["imagePullPolicy"] = "Never"

                    # Relax container-level security context – don't enforce readOnlyRootFilesystem
                    c_sec = container.get("securityContext", {})
                    c_sec.pop("readOnlyRootFilesystem", None)
                    c_sec["allowPrivilegeEscalation"] = False
                    c_sec["runAsNonRoot"] = True
                    c_sec["runAsUser"] = 1000
                    container["securityContext"] = c_sec

                    # Inject / merge environment variables
                    if env_vars:
                        existing_env = container.get("env") or []
                        env_map = {
                            item["name"]: dict(item)
                            for item in existing_env
                            if isinstance(item, dict) and "name" in item
                        }
                        # A repository manifest may explicitly set Spring's
                        # Mongo URI to localhost. In a Pod, localhost refers
                        # to that Pod, so staging must replace it with the
                        # namespace-local Mongo Service address.
                        mongo_env_keys = {
                            "SPRING_DATA_MONGODB_URI",
                            "SPRING_DATA_MONGODB_HOST",
                            "SPRING_DATA_MONGODB_PORT",
                            "SPRING_DATA_MONGODB_DATABASE",
                            "MONGODB_URI",
                            "MONGO_URI",
                            "MONGO_URL",
                            "MONGO_HOST",
                            "MONGODB_HOST",
                            "MONGO_PORT",
                            "MONGODB_PORT",
                            "MONGO_DATABASE",
                            "MONGODB_DATABASE",
                        }
                        force_mongo_service = "SPRING_DATA_MONGODB_URI" in env_vars
                        for k, v in env_vars.items():
                            if k not in env_map or (force_mongo_service and k in mongo_env_keys):
                                env_map[k] = {"name": k, "value": str(v)}
                        container["env"] = list(env_map.values())

                    # Determine numeric port for TCP probes (named ports are not always resolvable)
                    container_ports = container.get("ports", [])
                    probe_port = 8000  # safe fallback
                    if container_ports:
                        first_port = container_ports[0]
                        if isinstance(first_port, dict) and first_port.get("containerPort"):
                            probe_port = int(first_port["containerPort"])
                        elif isinstance(first_port, int):
                            probe_port = first_port

                    # Convert HTTP probes to TCP for local kind stability
                    # Use the numeric port so k8s can resolve it regardless of port naming.
                    if "readinessProbe" in container and "httpGet" in container["readinessProbe"]:
                        container["readinessProbe"] = {
                            "tcpSocket": {"port": probe_port},
                            "initialDelaySeconds": 10,
                            "periodSeconds": 5,
                            "failureThreshold": 6,
                        }
                    if "livenessProbe" in container and "httpGet" in container["livenessProbe"]:
                        container["livenessProbe"] = {
                            "tcpSocket": {"port": probe_port},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 20,
                            "failureThreshold": 3,
                        }
            patched[name] = yaml.dump_all(docs, default_flow_style=False)
        else:
            patched[name] = content
    return patched


def patch_manifests_for_blue_green(
    files: dict[str, str],
    image: str,
    app_name: str,
    version: str = "green",
    replicas: int = 2,
    env_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    """Patch manifests specifically for blue-green production deployment."""
    import yaml
    patched = patch_manifests_for_local(files, image=image, app_name=app_name, replicas=replicas, env_vars=env_vars)
    result: dict[str, str] = {}
    for name, content in patched.items():
        if name.startswith("deployment") or name.startswith("service"):
            docs = list(yaml.safe_load_all(content))
            for doc in docs:
                if not doc:
                    continue
                kind = doc.get("kind")
                if kind == "Deployment":
                    doc["metadata"]["name"] = f"{app_name}-{version}"
                    template_labels = doc.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
                    template_labels["version"] = version
                    
                    # Also update selector to match the new version
                    selector = doc.get("spec", {}).get("selector", {}).get("matchLabels", {})
                    selector["version"] = version
                elif kind == "Service":
                    selector = doc.get("spec", {}).get("selector", {})
                    if selector:
                        selector["version"] = version
            result[name] = yaml.dump_all(docs, default_flow_style=False)
        else:
            result[name] = content
    return result


def switch_traffic(app_name: str, namespace: str, version: str) -> str:
    """Switch Kubernetes Service selector to target version (blue or green)."""
    proc = _run(
        [
            "kubectl",
            "patch",
            "service",
            app_name,
            "-n",
            namespace,
            "-p",
            f'{{"spec":{{"selector":{{"version":"{version}"}}}}}}',
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to switch traffic to {version}: {proc.stderr or proc.stdout}")
    logger.info("switched service %s traffic to %s", app_name, version)
    return f"Traffic switched to {version}"


def get_active_version(app_name: str, namespace: str) -> str:
    """Get the version ('blue' or 'green') currently pointed to by the Service selector."""
    proc = _run(
        [
            "kubectl",
            "get",
            "service",
            app_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={{.spec.selector.version}}",
        ],
        timeout=30,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return "blue"


def rollback(app_name: str, namespace: str) -> str:
    """Rollback traffic to blue version."""
    return switch_traffic(app_name, namespace, "blue")

