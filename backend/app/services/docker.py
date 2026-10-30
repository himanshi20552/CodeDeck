import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from collections.abc import Callable

from app.core.config import settings
from app.services.observability import measured
from app.services.deployment_control import DeploymentCancelled

logger = logging.getLogger("deploymind")

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
    logger.info("running: %s", " ".join(args))
    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "1"
    env["BUILDKIT_PROGRESS"] = "plain"
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

    logger.info(
        "Docker build starting for image '%s' (context='%s', dockerfile='%s', timeout=%ds)",
        image,
        context_dir,
        dockerfile,
        effective_timeout,
    )

    args = ["docker", "build", "-t", image, "-f", str(dockerfile), str(context_dir)]
    try:
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "1"
        env["BUILDKIT_PROGRESS"] = "plain"
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

    logger.info("Docker image built successfully: %s", image)
    return image


def kind_load_image(
    image: str,
    cluster: str = "deploymind",
    cancel_check: Callable[[], bool] | None = None,
) -> None:
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


