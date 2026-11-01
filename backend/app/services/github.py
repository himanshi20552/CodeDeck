import logging
import os
import re
import shutil
import stat
import subprocess
import base64
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import settings
from app.services.dependency_sanitizer import sanitize_requirements_file

logger = logging.getLogger("deploymind")

GITHUB_URL_RE = re.compile(
    r"^https?://(www\.)?github\.com/[\w.-]+/[\w.-]+/?$",
    re.IGNORECASE,
)


def validate_github_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if not GITHUB_URL_RE.match(url):
        raise ValueError("Invalid GitHub repository URL")
    host_and_path = url.split("://", 1)[1]
    if host_and_path.lower().startswith("www.github.com/"):
        host_and_path = host_and_path[len("www."):]
    return "https://" + host_and_path


def repo_name_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[-1]


def _clone_url(url: str) -> str:
    return url + ".git"


def _handle_remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def cleanup_repository(path: Path) -> None:
    path = Path(path)
    if path.exists():
        try:
            shutil.rmtree(path, onerror=_handle_remove_readonly)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)


def clone_repository(url: str, dest: Path) -> Path:
    url = validate_github_url(url)
    dest = dest.resolve()
    workspace = Path(settings.workspace_dir).resolve()
    if not dest.is_relative_to(workspace):
        raise ValueError("Clone destination is outside workspace")

    cleanup_repository(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if settings.github_token:
            credential = base64.b64encode(f"x-access-token:{settings.github_token}".encode()).decode("ascii")
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", _clone_url(url), str(dest)],
            capture_output=True,
            text=True,
            timeout=settings.repository_clone_timeout,
            env=env,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "Git clone failed").strip()
            if settings.github_token:
                detail = detail.replace(settings.github_token, "[REDACTED]")
            cleanup_repository(dest)
            raise RuntimeError(f"Failed to clone repository: {detail[-2000:]}")
        for req_path in dest.rglob("requirements.txt"):
            if req_path.is_file():
                try:
                    removed = sanitize_requirements_file(req_path.parent)
                    if removed:
                        logger.info("Sanitized requirements.txt at %s by removing: %s", req_path, ", ".join(removed))
                except Exception as e:
                    logger.warning("Failed to sanitize requirements.txt at %s: %s", req_path, e)
    except subprocess.TimeoutExpired as exc:
        cleanup_repository(dest)
        raise RuntimeError(f"Repository clone exceeded the {settings.repository_clone_timeout}s timeout") from exc
    except Exception:
        cleanup_repository(dest)
        raise

    logger.info("repository cloned to %s", dest)
    return dest
