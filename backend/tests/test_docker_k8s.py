from app.agents.deployment_agent import (
    _adjust_python_base_image,
    _align_fastapi_dockerfile_entrypoint,
    _collect_runtime_env,
    _reliable_staging_dockerfile,
    _refresh_fastapi_entrypoint,
)
from app.agents.docker_agent import generate_dockerfile
from app.agents.kubernetes_agent import (
    generate_database_manifest,
    generate_dependency_manifest,
    generate_kubernetes,
    sanitize_name,
)
from app.agents.validate import validate_configuration, validate_dockerfile, validate_k8s_yaml
from app.services.deployment_control import DeploymentCancelled
from app.services.kubernetes import wait_rollout


def test_sanitize_name():
    assert sanitize_name("My App!") == "my-app"
    assert sanitize_name("") == "app"


def test_generate_fastapi_dockerfile(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    result = generate_dockerfile(
        tmp_path,
        {
            "language": "Python",
            "framework": "FastAPI",
            "entrypoint": "app.main:app",
            "port": 8000,
            "has_dockerfile": False,
        },
    )
    assert result.generated
    assert "FROM python:3.11-slim" in result.dockerfile
    assert "USER appuser" in result.dockerfile
    assert "uvicorn" in result.dockerfile
    assert validate_dockerfile(result.dockerfile) == []


def test_generate_legacy_fastapi_dockerfile_uses_python_310(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.75.1\norjson==3.6.7\ngreenlet==1.1.2\nhttptools==0.2.0\n",
        encoding="utf-8",
    )
    result = generate_dockerfile(
        tmp_path,
        {
            "language": "Python",
            "framework": "FastAPI",
            "entrypoint": "app.main:app",
            "port": 8000,
            "has_dockerfile": False,
        },
    )
    assert "FROM python:3.10-slim" in result.dockerfile


def test_python_base_image_respects_pyproject_minimum_version(tmp_path):
    from app.services.dependency_sanitizer import choose_python_base_image

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "template-backend"\nrequires-python = ">=3.14,<4.0"\n'
        'dependencies = ["sqlmodel>=0.0.46"]\n',
        encoding="utf-8",
    )
    assert choose_python_base_image(tmp_path) == "python:3.14-slim"


def test_staging_pyproject_install_uses_declared_dependencies_and_fails_fast(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "template-backend"\nrequires-python = ">=3.14,<4.0"\n'
        'dependencies = ["fastapi", "sqlmodel"]\n',
        encoding="utf-8",
    )
    dockerfile = _reliable_staging_dockerfile(
        tmp_path,
        {"language": "Python", "framework": "FastAPI", "package_manager": "pip", "entrypoint": "app.main:app"},
    )

    assert "FROM python:3.14-slim" in dockerfile
    assert "COPY . .\nRUN pip install --no-cache-dir ." in dockerfile
    assert "|| pip install --no-cache-dir fastapi" not in dockerfile


def test_adjust_python_base_image_rewrites_old_generated_dockerfile(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "fastapi==0.75.1\norjson==3.6.7\ngreenlet==1.1.2\nhttptools==0.2.0\n",
        encoding="utf-8",
    )
    dockerfile = "FROM python:3.11-slim\nWORKDIR /app\n"
    adjusted = _adjust_python_base_image(dockerfile, tmp_path)
    assert adjusted.startswith("FROM python:3.10-slim")


def test_staging_aligns_existing_fastapi_dockerfile_with_detected_entrypoint(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    dockerfile = 'FROM python:3.11-slim\nCMD ["uvicorn", "main:app", "--host", "0.0.0.0"]\n'

    analysis = _refresh_fastapi_entrypoint(tmp_path, {"framework": "FastAPI", "entrypoint": "main:app"})
    updated = _align_fastapi_dockerfile_entrypoint(dockerfile, analysis["entrypoint"])

    assert analysis["entrypoint"] == "app.api:app"
    assert '"app.api:app"' in updated
    assert '"main:app"' not in updated


def test_keeps_existing_dockerfile(tmp_path):
    existing = "FROM python:3.11\nCMD [\"python\", \"app.py\"]\n"
    (tmp_path / "Dockerfile").write_text(existing, encoding="utf-8")
    result = generate_dockerfile(
        tmp_path,
        {"language": "Python", "framework": "Flask", "has_dockerfile": True, "port": 5000},
    )
    assert result.generated is False
    assert result.dockerfile == existing
    assert result.suggestions


def test_generate_spring_boot_dockerfile_builds_source(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    result = generate_dockerfile(
        tmp_path,
        {"language": "Java", "framework": "Spring Boot", "package_manager": "maven", "port": 8080},
    )

    assert "FROM maven:3.9-eclipse-temurin-21 AS build" in result.dockerfile
    assert "mvn -DskipTests package" in result.dockerfile
    assert "COPY --from=build /tmp/app.jar app.jar" in result.dockerfile


def test_generate_react_dockerfile_matches_nginx_port(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    result = generate_dockerfile(
        tmp_path,
        {"language": "JavaScript", "framework": "React", "package_manager": "npm", "port": 80},
    )

    assert "FROM nginx:1.27-alpine" in result.dockerfile
    assert "EXPOSE 80" in result.dockerfile


def test_database_manifest_is_valid_and_database_aware():
    manifest = generate_database_manifest("demo-api", "postgresql")

    assert manifest is not None
    assert "postgres:16-alpine" in manifest
    assert "demo-api-db" in manifest
    assert "runAsNonRoot" not in manifest
    assert validate_k8s_yaml("database.yaml", manifest) == []


def test_postgis_and_required_service_manifests_are_valid():
    postgis = generate_database_manifest("food-fiesta", "postgis")
    redis = generate_dependency_manifest("food-fiesta", "redis")
    kafka = generate_dependency_manifest("food-fiesta", "kafka")

    assert postgis is not None and "postgis/postgis:16-3.4" in postgis
    assert redis is not None and "redis:7-alpine" in redis
    assert kafka is not None and "apache/kafka:3.7.0" in kafka
    assert "food-fiesta-kafka:9092" in kafka
    assert validate_k8s_yaml("postgis.yaml", postgis) == []
    assert validate_k8s_yaml("redis.yaml", redis) == []
    assert validate_k8s_yaml("kafka.yaml", kafka) == []


def test_wait_rollout_honors_cancellation_before_running_kubectl():
    try:
        wait_rollout("demo-staging", "deploymind", cancel_check=lambda: True)
    except DeploymentCancelled:
        pass
    else:
        raise AssertionError("Expected staging cancellation to stop rollout polling")


def test_staging_database_environment_targets_in_cluster_postgres(tmp_path):
    env = _collect_runtime_env("demo-api", tmp_path, 8080, "postgresql")

    assert env["SPRING_DATASOURCE_URL"] == "jdbc:postgresql://demo-api-db:5432/app"
    assert env["DATABASE_URL"] == "postgresql://deploymind:deploymind@demo-api-db:5432/app"


def test_staging_runtime_env_points_spring_to_detected_services(tmp_path):
    env = _collect_runtime_env("food-fiesta", tmp_path, 8080, "postgis", ["redis", "kafka"])

    assert env["SPRING_DATASOURCE_URL"] == "jdbc:postgresql://food-fiesta-db:5432/app"
    assert env["REDIS_HOST"] == "food-fiesta-redis"
    assert env["KAFKA_BOOTSTRAP_SERVERS"] == "food-fiesta-kafka:9092"


def test_reliable_staging_rebuilds_spring_boot_source(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")

    dockerfile = _reliable_staging_dockerfile(
        tmp_path,
        {"language": "Java", "framework": "Spring Boot", "package_manager": "maven", "port": 8080},
    )

    assert "FROM maven:3.9-eclipse-temurin-21 AS build" in dockerfile


def test_staging_recognizes_java_dockerfile_that_needs_a_prebuilt_jar(tmp_path):
    from app.services.docker import requires_prebuilt_java_artifact

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM eclipse-temurin:21-jre\nCOPY target/*.jar app.jar\n", encoding="utf-8")

    assert requires_prebuilt_java_artifact(dockerfile)


def test_staging_recognizes_buildkit_only_dockerfile(tmp_path):
    from app.services.docker import has_complex_dockerfile

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-slim\nRUN --mount=type=cache pip install -r requirements.txt\n", encoding="utf-8")

    assert has_complex_dockerfile(dockerfile)


def test_staging_replaces_legacy_poetry_dockerfile(tmp_path):
    from app.services.docker import requires_staging_dockerfile_replacement

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.9\nRUN pip install poetry==1.1 && poetry install --no-dev\n", encoding="utf-8")

    assert requires_staging_dockerfile_replacement(dockerfile)


def test_reliable_poetry_dockerfile_uses_supported_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8")
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")

    dockerfile = _reliable_staging_dockerfile(
        tmp_path,
        {"language": "Python", "framework": "FastAPI", "package_manager": "poetry", "entrypoint": "main:app"},
    )

    assert '"poetry>=1.8,<2.0"' in dockerfile
    assert "--no-root --only main" in dockerfile


def test_generate_kubernetes_and_validate():
    docker = generate_dockerfile(
        ".",
        {
            "language": "Python",
            "framework": "FastAPI",
            "entrypoint": "app.main:app",
            "port": 8000,
            "has_dockerfile": False,
        },
    ).dockerfile
    k8s = generate_kubernetes(
        {"port": 8000, "framework": "FastAPI"},
        app_name="demo-api",
    )
    assert set(k8s.files) == {"deployment.yaml", "service.yaml", "rbac.yaml"}
    assert "replicas: 2" in k8s.files["deployment.yaml"]
    assert "readinessProbe" in k8s.files["deployment.yaml"]
    assert "ServiceAccount" in k8s.files["rbac.yaml"]

    validation = validate_configuration(docker, k8s.files)
    assert validation.valid, validation.errors


def test_validation_rejects_bad_dockerfile():
    errors = validate_dockerfile("echo hello")
    assert errors
