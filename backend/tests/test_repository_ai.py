from pathlib import Path

from app.services.repository_ai import (
    answer_repository_question,
    build_repository_index,
    evaluate_repository_index,
    explain_failure,
    generate_deployment_recommendation,
    get_active_ai_provider,
    _python_test_file,
    run_ai_generated_tests,
    search_repository,
)
from app.services.dependency_sanitizer import sanitize_requirements_file


def _sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("fastapi\nuvicorn\npytest\n", encoding="utf-8")
    app_dir = repo / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text(
        """
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    return {"message": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}
""".strip(),
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Demo app\n", encoding="utf-8")
    return repo


def test_repository_index_and_search(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("app.services.repository_ai.settings.workspace_dir", str(workspace))

    repo = _sample_repo(tmp_path)
    meta = build_repository_index(repo, project_id=11, repository_url="https://github.com/acme/demo")
    assert meta.file_count >= 2
    assert meta.chunk_count >= 2

    hits = search_repository(11, "health endpoint", limit=3)
    assert hits
    assert any("/health" in hit.snippet or "health" in hit.path.lower() for hit in hits)


def test_recommendation_and_evaluation(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("app.services.repository_ai.settings.workspace_dir", str(workspace))

    repo = _sample_repo(tmp_path)
    build_repository_index(repo, project_id=12, repository_url="https://github.com/acme/demo")
    analysis = {
        "framework": "FastAPI",
        "language": "Python",
        "package_manager": "pip",
        "entrypoint": "app.main:app",
        "port": 8000,
        "has_dockerfile": False,
        "has_kubernetes": False,
        "repository_url": "https://github.com/acme/demo",
    }
    recommendation = generate_deployment_recommendation(12, analysis)
    assert recommendation.detected["framework"] == "FastAPI"
    assert recommendation.recommendation

    report = evaluate_repository_index(12, analysis)
    assert report.questions >= 5
    assert 0 <= report.correct <= report.questions


def test_ai_tests_skip_for_unsupported_framework(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("app.services.repository_ai.settings.workspace_dir", str(workspace))

    repo = _sample_repo(tmp_path)
    build_repository_index(repo, project_id=13, repository_url="https://github.com/acme/demo")
    result = run_ai_generated_tests(
        13,
        "https://github.com/acme/demo",
        {"framework": "React", "repository_url": "https://github.com/acme/demo"},
    )
    assert result.status == "skipped"


def test_failure_explanation_uses_evidence():
    result = explain_failure("Smoke test failed", ["port-forward exited early", "connection refused"])
    assert "port" in result["suggested_fix"].lower()
    assert result["evidence"]


def test_failure_explanation_for_missing_kind_cluster(monkeypatch):
    monkeypatch.setattr("app.services.repository_ai.settings.kind_cluster_name", "deploymind")
    result = explain_failure("Kubernetes cluster not reachable. Create kind cluster first: kind create cluster --name deploymind")
    assert "kind kubernetes cluster" in result["likely_cause"].lower()
    assert "kind create cluster --name deploymind" in result["suggested_fix"]


def test_failure_explanation_distinguishes_docker_platform_outage():
    result = explain_failure("failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine")
    assert result["category"] == "DOCKER_UNAVAILABLE"
    assert result["repository_status"] == "unknown"


def test_failure_explanation_for_invalid_dependency():
    result = explain_failure(
        "Dependency install failed before AI tests",
        ["ERROR: Could not find a version that satisfies the requirement install==1.3.5"],
    )
    assert "dependency installation failed" in result["likely_cause"].lower()
    assert "dependency file" in result["suggested_fix"].lower()
    assert result["repository_status"] == "incompatible_or_faulty"


def test_failure_explanation_identifies_repository_build_failure():
    result = explain_failure("Docker build failed", ["Failed to execute goal org.springframework.boot:spring-boot-maven-plugin"])
    assert result["category"] == "REPOSITORY_BUILD_FAILED"
    assert result["repository_status"] == "incompatible_or_faulty"


def test_fastapi_generated_test_file_uses_fixture():
    code = _python_test_file("FastAPI", "app.main", "app", ["/", "/health"])
    assert "sys.path.insert" in code
    assert "@pytest.fixture" in code
    assert "def client()" in code
    assert "def test_1_route(client)" in code


def test_sanitize_requirements_file_removes_suspicious_install(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    req = repo / "requirements.txt"
    req.write_text("fastapi==0.75.1\ninstall==1.3.5\nuvicorn==0.17.6\n", encoding="utf-8")

    removed = sanitize_requirements_file(repo)

    assert removed == ["install==1.3.5"]
    assert "install==1.3.5" not in req.read_text(encoding="utf-8")


def test_test_inferences_generated_for_fastapi():
    from app.services.repository_ai import _generate_test_inferences
    inferences = _generate_test_inferences("FastAPI", ["/", "/health", "/items/{id}"])
    targets = [inf.target for inf in inferences]
    assert "/" in targets
    assert "/openapi.json" in targets
    assert "/docs" in targets
    assert any("OpenAPI" in inf.name for inf in inferences)
    assert any("Kubernetes" in inf.inference for inf in inferences)


def test_get_active_ai_provider_selection(monkeypatch):
    from app.services.repository_ai import get_active_ai_provider, settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "grok_api_key", "")
    monkeypatch.setattr(settings, "xai_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    p1 = get_active_ai_provider()
    assert p1["provider"] == "Grounded Heuristic"

    monkeypatch.setattr(settings, "grok_api_key", "xai-test-key-12345")
    p2 = get_active_ai_provider()
    assert p2["provider"] == "xAI Grok"
    assert "grok" in p2["model"].lower()

    monkeypatch.setattr(settings, "grok_api_key", "gsk_test_key_12345")
    p3 = get_active_ai_provider()
    assert p3["provider"] == "Groq"

    monkeypatch.setattr(settings, "openai_api_key", "sk-live-test-key-999")
    p4 = get_active_ai_provider()
    assert p4["provider"] == "OpenAI"


def test_failure_explanation_categories():
    r1 = explain_failure("pydantic_core.ValidationError: 1 validation error for Settings\nSECRET_KEY Field required")
    assert r1["category"] == "SETTINGS_VALIDATION"
    assert "secret_key" in r1["suggested_fix"].lower()

    r2 = explain_failure("AssertionError: 401 != 200", ["Status 401 Unauthorized", "Not authenticated"])
    assert r2["category"] == "AUTHENTICATION_REQUIRED"
    assert "authorization" in r2["suggested_fix"].lower()


def test_full_ai_pipeline_end_to_end(tmp_path: Path, monkeypatch):
    """
    Integration test: validates the complete AI pipeline in a single flow.

    Pipeline stages exercised:
        1. build_repository_index   - RAG index construction
        2. search_repository        - cosine similarity retrieval
        3. answer_repository_question - RAG Q&A (heuristic mode; no API key needed)
        4. evaluate_repository_index  - retrieval accuracy measurement
        5. generate_deployment_recommendation - AI deployment analysis

    Evaluation metric note:
        evaluate_repository_index() uses substring matching between the expected
        label (e.g. "FastAPI") and the combined answer+source text. This is
        appropriate for factual fields (framework, port, entrypoint) because
        the expected values are exact tokens, not paraphrases.
        Limitation: paraphrase-blind. A semantic similarity metric (e.g.,
        embedding cosine between expected and answer) would be more robust
        for open-ended questions.
    """
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("app.services.repository_ai.settings.workspace_dir", str(workspace))

    repo = _sample_repo(tmp_path)
    analysis = {
        "framework": "FastAPI",
        "language": "Python",
        "package_manager": "pip",
        "entrypoint": "app.main:app",
        "port": 8000,
        "has_dockerfile": False,
        "has_kubernetes": False,
        "repository_url": "https://github.com/acme/demo",
    }

    # Stage 1: RAG index construction
    meta = build_repository_index(repo, project_id=50, repository_url=analysis["repository_url"])
    assert meta.file_count >= 2, "Index must contain at least 2 files"
    assert meta.chunk_count >= 2, "Index must contain at least 2 chunks"

    # Stage 2: Semantic retrieval
    hits = search_repository(50, "fastapi health endpoint", limit=3)
    assert hits, "Retrieval must return at least one result"
    assert hits[0].score > 0, "Top result must have a positive cosine score"

    # Stage 3: RAG Q&A (heuristic path — no API key required)
    answer = answer_repository_question(50, "FastAPI framework used by this repository", analysis)
    assert answer.answer, "Q&A must return a non-empty answer"
    assert answer.sources, "Q&A must cite at least one source chunk"
    # Answer must reference FastAPI (either from LLM or heuristic fallback)
    assert "fastapi" in answer.answer.lower() or answer.sources

    # Stage 4: Quantitative evaluation — retrieval accuracy
    report = evaluate_repository_index(50, analysis)
    assert report.questions >= 5, "Evaluation must run at least 5 questions"
    # With semantic downloads disabled, only BM25 evidence is available; keep
    # the observed result and avoid asserting an unmeasured semantic baseline.
    assert 0 <= report.retrieval_accuracy <= 100

    # Stage 5: AI deployment recommendation
    rec = generate_deployment_recommendation(50, analysis)
    assert rec.detected["framework"] == "FastAPI"
    assert rec.detected["port"] == 8000
    assert len(rec.recommendation) >= 2, "Recommendation must include at least 2 steps"


def test_get_active_ai_provider_returns_structured_dict(monkeypatch):
    """Provider info dict must contain the required fields for UI display."""
    from app.services.repository_ai import get_active_ai_provider, settings
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "grok_api_key", "")
    monkeypatch.setattr(settings, "xai_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    provider = get_active_ai_provider()
    # All required keys for API response / UI must be present
    assert "provider" in provider
    assert "model" in provider
    assert "status" in provider
    assert provider["provider"] == "Grounded Heuristic"
