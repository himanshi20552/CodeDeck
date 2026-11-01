import logging
import os
import re
import shutil
import httpx
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

from app.agents.kubernetes_agent import (
    generate_database_manifest,
    generate_dependency_manifest,
    sanitize_name,
    staging_namespace,
)
from app.core.config import settings
from app.core.deployment_context import deployment_target
from app.services import docker as docker_svc
from app.services import kubernetes as k8s_svc
from app.services.dependency_sanitizer import choose_python_base_image, sanitize_requirements_file
from app.services.docker import (
    has_complex_dockerfile,
    requires_prebuilt_java_artifact,
    requires_staging_dockerfile_replacement,
)
from app.services.github import cleanup_repository, clone_repository
from app.services.deployment_control import DeploymentCancelled

logger = logging.getLogger("deploymind")


_UVICORN_ARRAY_ENTRYPOINT = re.compile(
    r'((?:"|\')uvicorn(?:"|\')\s*,\s*(?:"|\'))([^"\']+:[^"\']+)((?:"|\'))',
    re.IGNORECASE,
)
_UVICORN_SHELL_ENTRYPOINT = re.compile(
    r'(\buvicorn\s+)([A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)


def _refresh_fastapi_entrypoint(app_dir: Path, analysis: dict) -> dict:
    """Refresh a FastAPI target from the deployment copy before building.

    Analysis may have been saved before a repository changed, and a number of
    full-stack projects keep main.py as a launcher rather than ASGI module.
    """
    refreshed = dict(analysis)
    if (refreshed.get("framework") or "").lower() != "fastapi":
        return refreshed

    from app.agents.repository_agent import _find_fastapi_entrypoint

    entrypoint = _find_fastapi_entrypoint(app_dir)
    if entrypoint:
        refreshed["entrypoint"] = entrypoint
    return refreshed


def _refresh_runtime_analysis(app_dir: Path, analysis: dict) -> dict:
    """Refresh mutable runtime signals from the deployment copy."""
    refreshed = _refresh_fastapi_entrypoint(app_dir, analysis)
    try:
        from app.agents.repository_agent import analyze_repository

        current = analyze_repository(app_dir)
        if current.database:
            refreshed["database"] = current.database
        if current.staging_dependencies:
            refreshed["staging_dependencies"] = current.staging_dependencies
        if current.framework in {"Flask", "Django"} and current.entrypoint:
            refreshed["entrypoint"] = current.entrypoint
    except Exception:
        logger.warning("Could not refresh runtime analysis for %s", app_dir, exc_info=True)
    return refreshed


def _align_fastapi_dockerfile_entrypoint(dockerfile_content: str, entrypoint: str | None) -> str:
    """Use the analyzed ASGI target when a Dockerfile starts uvicorn directly."""
    if not entrypoint or ":" not in entrypoint:
        return dockerfile_content

    def replace_array(match: re.Match[str]) -> str:
        return f"{match.group(1)}{entrypoint}{match.group(3)}"

    def replace_shell(match: re.Match[str]) -> str:
        return f"{match.group(1)}{entrypoint}"

    updated = _UVICORN_ARRAY_ENTRYPOINT.sub(replace_array, dockerfile_content)
    return _UVICORN_SHELL_ENTRYPOINT.sub(replace_shell, updated)


def _needs_fastapi_entrypoint_alignment(dockerfile_content: str, entrypoint: str | None) -> bool:
    return _align_fastapi_dockerfile_entrypoint(dockerfile_content, entrypoint) != dockerfile_content


def _allocate_node_port(app_name: str, base: int = 30100, spread: int = 2600) -> int:
    """Map an app name deterministically to a NodePort in the 30000–32767 range.

    Using hash(name) % spread + base keeps ports stable across redeployments
    for the same app while staying within Kubernetes' allowed NodePort range.
    """
    return base + (hash(app_name) % spread)


def _nodeport_service_manifest(
    app_name: str, container_port: int, node_port: int, namespace: str
) -> str:
    """Generate a Kubernetes NodePort Service YAML that exposes the app on the
    host so users can open http://localhost:<node_port> directly in a browser."""
    return f"""apiVersion: v1
kind: Service
metadata:
  name: {app_name}-nodeport
  namespace: {namespace}
  labels:
    app: {app_name}
    managed-by: codedeck
spec:
  type: NodePort
  selector:
    app: {app_name}
  ports:
    - name: http
      protocol: TCP
      port: {container_port}
      targetPort: {container_port}
      nodePort: {node_port}
"""


def _detect_application_port(analysis: dict, dockerfile: Path, manifests: dict[str, str]) -> int:
    """Resolve the container port from analysis, Dockerfile, or generated Service manifests."""
    candidates: list[int] = []
    try:
        if analysis.get("port"):
            candidates.append(int(analysis["port"]))
    except (TypeError, ValueError):
        pass
    try:
        for match in re.finditer(r"^\s*EXPOSE\s+(\d+)", dockerfile.read_text(encoding="utf-8", errors="ignore"), re.MULTILINE | re.IGNORECASE):
            candidates.append(int(match.group(1)))
    except OSError:
        pass
    try:
        import yaml
        for content in manifests.values():
            for document in yaml.safe_load_all(content):
                if not isinstance(document, dict) or document.get("kind") != "Service":
                    continue
                for service_port in document.get("spec", {}).get("ports", []):
                    target = service_port.get("targetPort")
                    if isinstance(target, int):
                        candidates.append(target)
    except Exception:
        pass
    port = next((value for value in candidates if 1 <= value <= 65535), None)
    if port is None:
        raise RuntimeError("Application port was not detected; specify it in repository analysis or a Kubernetes Service targetPort")
    return port


def _collect_runtime_env(
    app_name: str,
    app_dir: Path,
    port: int,
    database: str | None = None,
    staging_dependencies: list[str] | None = None,
) -> dict[str, str]:
    """Gather environment variables from .env files and provide standard defaults
    so apps requiring settings (like FastAPI templates) start cleanly."""
    env_vars: dict[str, str] = {}
    candidates = [
        app_dir / ".env",
        app_dir / ".env.example",
        app_dir / ".env.sample",
        app_dir.parent / ".env",
        app_dir.parent / ".env.example",
        app_dir / "backend" / ".env",
        app_dir / "backend" / ".env.example",
    ]
    for p in candidates:
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in env_vars:
                            env_vars[k] = v
            except Exception:
                pass

    standard_defaults = {
        "ENVIRONMENT": "local",
        "PROJECT_NAME": app_name,
        "SECRET_KEY": "staging-secret-key-super-secure-change-in-prod-12345678",
        "FIRST_SUPERUSER": "admin@example.com",
        "FIRST_SUPERUSER_PASSWORD": "supersecretpassword123",
        "USERS_OPEN_REGISTRATION": "True",
        "EMAILS_ENABLED": "False",
        "DOMAIN": "localhost",
        "FRONTEND_HOST": "http://localhost",
        "BACKEND_CORS_ORIGINS": '["http://localhost", "http://localhost:3000", "http://localhost:8000"]',
        # Use SQLite as a safe DB fallback so repos that configure a missing Postgres
        # don't immediately crash on startup in the staging environment.
        "POSTGRES_SERVER": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "supersecretpassword123",
        "POSTGRES_DB": "app",
        "DATABASE_URL": "sqlite:///./staging.db",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///./staging.db",
        "PORT": str(port),
    }
    merged = {**standard_defaults, **env_vars}
    # Staging owns an ephemeral in-cluster database. Override developer .env
    # endpoints so the deployed pod never attempts to reach localhost or a
    # private production database.
    if database in {"postgresql", "postgis"}:
        service = f"{app_name}-db"
        url = f"postgresql://deploymind:deploymind@{service}:5432/app"
        merged.update({
            "DATABASE_URL": url,
            "SQLALCHEMY_DATABASE_URI": url,
            "POSTGRES_SERVER": service,
            "POSTGRES_PORT": "5432",
            "POSTGRES_USER": "deploymind",
            "POSTGRES_PASSWORD": "deploymind",
            "POSTGRES_DB": "app",
            "SPRING_DATASOURCE_URL": f"jdbc:postgresql://{service}:5432/app",
            "SPRING_DATASOURCE_USERNAME": "deploymind",
            "SPRING_DATASOURCE_PASSWORD": "deploymind",
        })
    elif database == "mysql":
        service = f"{app_name}-db"
        url = f"mysql+pymysql://deploymind:deploymind@{service}:3306/app"
        merged.update({
            "DATABASE_URL": url,
            "SQLALCHEMY_DATABASE_URI": url,
            "MYSQL_HOST": service,
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "app",
            "MYSQL_USER": "deploymind",
            "MYSQL_PASSWORD": "deploymind",
            "SPRING_DATASOURCE_URL": f"jdbc:mysql://{service}:3306/app?createDatabaseIfNotExist=true&useSSL=false&allowPublicKeyRetrieval=true",
            "SPRING_DATASOURCE_USERNAME": "deploymind",
            "SPRING_DATASOURCE_PASSWORD": "deploymind",
        })
    elif database == "mongodb":
        service = f"{app_name}-db"
        existing_uri = next(
            (
                merged[key]
                for key in (
                    "SPRING_DATA_MONGODB_URI",
                    "MONGODB_URI",
                    "MONGO_URI",
                    "MONGO_URL",
                )
                if merged.get(key, "").strip()
            ),
            "",
        )
        configured_database = ""
        if not existing_uri:
            # Preserve the repository's configured database name when its
            # Mongo URI lives in Spring's application.properties/yml instead
            # of an env file. Only the database name is reused; the local
            # staging URI always points at the in-cluster service.
            for config_file in sorted(app_dir.rglob("application.*")):
                if config_file.suffix.lower() not in {".properties", ".yml", ".yaml"}:
                    continue
                try:
                    config_text = config_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                flat_database = re.search(
                    r"(?im)^\s*spring\.data\.mongodb\.database\s*[:=]\s*[\"']?([A-Za-z0-9_-]+)",
                    config_text,
                )
                if flat_database:
                    configured_database = flat_database.group(1)
                config_lines = config_text.splitlines()
                for index, line in enumerate(config_lines):
                    section = re.match(r"^(\s*)mongodb\s*:\s*(?:#.*)?$", line, re.IGNORECASE)
                    if not section:
                        continue
                    section_indent = len(section.group(1))
                    for child_line in config_lines[index + 1:]:
                        if child_line.strip() and len(child_line) - len(child_line.lstrip()) <= section_indent:
                            break
                        yaml_database = re.match(
                            r"^\s+database\s*:\s*[\"']?([A-Za-z0-9_-]+)", child_line, re.IGNORECASE
                        )
                        if yaml_database:
                            configured_database = yaml_database.group(1)
                            break
                uri_match = re.search(r"mongodb(?:\+srv)?://[^\s\"'<>]+", config_text, re.IGNORECASE)
                if uri_match:
                    existing_uri = uri_match.group(0).rstrip(",;})]")
                    break
        database_name = ""
        if existing_uri:
            try:
                database_name = unquote(urlsplit(existing_uri).path.lstrip("/").split("/", 1)[0])
            except ValueError:
                database_name = ""
        database_name = (
            database_name
            or merged.get("SPRING_DATA_MONGODB_DATABASE", "").strip()
            or merged.get("MONGO_DATABASE", "").strip()
            or merged.get("MONGODB_DATABASE", "").strip()
            or configured_database
            or "app"
        )
        database_name = database_name or "app"
        mongo_uri = f"mongodb://{service}:27017/{database_name}"
        merged.update({
            # Spring Boot's URI takes precedence over localhost-based
            # application.properties settings in the staged pod.
            "SPRING_DATA_MONGODB_URI": mongo_uri,
            "SPRING_DATA_MONGODB_HOST": service,
            "SPRING_DATA_MONGODB_PORT": "27017",
            "SPRING_DATA_MONGODB_DATABASE": database_name,
            # Common names used by non-Spring Java and other app frameworks.
            "MONGODB_URI": mongo_uri,
            "MONGO_URI": mongo_uri,
            "MONGO_URL": mongo_uri,
            "MONGO_HOST": service,
            "MONGODB_HOST": service,
            "MONGO_PORT": "27017",
            "MONGODB_PORT": "27017",
            "MONGO_DATABASE": database_name,
            "MONGODB_DATABASE": database_name,
        })
    dependencies = set(staging_dependencies or [])
    if "redis" in dependencies:
        merged.update({"REDIS_HOST": f"{app_name}-redis", "REDIS_PORT": "6379"})
    if "kafka" in dependencies:
        merged["KAFKA_BOOTSTRAP_SERVERS"] = f"{app_name}-kafka:9092"
    # Replace placeholder passwords
    if "DATABASE_URL" in merged and "changethis" in merged["DATABASE_URL"]:
        merged["DATABASE_URL"] = merged["DATABASE_URL"].replace("changethis", "supersecretpassword123")
    if merged.get("FIRST_SUPERUSER_PASSWORD") == "changethis":
        merged["FIRST_SUPERUSER_PASSWORD"] = "supersecretpassword123"
    # If env-file provided a non-sqlite DB URL that still points to localhost/changethis,
    # fall back to SQLite so the app starts cleanly in a kubernetes pod without a real DB.
    db_url = merged.get("DATABASE_URL", "")
    if db_url and any(host in db_url for host in ("localhost", "127.0.0.1", "0.0.0.0")) and "sqlite" not in db_url:
        merged["DATABASE_URL"] = "sqlite:///./staging.db"
        merged["SQLALCHEMY_DATABASE_URI"] = "sqlite:///./staging.db"
    return merged


def _prepare_deployment_source(project_id: int, repository_url: str, dest: Path) -> Path:
    cleanup_repository(dest)
    cached_src = Path(settings.workspace_dir) / f"project_{project_id}"
    if cached_src.is_dir():
        try:
            shutil.copytree(cached_src, dest)
            logger.info("Reused local repository cache from %s for project %d deployment", cached_src, project_id)
        except Exception as exc:
            logger.warning("Failed to copy cached repository (%s); re-cloning...", exc)
            cleanup_repository(dest)
            clone_repository(repository_url, dest)
    else:
        clone_repository(repository_url, dest)
    # Never let Git credential metadata enter a Docker build context.
    for root, directories, files in os.walk(dest):
        if ".git" in directories:
            shutil.rmtree(Path(root) / ".git", ignore_errors=True)
            directories.remove(".git")
        if ".git" in files:
            (Path(root) / ".git").unlink(missing_ok=True)
    return dest


class StagingDeployResult(BaseModel):
    status: str
    environment: str = "staging"
    version: str = "blue"
    image: str = ""
    namespace: str = ""
    app_name: str = ""
    application_port: int = 0
    rollout: str = ""
    smoke: dict = Field(default_factory=dict)
    deployment_status: dict = Field(default_factory=dict)
    message: str = ""


def _determine_build_context(
    repo_path: Path,
    app_dir: Path,
    dockerfile_path: Path,
    source_subdir: str | None,
) -> Path:
    """Determine whether docker build context should be repo root or app_dir.

    If the Dockerfile explicitly references paths like 'backend/' or '../',
    or if app_dir is repo_path, context is repo_path.
    Otherwise, if the Dockerfile is in app_dir and uses relative paths (e.g. 'COPY requirements.txt'),
    context must be app_dir so files are placed directly at the container root.
    """
    if app_dir == repo_path or not source_subdir:
        return repo_path

    if dockerfile_path.is_file():
        content = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        if f"{source_subdir}/" in content or f"./{source_subdir}/" in content or "../" in content:
            return repo_path

    return app_dir


def _write_build_context(
    repo_path: Path,
    dockerfile_content: str,
    source_subdir: str | None,
) -> tuple[Path, Path]:
    app_dir = repo_path / source_subdir if source_subdir else repo_path
    if not app_dir.is_dir():
        app_dir = repo_path

    existing_dockerfile = app_dir / "Dockerfile"
    if not existing_dockerfile.exists():
        existing_dockerfile = repo_path / "Dockerfile"

    dockerfile_path = app_dir / "Dockerfile"
    if dockerfile_content.strip():
        dockerfile_path.write_text(dockerfile_content, encoding="utf-8")
    elif existing_dockerfile.exists():
        dockerfile_path = existing_dockerfile
    else:
        raise RuntimeError("No Dockerfile available for build")

    context_dir = _determine_build_context(repo_path, app_dir, dockerfile_path, source_subdir)
    return context_dir, dockerfile_path


def _python_runtime_command(analysis: dict, entrypoint: str, port: int) -> str:
    framework = (analysis.get("framework") or "").lower()
    if framework == "flask":
        return f'CMD ["gunicorn", "-b", "0.0.0.0:{port}", "{entrypoint or "app:app"}"]'
    if framework == "django":
        return f'CMD ["gunicorn", "-b", "0.0.0.0:{port}", "{entrypoint or "config.wsgi:application"}"]'
    return f'CMD ["uvicorn", "{entrypoint}", "--host", "0.0.0.0", "--port", "{port}"]'


# Optional monitoring/telemetry packages that are commonly imported but not
# always listed in requirements.txt. We detect their imports at staging time
# and inject a fallback pip install so the container doesn't crash on startup.
_OPTIONAL_PACKAGE_IMPORTS: dict[str, str] = {
    "sentry_sdk": "sentry-sdk",
    "datadog": "datadog",
    "ddtrace": "ddtrace",
    "newrelic": "newrelic",
    "prometheus_client": "prometheus-client",
    "opentelemetry": "opentelemetry-sdk",
    "elastic_apm": "elastic-apm",
    "scout_apm": "scout-apm",
    "rollbar": "rollbar",
    "bugsnag": "bugsnag",
    "logfire": "logfire",
}


def _scan_optional_pip_packages(app_dir: Path) -> list[str]:
    """Scan Python source files and return pip package names for optional
    imports (monitoring, telemetry, etc.) found in the source."""
    found: list[str] = []
    try:
        for py_file in app_dir.rglob("*.py"):
            try:
                text = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for import_name, pip_name in _OPTIONAL_PACKAGE_IMPORTS.items():
                if pip_name not in found and import_name in text:
                    found.append(pip_name)
    except Exception:
        pass
    return found


def _patch_dockerfile_with_optional_deps(content: str, packages: list[str]) -> str:
    """Install detected runtime imports before the image drops root privileges.

    These packages are only optional in the sense that many apps do not import
    them. Once source code imports one unconditionally (for example,
    ``import sentry_sdk``), it is a required runtime dependency. Do not hide a
    failed install: a successful image build must not produce a container that
    immediately crashes with ModuleNotFoundError.
    """
    if not packages:
        return content
    pkg_str = " ".join(packages)
    inject = f"RUN pip install --no-cache-dir {pkg_str}\n"

    # A typical Dockerfile switches to a non-root runtime user near its end.
    # Install before that switch so pip has permission to write to site-packages.
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().upper().startswith("USER "):
            lines.insert(index, inject)
            return "".join(lines)

    # Otherwise install after dependency manifests have been copied/installed,
    # but before application source or startup instructions.
    for marker in ("COPY . .", "COPY . /app", "COPY ./ /app", "CMD [", "ENTRYPOINT [", "CMD ", "ENTRYPOINT "):
        idx = content.find(marker)
        if idx != -1:
            line_start = content.rfind("\n", 0, idx) + 1
            return content[:line_start] + inject + content[line_start:]
    return content + "\n" + inject


def _reliable_staging_dockerfile(app_dir: Path, analysis: dict) -> str:
    port = int(analysis.get("port") or 8000)

    entry = analysis.get("entrypoint") or "app.main:app"
    pm = (analysis.get("package_manager") or "").lower()
    lang = (analysis.get("language") or "").lower()
    python_base = choose_python_base_image(app_dir)
    runtime_command = _python_runtime_command(analysis, entry, port)
    runtime_dependency = "RUN pip install --no-cache-dir gunicorn\n" if analysis.get("framework") in {"Flask", "Django"} else ""

    existing_dockerfile = app_dir / "Dockerfile"
    # Only reuse the existing Dockerfile if it's simple enough to build;
    # complex Dockerfiles (bun, uv --mount, multi-stage CI) need replacement.
    if (
        existing_dockerfile.exists()
        and not has_complex_dockerfile(existing_dockerfile)
        and not requires_staging_dockerfile_replacement(existing_dockerfile)
    ):
        content = existing_dockerfile.read_text(encoding="utf-8", errors="ignore")
        # Scan for optional monitoring deps that may not be in requirements.txt
        # and inject a fallback pip install to prevent startup crashes.
        optional_pkgs = _scan_optional_pip_packages(app_dir)
        return _patch_dockerfile_with_optional_deps(content, optional_pkgs)

    if (app_dir / "requirements.txt").exists():
        return f"""FROM {python_base}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || pip install --no-cache-dir -r requirements.txt --ignore-requires-python
RUN pip install --no-cache-dir aiosqlite 2>/dev/null || true
{runtime_dependency}COPY . .
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
{runtime_command}
"""

    if (app_dir / "uv.lock").exists() or pm == "uv":
        # Install with pip (no uv mount syntax) so BuildKit is not required.
        # We copy the full source first so editable/src-layout packages resolve.
        return f"""FROM {python_base}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml* uv.lock* README* ./
COPY . .
RUN pip install --no-cache-dir --upgrade pip && \\
    pip install --no-cache-dir . 2>/dev/null || \\
    pip install --no-cache-dir fastapi uvicorn sqlalchemy aiosqlite pydantic pydantic-settings
{runtime_dependency}RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
{runtime_command}
"""

    if (app_dir / "poetry.lock").exists() or pm == "poetry":
        return f"""FROM {python_base}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "poetry>=1.8,<2.0"
COPY pyproject.toml* poetry.lock* ./
RUN poetry config virtualenvs.create false && (poetry install --no-interaction --no-ansi --no-root --only main || poetry install --no-interaction --no-ansi --no-root)
COPY . .
{runtime_dependency}RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
{runtime_command}
"""

    if (app_dir / "pyproject.toml").exists():
        return f"""FROM {python_base}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN pip install --no-cache-dir . || pip install --no-cache-dir fastapi uvicorn aiosqlite
COPY . .
{runtime_dependency}RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
{runtime_command}
"""

    # For Python projects without requirements.txt / lockfiles (e.g. testdrivenio/fastapi-react)
    if lang == "python" or analysis.get("framework") in {"FastAPI", "Flask", "Django"}:
        extra_pkgs: list[str] = []
        for py_file in app_dir.rglob("*.py"):
            try:
                code_text = py_file.read_text(encoding="utf-8", errors="ignore")
                for pkg_candidate in ("sqlalchemy", "pydantic", "httpx", "requests", "jinja2", "redis"):
                    if pkg_candidate in code_text and pkg_candidate not in extra_pkgs:
                        extra_pkgs.append(pkg_candidate)
            except Exception:
                pass

        pkgs_str = " ".join(["fastapi", "uvicorn", "aiosqlite"] + extra_pkgs)
        if analysis.get("framework") == "Flask":
            pkgs_str = "flask gunicorn aiosqlite " + " ".join(extra_pkgs)
        elif analysis.get("framework") == "Django":
            pkgs_str = "django gunicorn aiosqlite " + " ".join(extra_pkgs)

        return f"""FROM {python_base}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libsqlite3-dev && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir {pkgs_str}
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE {port}
{runtime_command}
"""

    # Delegate to docker_agent for Node, Java, or other frameworks
    from app.agents.docker_agent import generate_dockerfile
    res = generate_dockerfile(app_dir, {**analysis, "has_dockerfile": False})
    if res.dockerfile:
        return res.dockerfile

    raise RuntimeError(
        "Unable to generate a Dockerfile. "
        "No existing Dockerfile or supported project configuration found."
    )


def _adjust_python_base_image(dockerfile_content: str, app_dir: Path) -> str:
    python_base = choose_python_base_image(app_dir)
    if python_base == "python:3.11-slim":
        return dockerfile_content
    return dockerfile_content.replace("FROM python:3.11-slim", f"FROM {python_base}", 1)



def deploy_staging(
    project_id: int,
    repository_url: str,
    repository_name: str,
    analysis: dict,
    dockerfile: str,
    k8s_files: dict[str, str],
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    namespace: str | None = None,
    image_tag: str | None = None,
) -> StagingDeployResult:
    app_name = sanitize_name(repository_name or analysis.get("repository_name") or "app")
    namespace = namespace or staging_namespace(settings.k8s_namespace, project_id, "legacy")
    cluster = settings.kind_cluster_name
    image = f"deploymind/{app_name}:{image_tag or 'staging'}"
    dest = Path(settings.workspace_dir) / f"project_{project_id}_deploy"

    logger.info("deployment started: staging %s", app_name)

    def report(message: str) -> None:
        if cancel_check and cancel_check():
            raise DeploymentCancelled("Staging deployment canceled by user.")
        logger.info("staging %s: %s", app_name, message)
        if progress:
            progress(message)

    report("Checking Kubernetes cluster")
    k8s_svc.prepare_kubeconfig()
    if not k8s_svc.cluster_reachable():
        raise RuntimeError(
            f"Kubernetes cluster not reachable. Create kind cluster first: "
            f"kind create cluster --name {cluster}"
        )

    try:
        report("Preparing repository source")
        _prepare_deployment_source(project_id, repository_url, dest)
        app_dir = dest / analysis.get("source_subdir", "") if analysis.get("source_subdir") else dest
        if not app_dir.is_dir():
            app_dir = dest
        sanitize_requirements_file(app_dir)
        analysis = _refresh_runtime_analysis(app_dir, analysis)
        existing_dockerfile = app_dir / "Dockerfile"
        # Bypass the repo's own Dockerfile if it uses BuildKit-only features
        # (e.g., RUN --mount, oven/bun, uv sync --frozen) that represent a
        # full CI/CD pipeline rather than a simple container build. We generate
        # a simpler, self-contained Dockerfile for the backend service only.
        requires_source_build = requires_prebuilt_java_artifact(existing_dockerfile)
        requires_replacement = requires_staging_dockerfile_replacement(existing_dockerfile)
        if existing_dockerfile.exists() and not has_complex_dockerfile(existing_dockerfile) and not requires_source_build and not requires_replacement:
            existing_content = existing_dockerfile.read_text(encoding="utf-8", errors="ignore")
            # Always inject optional monitoring deps to prevent startup crashes
            # (e.g. sentry_sdk imported in main.py but not in requirements.txt).
            optional_pkgs = _scan_optional_pip_packages(app_dir)
            existing_content = _patch_dockerfile_with_optional_deps(existing_content, optional_pkgs)
            if (analysis.get("framework") or "").lower() == "fastapi" and _needs_fastapi_entrypoint_alignment(
                existing_content, analysis.get("entrypoint")
            ):
                staging_content = _align_fastapi_dockerfile_entrypoint(existing_content, analysis.get("entrypoint"))
                context_dir, dockerfile_path = _write_build_context(
                    dest, staging_content, analysis.get("source_subdir")
                )
                logger.info("Aligned FastAPI Dockerfile entrypoint with detected target %s", analysis.get("entrypoint"))
            else:
                context_dir, dockerfile_path = _write_build_context(
                    dest, existing_content, analysis.get("source_subdir")
                )
        else:
            if existing_dockerfile.exists() and (has_complex_dockerfile(existing_dockerfile) or requires_source_build or requires_replacement):
                logger.warning(
                    "Repo Dockerfile requires a source-build-compatible replacement for staging."
                )
            # Generated artifacts can be older than the current source or
            # framework rules. Recreate them from the disposable clone so
            # Java/React/Python runtimes always use the current detection.
            staging_content = _reliable_staging_dockerfile(app_dir, analysis)
            # A generated Dockerfile also needs imported monitoring packages
            # that were omitted from the repository's dependency manifest.
            staging_content = _patch_dockerfile_with_optional_deps(
                staging_content, _scan_optional_pip_packages(app_dir)
            )
            if (analysis.get("framework") or "").lower() == "fastapi":
                staging_content = _align_fastapi_dockerfile_entrypoint(
                    staging_content, analysis.get("entrypoint")
                )
            staging_content = _adjust_python_base_image(staging_content, app_dir)
            context_dir, dockerfile_path = _write_build_context(
                dest,
                staging_content,
                analysis.get("source_subdir"),
            )

        report("Building container image")
        docker_svc.build_image(
            context_dir,
            dockerfile_path,
            image,
            timeout=settings.docker_build_timeout,
            cancel_check=cancel_check,
            progress_callback=lambda line: report(f"Building container image: {line}"),
        )
        report("Loading image into Kubernetes container runtime")
        docker_svc.kind_load_image(image, cluster=cluster, cancel_check=cancel_check)

        local_files = {
            k: v
            for k, v in k8s_files.items()
            if k.endswith((".yaml", ".yml"))
        }
        if not local_files:
            raise RuntimeError("No Kubernetes manifests found. Run analysis first.")

        port = _detect_application_port(analysis, dockerfile_path, k8s_files)
        database = analysis.get("database")
        staging_dependencies = list(analysis.get("staging_dependencies") or [])
        env_vars = _collect_runtime_env(app_name, app_dir, port, database, staging_dependencies)
        patched = k8s_svc.patch_manifests_for_local(
            local_files, image=image, app_name=app_name, replicas=1, env_vars=env_vars
        )
        # Rename the Deployment to {app_name}-staging at the YAML level
        # (raw string replacement is fragile and can accidentally corrupt selectors/labels).
        if "deployment.yaml" in patched:
            import yaml as _yaml
            docs = list(_yaml.safe_load_all(patched["deployment.yaml"]))
            for doc in docs:
                if doc and doc.get("kind") == "Deployment":
                    meta = doc.setdefault("metadata", {})
                    if meta.get("name") == app_name:
                        meta["name"] = f"{app_name}-staging"
            patched["deployment.yaml"] = _yaml.dump_all(docs, default_flow_style=False)

        # Local kind keeps its developer-facing NodePort. Remote K3s exposes
        # applications through Traefik only when an operator has configured a domain.
        ingress_hostname = None
        if deployment_target() not in {"remote", "k3s"}:
            node_port = _allocate_node_port(f"{app_name}-{namespace}")
            patched["nodeport-service.yaml"] = _nodeport_service_manifest(
                app_name, port, node_port, namespace
            )
        elif settings.k3s_ingress_domain:
            ingress_domain = settings.k3s_ingress_domain.strip(".").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", ingress_domain):
                raise RuntimeError("K3S_INGRESS_DOMAIN must be a DNS hostname")
            ingress_hostname = f"app-{image_tag or project_id}.{ingress_domain}"
            patched["ingress.yaml"] = f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {app_name}
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
spec:
  ingressClassName: traefik
  rules:
    - host: {ingress_hostname}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {app_name}
                port:
                  number: 80
"""
        if database and "database.yaml" not in patched:
            database_manifest = generate_database_manifest(app_name, database)
            if database_manifest:
                patched["database.yaml"] = database_manifest
        for dependency in staging_dependencies:
            filename = f"dependency-{dependency}.yaml"
            if filename not in patched:
                manifest = generate_dependency_manifest(app_name, dependency)
                if manifest:
                    patched[filename] = manifest

        deploy_name = f"{app_name}-staging"
        support_files = {
            name: content for name, content in patched.items()
            if name == "database.yaml" or name.startswith("dependency-")
        }
        app_files = {name: content for name, content in patched.items() if name not in support_files}
        if support_files:
            report("Starting staging services")
            k8s_svc.apply_manifests(support_files, namespace=namespace)
            if database:
                k8s_svc.wait_rollout(
                    f"{app_name}-db",
                    namespace=namespace,
                    timeout_s=240,
                    cancel_check=cancel_check,
                    progress_callback=lambda detail: report(f"Waiting for staging database: {detail}"),
                )
            for dependency in staging_dependencies:
                report(f"Waiting for staging {dependency}")
                k8s_svc.wait_rollout(
                    f"{app_name}-{dependency}",
                    namespace=namespace,
                    timeout_s=300,
                    cancel_check=cancel_check,
                    progress_callback=lambda detail, dependency=dependency: report(f"Waiting for staging {dependency}: {detail}"),
                )
        report("Applying Kubernetes manifests")
        k8s_svc.apply_manifests(app_files, namespace=namespace)
        report("Waiting for application rollout")
        rollout = k8s_svc.wait_rollout(
            deploy_name,
            namespace=namespace,
            timeout_s=settings.k3s_deployment_timeout if deployment_target() in {"remote", "k3s"} else 300,
            cancel_check=cancel_check,
            progress_callback=lambda detail: report(f"Waiting for application rollout: {detail}"),
        )
        status = k8s_svc.get_deployment_status(deploy_name, namespace=namespace)
        report("Running staging smoke tests")
        smoke = k8s_svc.smoke_test(
            app_name,
            namespace=namespace,
            cancel_check=cancel_check,
            progress_callback=lambda detail: report(f"Running staging smoke tests: {detail}"),
        )

        if smoke.get("status") != "passed":
            raise RuntimeError(smoke.get("message") or "Smoke test failed")

        if deployment_target() in {"remote", "k3s"}:
            if ingress_hostname:
                ingress_url = f"http://{ingress_hostname}"
                try:
                    ingress_response = httpx.get(ingress_url, timeout=10.0, follow_redirects=True)
                    if ingress_response.status_code >= 500:
                        raise RuntimeError(f"Ingress returned HTTP {ingress_response.status_code}")
                    smoke["ingress_status_code"] = ingress_response.status_code
                except Exception as exc:
                    raise RuntimeError(f"Configured ingress is not reachable at {ingress_url}: {exc}") from exc
            access_url = f"http://{ingress_hostname}" if ingress_hostname else None
            smoke["url"] = access_url
            smoke["access"] = "ingress" if ingress_hostname else "worker-port-forward"
        else:
            access_url = f"http://localhost:3000/api/projects/{project_id}/preview/docs"
            smoke["url"] = access_url
        report("Staging deployment healthy")
        logger.info("deployment completed: staging %s accessible at %s", app_name, access_url)
        return StagingDeployResult(
            status="healthy",
            image=image,
            namespace=namespace,
            app_name=app_name,
            application_port=port,
            rollout=rollout,
            smoke=smoke,
            deployment_status=status,
            message=(
                f"Staging healthy at {access_url} ({smoke.get('message')})"
                if access_url else
                f"Staging healthy; no public hostname is configured. Use a worker-side kubectl port-forward for temporary access ({smoke.get('message')})"
            ),
        )
    finally:
        cleanup_repository(dest)


class ProductionDeployResult(BaseModel):
    status: str
    environment: str = "production"
    version: str = "green"
    image: str = ""
    namespace: str = ""
    app_name: str = ""
    rollout: str = ""
    smoke: dict = Field(default_factory=dict)
    deployment_status: dict = Field(default_factory=dict)
    message: str = ""


def deploy_green(
    project_id: int,
    repository_url: str,
    repository_name: str,
    analysis: dict,
    dockerfile: str,
    k8s_files: dict[str, str],
) -> ProductionDeployResult:
    app_name = sanitize_name(repository_name or analysis.get("repository_name") or "app")
    namespace = settings.k8s_namespace
    cluster = settings.kind_cluster_name
    image = f"deploymind/{app_name}:green"
    dest = Path(settings.workspace_dir) / f"project_{project_id}_prod_deploy"

    logger.info("deployment started: green production %s", app_name)

    k8s_svc.prepare_kubeconfig()
    if not k8s_svc.cluster_reachable():
        raise RuntimeError(
            f"Kubernetes cluster not reachable. Create kind cluster first: "
            f"kind create cluster --name {cluster}"
        )

    try:
        _prepare_deployment_source(project_id, repository_url, dest)
        app_dir = dest / analysis.get("source_subdir", "") if analysis.get("source_subdir") else dest
        if not app_dir.is_dir():
            app_dir = dest
        sanitize_requirements_file(app_dir)
        analysis = _refresh_runtime_analysis(app_dir, analysis)

        existing_dockerfile = app_dir / "Dockerfile"
        # Bypass complex Dockerfiles (bun, uv --mount, etc.) same as staging
        requires_source_build = requires_prebuilt_java_artifact(existing_dockerfile)
        requires_replacement = requires_staging_dockerfile_replacement(existing_dockerfile)
        if existing_dockerfile.exists() and not has_complex_dockerfile(existing_dockerfile) and not requires_source_build and not requires_replacement:
            existing_content = existing_dockerfile.read_text(encoding="utf-8", errors="ignore")
            existing_content = _patch_dockerfile_with_optional_deps(
                existing_content, _scan_optional_pip_packages(app_dir)
            )
            if (analysis.get("framework") or "").lower() == "fastapi" and _needs_fastapi_entrypoint_alignment(
                existing_content, analysis.get("entrypoint")
            ):
                staging_content = _align_fastapi_dockerfile_entrypoint(existing_content, analysis.get("entrypoint"))
                context_dir, dockerfile_path = _write_build_context(
                    dest, staging_content, analysis.get("source_subdir")
                )
                logger.info("Aligned FastAPI Dockerfile entrypoint with detected target %s", analysis.get("entrypoint"))
            else:
                dockerfile_path = existing_dockerfile
                context_dir = _determine_build_context(dest, app_dir, dockerfile_path, analysis.get("source_subdir"))
        else:
            if existing_dockerfile.exists() and (has_complex_dockerfile(existing_dockerfile) or requires_source_build or requires_replacement):
                logger.warning(
                    "Repo Dockerfile requires a source-build-compatible replacement for production build."
                )
            staging_content = _reliable_staging_dockerfile(app_dir, analysis)
            staging_content = _patch_dockerfile_with_optional_deps(
                staging_content, _scan_optional_pip_packages(app_dir)
            )
            if (analysis.get("framework") or "").lower() == "fastapi":
                staging_content = _align_fastapi_dockerfile_entrypoint(
                    staging_content, analysis.get("entrypoint")
                )
            staging_content = _adjust_python_base_image(staging_content, app_dir)
            context_dir, dockerfile_path = _write_build_context(
                dest,
                staging_content,
                analysis.get("source_subdir"),
            )

        docker_svc.build_image(context_dir, dockerfile_path, image, timeout=1800)
        docker_svc.kind_load_image(image, cluster=cluster)

        local_files = {
            k: v
            for k, v in k8s_files.items()
            if k.endswith((".yaml", ".yml"))
        }
        if not local_files:
            raise RuntimeError("No Kubernetes manifests found.")

        port = int(analysis.get("port") or 8000)
        database = analysis.get("database")
        staging_dependencies = list(analysis.get("staging_dependencies") or [])
        env_vars = _collect_runtime_env(app_name, app_dir, port, database, staging_dependencies)
        patched = k8s_svc.patch_manifests_for_blue_green(
            local_files, image=image, app_name=app_name, version="green", replicas=2, env_vars=env_vars
        )

        deploy_name = f"{app_name}-green"
        support_files: dict[str, str] = {}
        database_manifest = generate_database_manifest(app_name, database) if database else None
        if database_manifest:
            support_files["database.yaml"] = database_manifest
        for dependency in staging_dependencies:
            manifest = generate_dependency_manifest(app_name, dependency)
            if manifest:
                support_files[f"dependency-{dependency}.yaml"] = manifest
        if support_files:
            k8s_svc.apply_manifests(support_files, namespace=namespace)
            if database:
                k8s_svc.wait_rollout(f"{app_name}-db", namespace=namespace, timeout_s=240)
            for dependency in staging_dependencies:
                k8s_svc.wait_rollout(f"{app_name}-{dependency}", namespace=namespace, timeout_s=300)
        k8s_svc.apply_manifests(patched, namespace=namespace)
        rollout = k8s_svc.wait_rollout(deploy_name, namespace=namespace, timeout_s=240)
        status = k8s_svc.get_deployment_status(deploy_name, namespace=namespace)

        # Ensure blue deployment exists as rollback fallback
        blue_status = k8s_svc.get_deployment_status(f"{app_name}-blue", namespace=namespace)
        if not blue_status.get("ready"):
            blue_patched = k8s_svc.patch_manifests_for_blue_green(
                local_files, image=f"deploymind/{app_name}:staging", app_name=app_name, version="blue", replicas=1, env_vars=env_vars
            )
            k8s_svc.apply_manifests(blue_patched, namespace=namespace)

        smoke = k8s_svc.smoke_test(app_name, namespace=namespace)
        smoke["url"] = f"http://localhost:3000/api/projects/{project_id}/preview/docs"

        logger.info("green deployment ready: %s, awaiting approval", app_name)
        return ProductionDeployResult(
            status="awaiting_approval",
            image=image,
            namespace=namespace,
            app_name=app_name,
            rollout=rollout,
            smoke=smoke,
            deployment_status=status,
            message=f"GREEN version deployed and healthy. Open {smoke['url']} to test before approving.",
        )
    finally:
        cleanup_repository(dest)


def switch_traffic(app_name: str, namespace: str | None = None) -> dict:
    ns = namespace or settings.k8s_namespace
    msg = k8s_svc.switch_traffic(app_name, ns, "green")
    smoke = k8s_svc.smoke_test(app_name, ns)
    return {
        "status": "healthy",
        "version": "green",
        "message": f"{msg}. Smoke test: {smoke.get('message')}",
        "smoke": smoke,
    }


def rollback(app_name: str, namespace: str | None = None) -> dict:
    ns = namespace or settings.k8s_namespace
    msg = k8s_svc.rollback(app_name, ns)
    smoke = k8s_svc.smoke_test(app_name, ns)
    return {
        "status": "rolled_back",
        "version": "blue",
        "message": f"{msg}. Smoke test: {smoke.get('message')}",
        "smoke": smoke,
    }


def get_deployment_status(deployment_name: str, namespace: str | None = None) -> dict:
    ns = namespace or settings.k8s_namespace
    return k8s_svc.get_deployment_status(deployment_name, ns)


def run_smoke_test(service_name: str, namespace: str | None = None) -> dict:
    ns = namespace or settings.k8s_namespace
    return k8s_svc.smoke_test(service_name, ns)

