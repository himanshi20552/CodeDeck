import logging
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel
from app.services.dependency_sanitizer import sanitize_requirements_file
from app.services.repository_env import repository_child_environment

logger = logging.getLogger("deploymind")

ALLOWED_COMMANDS = [
    re.compile(r"^pytest(\s+.+)?$"),
    re.compile(r"^python -m pytest(\s+.+)?$"),
    re.compile(r"^python -m unittest(\s+.+)?$"),
    re.compile(r"^npm test$"),
    re.compile(r"^npm run test$"),
    re.compile(r"^./mvnw test$"),
    re.compile(r"^mvn test$"),
    re.compile(r"^./gradlew test$"),
    re.compile(r"^gradle test$"),
]

DEFAULT_TIMEOUT = 180


class TestResult(BaseModel):
    command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    status: str = "skipped"
    message: str = ""
    skipped: bool = False


def _is_allowed(command: str) -> bool:
    cmd = " ".join(command.split())
    return any(pat.match(cmd) for pat in ALLOWED_COMMANDS)


def _child_env() -> dict[str, str]:
    return repository_child_environment()


def _run(command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )


def _prepare(repo_path: Path, analysis: dict, timeout: int) -> str | None:
    language = (analysis.get("language") or "").lower()
    try:
        if language == "python":
            req = repo_path / "requirements.txt"
            if req.exists():
                sanitize_requirements_file(repo_path)
                logger.info("tests started: installing Python requirements")
                proc = _run(
                    "pip install -r requirements.txt pytest -q",
                    repo_path,
                    min(timeout, 240),
                )
                if proc.returncode != 0:
                    return (proc.stderr or proc.stdout or "pip install failed")[:2000]
        elif language == "javascript":
            if (repo_path / "package.json").exists():
                logger.info("tests started: npm install")
                install_cmd = (
                    "npm ci --ignore-scripts"
                    if (repo_path / "package-lock.json").exists()
                    else "npm install --ignore-scripts"
                )
                proc = _run(install_cmd, repo_path, min(timeout, 240))
                if proc.returncode != 0:
                    return (proc.stderr or proc.stdout or "npm install failed")[:2000]
    except subprocess.TimeoutExpired:
        return "Dependency install timed out"
    return None


def run_tests(repo_path: str | Path, analysis: dict, timeout: int = DEFAULT_TIMEOUT) -> TestResult:
    root = Path(repo_path)
    command = (analysis.get("test_command") or "").strip() or None

    if not command:
        return TestResult(status="skipped", skipped=True, message="No test command detected")

    if not _is_allowed(command):
        return TestResult(
            command=command,
            status="failed",
            message=f"Test command not allowed: {command}",
        )

    logger.info("tests started: %s", command)
    prepare_error = _prepare(root, analysis, timeout)
    if prepare_error:
        logger.info("tests completed: dependency install failed")
        return TestResult(
            command=command,
            exit_code=1,
            stderr=prepare_error,
            status="failed",
            message="Dependency install failed before tests",
        )

    try:
        proc = _run(command, root, timeout)
    except subprocess.TimeoutExpired:
        logger.info("tests completed: timed out")
        return TestResult(
            command=command,
            status="failed",
            message=f"Tests timed out after {timeout}s",
        )

    stdout = (proc.stdout or "")[-4000:]
    stderr = (proc.stderr or "")[-4000:]
    ok = proc.returncode == 0
    message = "Tests passed" if ok else f"Tests failed (exit {proc.returncode})"

    match = re.search(r"(\d+)\s+passed", stdout + stderr)
    if match and ok:
        message = f"{match.group(1)} tests passed"
    failed = re.search(r"(\d+)\s+failed", stdout + stderr)
    if failed and not ok:
        message = f"{failed.group(1)} tests failed"

    logger.info("tests completed: %s", message)
    return TestResult(
        command=command,
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        status="passed" if ok else "failed",
        message=message,
    )
