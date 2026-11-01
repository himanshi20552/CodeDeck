import logging
import os
import re
import signal
import subprocess
import threading
import time
import shutil
from collections import deque
from pathlib import Path
from collections.abc import Callable

from app.core.config import settings
from app.core.deployment_context import deployment_target
from app.services.observability import measured
from app.services.deployment_control import DeploymentCancelled

logger = logging.getLogger("deploymind")


def _docker_cli() -> str:
    """Resolve one Docker CLI binary for the worker's entire build invocation."""
    configured = os.environ.get("CODEDECK_DOCKER_CLI")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        raise RuntimeError("CODEDECK_DOCKER_CLI does not point to a Docker CLI executable")
    # systemd often provides a shorter PATH than an interactive login shell.
    # Prefer Ubuntu's packaged CLI when present, then resolve the active PATH.
    if os.name != "nt" and Path("/usr/bin/docker").is_file():
        return "/usr/bin/docker"
    executable = shutil.which("docker")
    if executable:
        return executable
    raise RuntimeError("Docker CLI not found; install Docker or set CODEDECK_DOCKER_CLI")


def _docker_env() -> dict[str, str]:
    """Build a safe Docker CLI environment for the selected deployment target."""
    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "1"
    env["BUILDKIT_PROGRESS"] = "plain"
    if deployment_target() in {"remote", "k3s"} and not env.get("DOCKER_CONFIG"):
        # Avoid Docker falling back to a user's home directory hidden by
        # systemd ProtectHome. The directory contains no credentials by default.
        config_dir = Path(settings.workspace_dir).resolve().parent / "docker-config"
        try:
            config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                config_dir.chmod(0o700)
        except OSError as exc:
            raise RuntimeError("Could not create the private remote Docker config directory") from exc
        env["DOCKER_CONFIG"] = str(config_dir)
    return env


def _log_docker_cli_diagnostics(executable: str, env: dict[str, str]) -> None:
    """Record CLI/plugin/context identity without reading or printing auth config."""
    checks = (
        ("client-version", [executable, "--version"]),
        ("buildx-version", [executable, "buildx", "version"]),
        ("context", [executable, "context", "show"]),
    )
    for name, command in checks:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=10, env=env)
            detail = (proc.stdout or proc.stderr or "").strip().replace("\n", " ")[-500:]
            logger.info("Docker CLI diagnostic %s (exit=%s): %s", name, proc.returncode, detail)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Docker CLI diagnostic %s failed (%s)", name, type(exc).__name__)


_IMAGE_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _validate_image_reference(image: str) -> None:
    """Validate a Docker name:tag reference before invoking an external CLI."""
    if not isinstance(image, str) or not image or len(image) > 255 or "@" in image:
        raise ValueError("Invalid Docker image reference")
    name, separator, tag = image.rpartition(":")
    if not separator:
        name = image
    # A colon before a slash is part of a registry port, not the tag.
    if separator and "/" in tag:
        name, tag, separator = image, "", ""
    if separator and not _IMAGE_TAG.fullmatch(tag):
        raise ValueError("Invalid Docker image tag")
    if not name or name.startswith("/") or name.endswith("/"):
        raise ValueError("Invalid Docker image name")
    parts = name.split("/")
    for index, part in enumerate(parts):
        if index == 0 and (":" in part or part == "localhost" or ("." in part and part not in {".", ".."})):
            host, colon, port = part.partition(":")
            labels = host.split(".")
            valid_host = all(
                label and label[0].isalnum() and label[-1].isalnum()
                and all(char.isalnum() or char == "-" for char in label)
                for label in labels
            )
            if not valid_host or (colon and (not port.isdigit() or not 1 <= int(port) <= 65535)):
                raise ValueError("Invalid Docker registry in image reference")
            continue
        if not _IMAGE_COMPONENT.fullmatch(part):
            raise ValueError("Invalid Docker image name")

# Patterns that indicate a Dockerfile requires BuildKit (--mount) or complex
# multi-stage tooling (bun, uv sync --frozen, etc.) that won't work with the
# legacy builder inside the CodeDeck container.
_BUILDKIT_REQUIRED_PATTERNS = re.compile(
    r"--mount=type="
    r"|RUN\s+--mount"
    r"|FROM\s+oven/bun"
    r"|FROM\s+ghcr\.io/astral-sh/uv"
    r"|uv\s+sync\s+--frozen"
    r"|bun\s+install"
    r"|bun\s+run\s+build",
    re.IGNORECASE,
)
_PREBUILT_JAVA_ARTIFACT_PATTERN = re.compile(
    r"\b(?:COPY|ADD)\s+(?:--\S+\s+)*(?:target/|build/libs/)",
    re.IGNORECASE,
)
_LEGACY_POETRY_PATTERN = re.compile(
    r"pip\s+install\s+poetry(?:==1\.[0-7](?:\.|\b))?|poetry\s+install\s+--no-dev",
    re.IGNORECASE,
)


def has_complex_dockerfile(path: Path) -> bool:
    """Return True if the Dockerfile uses BuildKit-only features or multi-stage
    tooling (bun, uv --frozen mounts) that CodeDeck cannot build as-is."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(_BUILDKIT_REQUIRED_PATTERNS.search(text))
    except OSError:
        return False


def requires_prebuilt_java_artifact(path: Path) -> bool:
    """Return True when a Dockerfile expects CI to have built a JAR first."""
    if not path.is_file():
        return False
    try:
        return bool(_PREBUILT_JAVA_ARTIFACT_PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return False


def requires_staging_dockerfile_replacement(path: Path) -> bool:
    """Detect known non-reproducible source-build Dockerfile patterns."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(_LEGACY_POETRY_PATTERN.search(text))
    except OSError:
        return False


def run_cmd(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: int = 600,
    cancel_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "docker":
        args = [_docker_cli(), *args[1:]]
    logger.info("running: %s", " ".join(args))
    env = _docker_env()
    if not cancel_check:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)

    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        start_new_session=(os.name != "nt"),
    )
    deadline = time.monotonic() + timeout
    while True:
        if cancel_check():
            _stop_subprocess(process)
            raise DeploymentCancelled(f"Staging deployment canceled while running {' '.join(args[:2])}.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_subprocess(process)
            raise subprocess.TimeoutExpired(args, timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(1, remaining))
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


def _stop_subprocess(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)


@measured("docker_build")
def build_image(
    context_dir: Path,
    dockerfile: Path,
    image: str,
    timeout: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    effective_timeout = timeout if timeout is not None else settings.docker_build_timeout

    if not context_dir.is_dir():
        raise RuntimeError(f"Build context directory not found: {context_dir}")
    if not dockerfile.is_file():
        raise RuntimeError(f"Dockerfile not found: {dockerfile}")
    if not dockerfile.resolve().is_relative_to(context_dir.resolve()):
        raise ValueError("Dockerfile must be inside the build context")
    _validate_image_reference(image)

    logger.info(
        "Docker build starting for image '%s' (context='%s', dockerfile='%s', timeout=%ds)",
        image,
        context_dir,
        dockerfile,
        effective_timeout,
    )

    executable = _docker_cli()
    args = [executable, "build", "-t", image, "-f", str(dockerfile), str(context_dir)]
    logger.info("Docker executable: %s", executable)
    logger.info("Docker build arguments: %r", args)
    env = _docker_env()
    logger.info("Docker environment: HOME=%s DOCKER_CONFIG=%s DOCKER_HOST_set=%s PATH=%s",
                env.get("HOME", ""), env.get("DOCKER_CONFIG", ""),
                bool(env.get("DOCKER_HOST")), env.get("PATH", ""))
    _log_docker_cli_diagnostics(executable, env)
    try:
        # Drain Docker output while the build runs. Waiting until after poll()
        # to read PIPEs can fill the OS pipe buffer and block docker build
        # forever, which also prevents staging cancellation from completing.
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=(os.name != "nt"),
        )
        output_tail: deque[str] = deque(maxlen=300)
        latest_output = {"line": ""}

        def drain_output() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                output_tail.append(line)
                latest_output["line"] = line.strip()
                logger.info("docker build: %s", line.rstrip())

        reader = threading.Thread(target=drain_output, name="docker-build-output", daemon=True)
        reader.start()

        def stop_build() -> None:
            if process.poll() is not None:
                return
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=5)

        deadline = time.monotonic() + effective_timeout
        last_reported_line = ""
        last_reported_at = 0.0
        while process.poll() is None:
            if cancel_check and cancel_check():
                stop_build()
                reader.join(timeout=2)
                raise DeploymentCancelled("Staging deployment canceled while building the container image.")
            if time.monotonic() >= deadline:
                stop_build()
                reader.join(timeout=2)
                raise subprocess.TimeoutExpired(args, effective_timeout)
            current_line = latest_output["line"]
            now = time.monotonic()
            if progress_callback and current_line and current_line != last_reported_line and now - last_reported_at >= 4:
                progress_callback(current_line[:180])
                last_reported_line = current_line
                last_reported_at = now
            time.sleep(0.5)
        reader.join(timeout=5)
        proc = subprocess.CompletedProcess(args, process.returncode, "".join(output_tail), "")
    except FileNotFoundError as exc:
        msg = "Docker CLI not found. Install Docker Desktop and ensure 'docker' is in PATH."
        logger.error(msg)
        raise RuntimeError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"Docker build timed out after {effective_timeout} seconds."
        logger.error(msg)
        raise RuntimeError(msg) from exc
    if proc.returncode != 0:
        # Combine stdout and stderr — Docker sends build output to stdout, errors to stderr
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        detail = combined[-5000:]
        msg = f"Docker build failed:\n{detail}"
        logger.error("Docker build failed for %s:\n%s", image, detail)
        raise RuntimeError(msg)

    inspect_args = [executable, "image", "inspect", image]
    try:
        inspected = subprocess.run(inspect_args, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Docker build completed but image verification failed: {type(exc).__name__}") from exc
    if inspected.returncode != 0:
        detail = ((inspected.stderr or inspected.stdout or "image inspect failed").strip())[-1000:]
        raise RuntimeError(f"Docker build completed but image '{image}' is unavailable: {detail}")

    logger.info("Docker image built successfully: %s", image)
    return image


def kind_load_image(
    image: str,
    cluster: str = "deploymind",
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    if deployment_target() in {"remote", "k3s"}:
        _k3s_load_image(image, cancel_check=cancel_check)
        return
    logger.info("Loading docker image '%s' into kind cluster '%s'...", image, cluster)

    # 1. Try standard kind load
    try:
        proc = run_cmd(
            ["kind", "load", "docker-image", image, "--name", cluster],
            timeout=300,
            cancel_check=cancel_check,
        )
        if proc.returncode == 0:
            logger.info("Image successfully loaded into kind cluster '%s': %s", cluster, image)
            return
        logger.warning(
            "kind load failed: %s. Falling back to direct containerd import...",
            (proc.stderr or proc.stdout or "").strip()[:500],
        )
    except DeploymentCancelled:
        raise
    except Exception as exc:
        logger.warning("kind load invocation error (%s). Falling back to direct containerd import...", exc)

    # 2. Robust fallback: pipe docker save directly into kind node's containerd
    node_name = f"{cluster}-control-plane"
    try:
        if cancel_check and cancel_check():
            raise DeploymentCancelled("Staging deployment canceled before importing the image into kind.")
        save_proc = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name != "nt"),
        )
        import_proc = subprocess.Popen(
            ["docker", "exec", "-i", node_name, "ctr", "--namespace=k8s.io", "images", "import", "-"],
            stdin=save_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
        if save_proc.stdout:
            save_proc.stdout.close()
        deadline = time.monotonic() + 300
        while True:
            if cancel_check and cancel_check():
                _stop_subprocess(import_proc)
                _stop_subprocess(save_proc)
                raise DeploymentCancelled("Staging deployment canceled while importing the image into kind.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_subprocess(import_proc)
                _stop_subprocess(save_proc)
                raise subprocess.TimeoutExpired(["docker", "exec", node_name], 300)
            try:
                out, err = import_proc.communicate(timeout=min(1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        save_proc.wait(timeout=5)
        if import_proc.returncode == 0:
            logger.info("Image successfully loaded into kind via containerd import: %s", image)
            return
        raise RuntimeError(f"containerd import failed: {(err or out).strip()[:1000]}")
    except DeploymentCancelled:
        raise
    except Exception as exc:
        msg = f"Failed to load image into kind node '{node_name}': {exc}"
        logger.error(msg)
        raise RuntimeError(msg) from exc


def _k3s_load_image(image: str, cancel_check: Callable[[], bool] | None = None) -> None:
    """Transfer a Docker-built image into K3s' separate containerd image store."""
    import tempfile

    if not shutil_which("k3s"):
        raise RuntimeError("K3s CLI is required on the remote deployment worker")
    inspect = run_cmd([_docker_cli(), "image", "inspect", image], timeout=30, cancel_check=cancel_check)
    if inspect.returncode:
        raise RuntimeError(f"Docker image is not available for K3s import: {image}")
    with tempfile.TemporaryDirectory(prefix="codedeck-image-") as directory:
        archive = Path(directory) / "image.tar"
        try:
            proc = run_cmd(["docker", "save", "-o", str(archive), image], timeout=300, cancel_check=cancel_check)
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout or "docker save failed")[-2000:])
            ctr = (["sudo", "-n", "k3s", "ctr"] if settings.k3s_ctr_use_sudo else ["k3s", "ctr"])
            proc = run_cmd([*ctr, "--namespace", "k8s.io", "images", "import", str(archive)], timeout=300, cancel_check=cancel_check)
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout or "K3s image import failed")[-2000:])
            listed = run_cmd([*ctr, "--namespace", "k8s.io", "images", "list", "-q"], timeout=30, cancel_check=cancel_check)
            if listed.returncode:
                raise RuntimeError((listed.stderr or listed.stdout or "Could not verify K3s image import")[-2000:])
            candidates = {image, f"docker.io/{image}"}
            if "/" not in image.split(":", 1)[0]:
                candidates.add(f"docker.io/library/{image}")
            if not candidates.intersection(set((listed.stdout or "").splitlines())):
                raise RuntimeError(f"K3s import completed but image '{image}' is not visible in containerd namespace k8s.io")
        except FileNotFoundError as exc:
            raise RuntimeError("Docker and K3s CLIs must be installed on the worker") from exc
    logger.info("Docker image imported into K3s containerd: %s", image)


def shutil_which(command: str) -> bool:
    from shutil import which
    return which(command) is not None


