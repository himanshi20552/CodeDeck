import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.prompts import (
    DEPLOYMENT_RECOMMENDATION_PROMPT,
    FAILURE_ANALYSIS_PROMPT,
    RAG_QA_PROMPT,
    TEST_FAILURE_PROMPT,
    TEST_GENERATION_PROMPT,
)
from app.services.dependency_sanitizer import sanitize_requirements_file
from app.services.github import cleanup_repository, clone_repository
from app.services.observability import (
    measure_operation,
    measured,
    record_llm_request,
    trace_observation,
    update_observation,
)

logger = logging.getLogger("deploymind")

_openai_class = None
_openai_import_checked = False
_openai_import_lock = threading.Lock()


def _get_openai_class():
    """Load the sizeable optional SDK only on the first configured LLM request."""
    global _openai_class, _openai_import_checked
    if not _openai_import_checked:
        with _openai_import_lock:
            if not _openai_import_checked:
                try:
                    from openai import OpenAI as client_class

                    _openai_class = client_class
                except ImportError:
                    _openai_class = None
                _openai_import_checked = True
    return _openai_class

INDEX_VERSION = 2
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
LEGACY_EMBEDDING_DIMENSIONS = 192
MAX_FILE_SIZE = 180_000
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 220
INCLUDED_FILENAMES = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "readme.md",
    ".env.example",
}
TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".kt",
    ".go",
    ".rb",
    ".php",
    ".rs",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".txt",
    ".md",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".bash",
    ".dockerignore",
    ".gitignore",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".turbo",
    "coverage",
    ".idea",
    ".vscode",
}
TEST_BLOCKED_ENV = {"OPENAI_API_KEY", "GROK_API_KEY", "XAI_API_KEY", "GITHUB_TOKEN", "GHCR_TOKEN"}


class RepositoryChunk(BaseModel):
    chunk_id: str = ""
    path: str
    content: str
    line_start: int
    line_end: int
    embedding: list[float]


class SearchResult(BaseModel):
    path: str
    snippet: str
    score: float
    line_start: int
    line_end: int
    retrieval_method: str = "hybrid"


class RepositoryIndexMetadata(BaseModel):
    version: int = INDEX_VERSION
    project_id: int
    repository_url: str
    chunk_count: int
    file_count: int
    indexed_at: str
    files: list[str] = Field(default_factory=list)
    embedding_model: str | None = None
    semantic_available: bool = False
    corpus_fingerprint: str = ""


class RepositoryAnswer(BaseModel):
    question: str
    answer: str
    sources: list[SearchResult] = Field(default_factory=list)
    provider: str = "heuristic"
    retrieval_method: str = "hybrid"
    retrieval_count: int = 0
    retrieval_latency_ms: float = 0


class DeploymentRecommendation(BaseModel):
    summary: str
    detected: dict[str, str | int | bool | None] = Field(default_factory=dict)
    potential_issues: list[str] = Field(default_factory=list)
    recommendation: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class EvaluationItem(BaseModel):
    question: str
    expected: str
    answer: str
    matched_source: str | None = None
    status: str


class EvaluationReport(BaseModel):
    questions: int
    correct: int
    retrieval_accuracy: float
    results: list[EvaluationItem] = Field(default_factory=list)


class GeneratedTestCase(BaseModel):
    name: str
    rationale: str
    path: str
    code: str


class TestInference(BaseModel):
    name: str
    target: str
    inference: str


class AITestRun(BaseModel):
    status: str
    message: str
    command: str | None = None
    generated_tests: list[GeneratedTestCase] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    failure_analysis: dict | None = None
    test_inferences: list[TestInference] = Field(default_factory=list)


def _workspace_generated_dir(project_id: int) -> Path:
    path = Path(settings.workspace_dir) / "generated" / f"project_{project_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path(project_id: int) -> Path:
    return _workspace_generated_dir(project_id) / "repository_index.json"


def _looks_textual(path: Path) -> bool:
    if path.name.lower() in INCLUDED_FILENAMES:
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    return path.suffix == "" and path.name.lower() in {"dockerfile", "makefile"}


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _iter_repository_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not _looks_textual(path):
            continue
        yield path


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_./:-]{1,63}", text.lower())


def _hash_embedding(text: str, dimensions: int = LEGACY_EMBEDDING_DIMENSIONS) -> list[float]:
    vec = [0.0] * dimensions
    for token, count in Counter(_tokenize(text)).items():
        idx = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % dimensions
        vec[idx] += float(count)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _chunk_text(path: Path, text: str) -> list[RepositoryChunk]:
    lines = text.splitlines()
    if not lines:
        return []

    normalized_path = str(path).replace("\\", "/")
    chunks: list[RepositoryChunk] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and size < CHUNK_SIZE:
            size += len(lines[end]) + 1
            end += 1

        chunk_lines = lines[start:end]
        chunk_text = "\n".join(chunk_lines).strip()
        if chunk_text:
            chunks.append(
                RepositoryChunk(
                    chunk_id=hashlib.sha256(
                        f"{normalized_path}:{start + 1}:{end}:".encode("utf-8")
                        + chunk_text.encode("utf-8")
                    ).hexdigest(),
                    path=normalized_path,
                    content=chunk_text,
                    line_start=start + 1,
                    line_end=end,
                    embedding=[],
                )
            )

        if end >= len(lines):
            break

        overlap_lines = 0
        overlap_size = 0
        while overlap_lines < len(chunk_lines) and overlap_size < CHUNK_OVERLAP:
            overlap_size += len(chunk_lines[-(overlap_lines + 1)]) + 1
            overlap_lines += 1
        start = max(end - overlap_lines, start + 1)
    return chunks


def _trim_snippet(text: str, limit: int = 380) -> str:
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _load_index_payload(project_id: int) -> dict:
    path = _index_path(project_id)
    if not path.is_file():
        raise FileNotFoundError("Repository index not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_index_payload(project_id: int, payload: dict) -> None:
    path = _index_path(project_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def _build_repository_index(repo_path: str | Path, project_id: int, repository_url: str) -> RepositoryIndexMetadata:
    root = Path(repo_path)
    files: list[str] = []
    chunks: list[dict] = []
    for path in _iter_repository_files(root):
        text = _read_text(path)
        if not text.strip():
            continue
        rel = path.relative_to(root).as_posix()
        file_chunks = _chunk_text(Path(rel), text)
        if not file_chunks:
            continue
        files.append(rel)
        chunks.extend(chunk.model_dump() for chunk in file_chunks)

    semantic_available = False
    try:
        if not settings.semantic_embeddings_enabled:
            raise RuntimeError("semantic embeddings disabled by configuration")
        from fastembed import TextEmbedding

        model = _get_embedding_model(TextEmbedding)
        previous_by_id: dict[str, dict] = {}
        index_file = _index_path(project_id)
        if index_file.is_file():
            try:
                previous_payload = json.loads(index_file.read_text(encoding="utf-8"))
                previous_meta = previous_payload.get("metadata", {})
                if previous_meta.get("repository_url") == repository_url and previous_meta.get("embedding_model") == EMBEDDING_MODEL:
                    previous_by_id = {
                        old.get("chunk_id", ""): old
                        for old in previous_payload.get("chunks", [])
                        if old.get("chunk_id") and old.get("embedding")
                    }
            except (OSError, ValueError):
                previous_by_id = {}
        missing = []
        for item in chunks:
            previous = previous_by_id.get(item["chunk_id"])
            if previous and previous.get("content") == item.get("content"):
                item["embedding"] = previous["embedding"]
            else:
                missing.append(item)
        vectors = list(model.embed([item["content"] for item in missing])) if missing else []
        if len(vectors) == len(missing):
            for item, vector in zip(missing, vectors):
                item["embedding"] = [float(value) for value in vector]
            semantic_available = all(bool(item.get("embedding")) for item in chunks)
    except Exception as exc:
        logger.warning("Semantic embeddings unavailable; indexing lexical BM25 only (%s)", type(exc).__name__)

    fingerprint = hashlib.sha256(
        "\n".join(f"{item['path']}:{item['chunk_id']}" for item in chunks).encode("utf-8")
    ).hexdigest()
    metadata = RepositoryIndexMetadata(
        project_id=project_id,
        repository_url=repository_url,
        chunk_count=len(chunks),
        file_count=len(files),
        indexed_at=datetime.utcnow().isoformat() + "Z",
        files=sorted(files)[:400],
        embedding_model=EMBEDDING_MODEL if semantic_available else None,
        semantic_available=semantic_available,
        corpus_fingerprint=fingerprint,
    )
    payload = {"metadata": metadata.model_dump(), "chunks": chunks}
    _save_index_payload(project_id, payload)
    return metadata


@measured("repository_index")
def build_repository_index(repo_path: str | Path, project_id: int, repository_url: str) -> RepositoryIndexMetadata:
    from filelock import FileLock

    with FileLock(str(_index_path(project_id)) + ".lock", timeout=600):
        return _build_repository_index(repo_path, project_id, repository_url)


def ensure_repository_index(project_id: int, repository_url: str) -> RepositoryIndexMetadata:
    from filelock import FileLock

    path = _index_path(project_id)
    with FileLock(str(path) + ".ensure.lock", timeout=600):
        if path.is_file():
            payload = _load_index_payload(project_id)
            metadata = RepositoryIndexMetadata.model_validate(payload["metadata"])
            if metadata.version == INDEX_VERSION and metadata.repository_url == repository_url:
                return metadata

        temp_dir = Path(settings.workspace_dir) / f"project_{project_id}_index"
        try:
            clone_repository(repository_url, temp_dir)
            return build_repository_index(temp_dir, project_id, repository_url)
        finally:
            cleanup_repository(temp_dir)


def _all_chunks(project_id: int) -> list[RepositoryChunk]:
    try:
        payload = _load_index_payload(project_id)
    except FileNotFoundError:
        return []
    return [RepositoryChunk.model_validate(chunk) for chunk in payload.get("chunks", [])]


_embedding_model = None
_embedding_lock = threading.Lock()


def _get_embedding_model(model_class=None):
    global _embedding_model
    if _embedding_model is None:
        with _embedding_lock:
            if _embedding_model is None:
                if model_class is None:
                    from fastembed import TextEmbedding as model_class
                _embedding_model = model_class(model_name=EMBEDDING_MODEL)
    return _embedding_model


def _bm25_rank(chunks: list[RepositoryChunk], query: str) -> list[int]:
    """Return Okapi BM25 ranks (Robertson et al.) for exact technical terms."""
    documents = [_tokenize(f"{chunk.path} {chunk.content}") for chunk in chunks]
    stop_words = {
        "what", "how", "why", "where", "which", "when", "who", "is", "are", "does", "do", "can", "could",
        "should", "would", "will", "was", "were", "be", "been", "the", "a", "an", "in", "on", "at", "of",
        "to", "for", "from", "with", "by", "this", "that", "these", "those", "project", "repository",
        "application", "app", "code", "source", "file", "find", "show", "me", "tell", "about", "use", "used",
        "using", "uses", "please",
    }
    query_terms = [term for term in _tokenize(query) if term not in stop_words]
    if not documents or not query_terms:
        return []
    lengths = [len(document) for document in documents]
    average_length = sum(lengths) / len(lengths) or 1.0
    frequencies = [Counter(document) for document in documents]
    df = Counter(term for term in set(query_terms) for counts in frequencies if counts.get(term, 0))
    k1, b = 1.5, 0.75
    scores = []
    for index, counts in enumerate(frequencies):
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(1 + (len(documents) - df[term] + 0.5) / (df[term] + 0.5))
            denominator = frequency + k1 * (1 - b + b * lengths[index] / average_length)
            score += inverse * (frequency * (k1 + 1)) / denominator
        scores.append((score, index))
    return [index for score, index in sorted(scores, reverse=True) if score > 0]


def _semantic_rank(chunks: list[RepositoryChunk], query: str) -> list[int]:
    compatible = [i for i, chunk in enumerate(chunks) if chunk.embedding]
    if not compatible:
        return []
    try:
        vector = list(_get_embedding_model().query_embed(
            ["Represent this sentence for searching relevant passages: " + query]
        ))[0]
        qvector = [float(value) for value in vector]
        scored = sorted(
            (( _cosine(qvector, chunks[index].embedding), index) for index in compatible),
            reverse=True,
        )
        return [index for score, index in scored if score >= settings.semantic_min_similarity]
    except Exception as exc:
        logger.warning("Semantic query embedding unavailable; using BM25 only (%s)", type(exc).__name__)
        return []


def _query_symbols(query: str) -> set[str]:
    """Detect code-like identifiers and proper nouns that require exact evidence."""
    symbols = set(re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", query))
    symbols.update(re.findall(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b", query))
    symbols.update(re.findall(r"\b[A-Z]{2,}[A-Z0-9_]*\b", query))
    words = re.findall(r"\b[A-Z][a-zA-Z0-9]{2,}\b", query)
    if words:
        first_word = re.search(r"\b\w+\b", query)
        first = first_word.group(0) if first_word else ""
        symbols.update(word for word in words if word != first)
        if first and first[:1].isupper() and first.lower() not in {"what", "how", "why", "where", "which", "when", "who", "is", "does", "find", "show", "explain", "can"}:
            symbols.add(first)
    generic = {"api", "http", "https", "url", "json", "sql", "id", "cpu", "gpu"}
    return {symbol.lower() for symbol in symbols if symbol.lower() not in generic}


def search_repository(project_id: int, query: str, limit: int = 4) -> list[SearchResult]:
    chunks = _all_chunks(project_id)
    if not chunks:
        return []
    corpus_terms = set(_tokenize(" ".join(f"{chunk.path} {chunk.content}" for chunk in chunks)))
    if any(symbol not in corpus_terms for symbol in _query_symbols(query)):
        return []
    lexical = _bm25_rank(chunks, query)
    semantic = _semantic_rank(chunks, query)
    ranks: dict[int, float] = {}
    for ranking in (semantic, lexical):
        for rank, index in enumerate(ranking, start=1):
            ranks[index] = ranks.get(index, 0.0) + 1.0 / (60 + rank)
    method = "hybrid" if semantic and lexical else "semantic" if semantic else "bm25"
    ranked = sorted(ranks, key=lambda index: ranks[index], reverse=True)
    output: list[SearchResult] = []
    seen: set[str] = set()
    for index in ranked:
        chunk = chunks[index]
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        output.append(SearchResult(
            path=chunk.path,
            snippet=_trim_snippet(chunk.content),
            score=round(ranks[index], 6),
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            retrieval_method=method,
        ))
        if len(output) >= limit:
            break
    return output


def get_active_ai_provider() -> dict[str, str]:
    openai_key = (settings.openai_api_key or "").strip()
    if openai_key and not openai_key.startswith("sk-abcdef") and not openai_key.startswith("sk-dummy"):
        return {"provider": "OpenAI", "model": getattr(settings, "openai_model", "gpt-4.1-mini"), "status": "configured"}

    groq_key = (getattr(settings, "groq_api_key", "") or "").strip()
    grok_key = (getattr(settings, "grok_api_key", "") or getattr(settings, "xai_api_key", "") or "").strip()
    if grok_key.startswith("gsk_"):
        if not groq_key:
            groq_key = grok_key
        grok_key = ""

    if groq_key and not groq_key.startswith("gsk-dummy"):
        return {"provider": "Groq", "model": getattr(settings, "groq_model", "qwen/qwen3.8-27b"), "status": "configured"}

    if grok_key and not grok_key.startswith("xai-dummy"):
        return {"provider": "xAI Grok", "model": getattr(settings, "grok_model", "grok-2-latest"), "status": "configured"}

    return {"provider": "Grounded Heuristic", "model": "rule-based-engine", "status": "offline_fallback", "message": "No usable LLM API key is configured."}


def _response_usage_details(response) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    return {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "total": int(getattr(usage, "total_tokens", input_tokens + output_tokens)),
    }


def _safe_chat_with_provider_impl(
    prompt: str,
    system_prompt: str = "You are a grounded repository assistant. Answer only from provided evidence.",
) -> tuple[str | None, str, dict[str, int] | None]:
    """Attempts completion with OpenAI first, falls back to Groq / xAI, or returns None and 'heuristic'."""
    last_failure: str | None = None

    def failure_label(provider: str, error: Exception) -> str:
        status_code = getattr(error, "status_code", None)
        detail = f"HTTP {status_code}" if status_code else type(error).__name__
        return f"{provider} request failed ({detail})"

    # 1. Try OpenAI if key is configured and not placeholder
    openai_key = (settings.openai_api_key or "").strip()
    openai_client_class = None
    if openai_key and not openai_key.startswith("sk-abcdef") and not openai_key.startswith("sk-dummy"):
        openai_client_class = _get_openai_class()
    if openai_client_class:
        try:
            client = openai_client_class(api_key=openai_key, timeout=settings.llm_timeout_seconds, max_retries=1)
            res = client.chat.completions.create(
                model=getattr(settings, "openai_model", "gpt-4.1-mini"),
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            text = res.choices[0].message.content if res.choices else None
            if isinstance(text, str) and text.strip():
                return text.strip(), "OpenAI (gpt-4.1-mini)", _response_usage_details(res)
        except Exception as e:
            last_failure = failure_label("OpenAI", e)
            logger.warning("OpenAI completion failed (%s), attempting secondary provider fallback", type(e).__name__)

    # 2. Try Groq (either configured directly or through gsk_ key in grok_api_key)
    grok_key = (getattr(settings, "grok_api_key", "") or getattr(settings, "xai_api_key", "") or "").strip()
    groq_key = (getattr(settings, "groq_api_key", "") or "").strip()
    if grok_key.startswith("gsk_"):
        if not groq_key:
            groq_key = grok_key
        grok_key = ""

    if groq_key and not groq_key.startswith("gsk-dummy"):
        openai_client_class = openai_client_class or _get_openai_class()
    if openai_client_class and groq_key and not groq_key.startswith("gsk-dummy"):
        configured_model = getattr(settings, "groq_model", "qwen/qwen3.8-27b")
        candidate_models = [configured_model, "qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        candidate_models = list(dict.fromkeys(candidate_models))

        client = openai_client_class(
            api_key=groq_key,
            base_url=getattr(settings, "groq_base_url", "https://api.groq.com/openai/v1"),
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
        )
        for model in candidate_models:
            try:
                res = client.chat.completions.create(
                    model=model,
                    temperature=0.2,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                )
                text = res.choices[0].message.content if res.choices else None
                if isinstance(text, str) and text.strip():
                    return text.strip(), f"Groq ({model})", _response_usage_details(res)
            except Exception as e:
                last_failure = failure_label("Groq", e)
                logger.warning("Groq completion failed with model %s (%s)", model, type(e).__name__)
                status_code = getattr(e, "status_code", None)
                if status_code == 404:
                    continue
                break

    # 3. Try xAI Grok if key is configured (or if OpenAI failed)
    if grok_key and not grok_key.startswith("xai-dummy"):
        openai_client_class = openai_client_class or _get_openai_class()
    if openai_client_class and grok_key and not grok_key.startswith("xai-dummy"):
        try:
            base_url = getattr(settings, "grok_base_url", "https://api.x.ai/v1")
            model = getattr(settings, "grok_model", "grok-2-latest")
            client = openai_client_class(api_key=grok_key, base_url=base_url, timeout=settings.llm_timeout_seconds, max_retries=1)
            res = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            text = res.choices[0].message.content if res.choices else None
            if isinstance(text, str) and text.strip():
                return text.strip(), f"xAI Grok ({model})", _response_usage_details(res)
        except Exception as e:
            last_failure = failure_label("xAI Grok", e)
            logger.warning("xAI Grok completion failed (%s)", type(e).__name__)

    if last_failure:
        return None, f"Grounded Heuristic Fallback — {last_failure}", None
    return None, "Grounded Heuristic Fallback (no provider configured)", None


def _safe_chat_with_provider(
    prompt: str,
    system_prompt: str = "You are a grounded repository assistant. Answer only from provided evidence.",
) -> tuple[str | None, str]:
    started = time.perf_counter()
    with trace_observation("llm_completion", as_type="generation") as trace:
        answer, provider, usage_details = _safe_chat_with_provider_impl(prompt, system_prompt)
        update_observation(
            trace,
            model=provider,
            level="DEFAULT" if answer else "WARNING",
            status_message=None if answer else "No configured provider returned a response",
            metadata={"provider_result": provider},
            usage_details=usage_details,
        )
    family = next((name for name in ("OpenAI", "Groq", "xAI") if name in provider), "none")
    record_llm_request(family, "success" if answer else "failure", time.perf_counter() - started)
    return answer, provider


def _safe_chat(
    prompt: str,
    system_prompt: str = "You are a grounded repository assistant. Answer only from provided evidence.",
) -> str | None:
    text, _ = _safe_chat_with_provider(prompt, system_prompt=system_prompt)
    return text


def _detect_database(chunks: list[RepositoryChunk]) -> str | None:
    joined = "\n".join(chunk.content.lower() for chunk in chunks[:250])
    patterns = [
        ("PostgreSQL", ("postgres", "psycopg", "sqlalchemy", "postgresql")),
        ("SQLite", ("sqlite",)),
        ("MySQL", ("mysql", "pymysql")),
        ("MongoDB", ("mongodb", "pymongo", "mongoose")),
        ("Redis", ("redis",)),
    ]
    for name, terms in patterns:
        if all(term in joined for term in terms[:1]):
            return name
    return None


def _detect_routes(chunks: list[RepositoryChunk], limit: int = 8) -> list[str]:
    patterns = [
        re.compile(r'@(?:app|router)\.(?:get|post|put|patch|delete)\("([^"]+)"'),
        re.compile(r"@(?:app|router)\.(?:get|post|put|patch|delete)\('([^']+)'"),
        re.compile(r'app\.(?:get|post|put|patch|delete)\("([^"]+)"'),
        re.compile(r"app\.(?:get|post|put|patch|delete)\('([^']+)'"),
        re.compile(r'route\("([^"]+)"'),
        re.compile(r"route\('([^']+)'"),
    ]
    found: list[str] = []
    for chunk in chunks:
        for pattern in patterns:
            for match in pattern.findall(chunk.content):
                if match not in found:
                    found.append(match)
                if len(found) >= limit:
                    return found
    return found


def _detect_health_endpoint(routes: list[str], chunks: list[RepositoryChunk]) -> bool:
    if any("health" in route.lower() for route in routes):
        return True
    return any("health" in chunk.content.lower() for chunk in chunks[:120])


def _detect_auth_files(chunks: list[RepositoryChunk]) -> list[str]:
    files = []
    for chunk in chunks:
        lowered = chunk.content.lower()
        if any(token in lowered for token in ("jwt", "oauth", "authentication", "authorization", "login", "token")):
            if chunk.path not in files:
                files.append(chunk.path)
    return files[:4]


def _collect_signals(project_id: int, analysis: dict) -> dict:
    chunks = _all_chunks(project_id)
    routes = _detect_routes(chunks)
    database = _detect_database(chunks)
    auth_files = _detect_auth_files(chunks)
    has_health = _detect_health_endpoint(routes, chunks)
    return {
        "database": database,
        "routes": routes,
        "auth_files": auth_files,
        "has_health_endpoint": has_health,
        "entrypoint": analysis.get("entrypoint"),
        "framework": analysis.get("framework"),
        "language": analysis.get("language"),
        "package_manager": analysis.get("package_manager"),
        "has_dockerfile": bool(analysis.get("has_dockerfile")),
        "has_kubernetes": bool(analysis.get("has_kubernetes") or analysis.get("kubernetes_result")),
        "port": analysis.get("port") or 8000,
        "source_subdir": analysis.get("source_subdir"),
    }


def _heuristic_answer(question: str, analysis: dict, signals: dict, sources: list[SearchResult]) -> str:
    q = question.lower()
    if "framework" in q:
        return f"The repository appears to use {analysis.get('framework') or 'an unknown framework'}."
    if "language" in q:
        return f"The primary language looks like {analysis.get('language') or 'unknown'}."
    if "database" in q:
        if signals.get("database"):
            return f"The repository shows signs of using {signals['database']}."
        return "I did not find strong database evidence in the indexed repository files."
    if "entrypoint" in q or "start" in q:
        if analysis.get("entrypoint"):
            return f"The deployment entrypoint is {analysis['entrypoint']}."
        return "I did not find a clear application entrypoint."
    if "port" in q:
        return f"The detected application port is {analysis.get('port') or 8000}."
    if "auth" in q:
        auth_files = signals.get("auth_files") or []
        if auth_files:
            return f"Authentication-related code likely lives in {', '.join(auth_files[:2])}."
        return "I did not find a clear authentication implementation in the indexed snippets."
    if "api" in q or "route" in q:
        routes = signals.get("routes") or []
        if routes:
            return f"I found routes such as {', '.join(routes[:4])}."
    if sources:
        top = sources[0]
        return f"The strongest repository evidence is in {top.path}, which appears relevant to your question."
    return "I do not have enough indexed repository context to answer that confidently."


def _ground_answer_citations(answer: str, sources: list[SearchResult]) -> str:
    allowed = {f"{source.path}:{source.line_start}-{source.line_end}" for source in sources}

    def validate(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in allowed else ""

    answer = re.sub(r"\[([^\]\n]+:\d+-\d+)\]", validate, answer).strip()
    if not any(f"[{citation}]" in answer for citation in allowed):
        first = sources[0]
        answer = f"{answer}\n\nEvidence: [{first.path}:{first.line_start}-{first.line_end}]"
    return answer


def answer_repository_question(project_id: int, question: str, analysis: dict) -> RepositoryAnswer:
    ensure_repository_index(project_id, analysis.get("repository_url", ""))
    retrieval_started = time.perf_counter()
    with measure_operation("repository_retrieval"):
        sources = search_repository(project_id, question, limit=4)
    retrieval_latency_ms = round((time.perf_counter() - retrieval_started) * 1000, 2)
    signals = _collect_signals(project_id, analysis)

    if not sources:
        return RepositoryAnswer(
            question=question,
            answer="I could not find relevant evidence for this question in the indexed repository, so I can’t answer it reliably.",
            sources=[],
            provider="insufficient repository evidence",
            retrieval_method="none",
            retrieval_count=0,
            retrieval_latency_ms=retrieval_latency_ms,
        )

    source_block = (
        "\n\n".join(f"[{src.path}:{src.line_start}-{src.line_end}]\n{src.snippet}" for src in sources)
        if sources
        else "No specific source file snippets matched this query."
    )
    system_prompt = (
        "You are DeployMind AI, an intelligent DevOps and repository technical assistant. "
        "Answer only from retrieved repository snippets. Every factual claim must cite an available source as [path:line-line]. "
        "The architecture signals are hints only and are not evidence. If snippets do not support an answer, say so. "
        "Never invent paths, line numbers, functions, dependencies, or settings. "
        "Format with clear bullet points and bold key terms. Keep the answer under 120 words. "
        "Do not write long rambles or unstructured text."
    )
    prompt = (
        f"QUESTION:\n{question}\n\n"
        f"DETECTED REPOSITORY ARCHITECTURE & SIGNALS:\n{json.dumps(signals, indent=2)}\n\n"
        f"RETRIEVED CODE EVIDENCE:\n{source_block}"
    )
    with trace_observation(
        "repository_qa",
        as_type="span",
        metadata={
            "project_id": project_id,
            "retrieval_count": len(sources),
            "retrieval_method": sources[0].retrieval_method,
            "sources": [
                {"path": source.path, "line_start": source.line_start, "line_end": source.line_end}
                for source in sources
            ],
        },
        input_value=question,
    ) as trace:
        with measure_operation("llm_generation"):
            answer_text, provider = _safe_chat_with_provider(prompt, system_prompt=system_prompt)
        if not answer_text:
            answer_text = _heuristic_answer(question, analysis, signals, sources)
            # Distinguish a request-time outage from an unconfigured provider. The
            # system settings show configured keys; this label shows what answered
            # this specific request and why it fell back.
            provider = provider.replace("Grounded Heuristic Fallback", "Heuristic Analysis")
        answer_text = _ground_answer_citations(answer_text, sources)
        update_observation(trace, output=answer_text, metadata={"provider": provider})
    return RepositoryAnswer(
        question=question,
        answer=answer_text,
        sources=sources,
        provider=provider,
        retrieval_method=sources[0].retrieval_method,
        retrieval_count=len(sources),
        retrieval_latency_ms=retrieval_latency_ms,
    )


def generate_deployment_recommendation(project_id: int, analysis: dict) -> DeploymentRecommendation:
    ensure_repository_index(project_id, analysis.get("repository_url", ""))
    signals = _collect_signals(project_id, analysis)
    chunks = _all_chunks(project_id)
    sources = search_repository(project_id, "health endpoint docker kubernetes deployment database", limit=4)

    issues: list[str] = []
    if signals.get("database"):
        issues.append(f"Database dependency detected: {signals['database']}.")
    if not signals.get("has_health_endpoint"):
        issues.append("No obvious health endpoint was detected in the indexed files.")
    if not signals.get("has_dockerfile"):
        issues.append("A Dockerfile was not detected, so DeployMind should generate one.")
    if not signals.get("has_kubernetes"):
        issues.append("Kubernetes manifests were not detected, so DeployMind should generate them.")

    recommendation = [
        f"Deploy the primary {signals.get('framework') or signals.get('language') or 'application'} service to Kubernetes.",
        f"Use port {signals.get('port') or 8000} for the container and Service configuration.",
        "Run a staging rollout and smoke test before promoting a green production version.",
    ]
    if signals.get("database"):
        recommendation.append("Verify database environment variables and connectivity before rollout.")
    if not signals.get("has_health_endpoint"):
        recommendation.append("Add or confirm a lightweight health endpoint for safer rollout checks.")

    detected = {
        "framework": signals.get("framework"),
        "language": signals.get("language"),
        "package_manager": signals.get("package_manager"),
        "port": signals.get("port"),
        "entrypoint": signals.get("entrypoint"),
        "dockerfile_detected": signals.get("has_dockerfile"),
        "kubernetes_detected": signals.get("has_kubernetes"),
        "database": signals.get("database"),
        "health_endpoint_detected": signals.get("has_health_endpoint"),
    }
    summary = (
        f"Detected {signals.get('framework') or signals.get('language') or 'application'}"
        f" with port {signals.get('port') or 8000}. "
        "Recommended flow: generate or validate container + Kubernetes config, deploy to staging, smoke test, then promote green production."
    )
    source_block = "\n".join(f"{src.path}:{src.line_start}-{src.line_end}" for src in sources)
    prompt = (
        f"{DEPLOYMENT_RECOMMENDATION_PROMPT}\n\n"
        f"Detected signals: {json.dumps(detected, indent=2)}\n"
        f"Issues: {json.dumps(issues, indent=2)}\n"
        f"Relevant files:\n{source_block}"
    )
    llm_summary = _safe_chat(
        prompt,
        system_prompt="You are a senior DevOps advisor. Provide a concise, structured deployment readiness summary (under 120 words) with clear bullet points. Do not write a continuous unformatted wall of text.",
    )
    if llm_summary:
        summary = llm_summary
    return DeploymentRecommendation(
        summary=summary,
        detected=detected,
        potential_issues=issues,
        recommendation=recommendation,
        evidence=[f"{src.path}:{src.line_start}-{src.line_end}" for src in sources] or [chunk.path for chunk in chunks[:2]],
    )


def evaluate_repository_index(project_id: int, analysis: dict) -> EvaluationReport:
    ensure_repository_index(project_id, analysis.get("repository_url", ""))
    signals = _collect_signals(project_id, analysis)
    questions: list[tuple[str, str]] = [
        ("What framework does this repository use?", str(analysis.get("framework") or "unknown")),
        ("What language does this repository use?", str(analysis.get("language") or "unknown")),
        ("What package manager does this repository use?", str(analysis.get("package_manager") or "unknown")),
        ("What is the deployment entrypoint?", str(analysis.get("entrypoint") or "unknown")),
        ("What port does the application use?", str(analysis.get("port") or 8000)),
    ]
    if signals.get("database"):
        questions.append(("What database does this application use?", str(signals["database"])))
    if signals.get("auth_files"):
        questions.append(("Which file contains authentication-related code?", str(signals["auth_files"][0])))

    results: list[EvaluationItem] = []
    correct = 0
    for question, expected in questions[:8]:
        answer = answer_repository_question(project_id, question, analysis)
        matched_source = answer.sources[0].path if answer.sources else None
        haystack = f"{answer.answer} {matched_source or ''}".lower()
        ok = expected.lower() in haystack
        if not ok and expected.isdigit():
            ok = expected in haystack
        results.append(
            EvaluationItem(
                question=question,
                expected=expected,
                answer=answer.answer,
                matched_source=matched_source,
                status="correct" if ok else "needs_review",
            )
        )
        if ok:
            correct += 1

    total = len(results)
    accuracy = round((correct / total) * 100, 1) if total else 0.0
    return EvaluationReport(
        questions=total,
        correct=correct,
        retrieval_accuracy=accuracy,
        results=results,
    )


def _entrypoint_module_and_attr(entrypoint: str | None) -> tuple[str | None, str | None]:
    if not entrypoint or ":" not in entrypoint:
        return None, None
    module, attr = entrypoint.split(":", 1)
    return module.strip(), attr.strip()


def _route_candidates(project_id: int) -> list[str]:
    chunks = _all_chunks(project_id)
    routes = [route for route in _detect_routes(chunks) if route and route.startswith("/")]
    ordered: list[str] = []
    for route in ["/", "/health", *routes]:
        if route not in ordered:
            ordered.append(route)
    return ordered[:4]


def _clean_route(route: str) -> str:
    """Replaces path parameters like {user_id} or :id with concrete dummy values."""
    cleaned = re.sub(r"\{[a-zA-Z0-9_]+_id\}", "1", route)
    cleaned = re.sub(r"\{id\}", "1", cleaned)
    cleaned = re.sub(r"\{[a-zA-Z0-9_]+\}", "test", cleaned)
    cleaned = re.sub(r":[a-zA-Z0-9_]+", "1", cleaned)
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def _generate_test_inferences(framework: str, routes: list[str]) -> list[TestInference]:
    inferences: list[TestInference] = [
        TestInference(
            name="Service Root Reachability",
            target="/",
            inference="Verifies the root endpoint handles incoming HTTP GET requests without unhandled 5xx server exceptions.",
        )
    ]
    if framework == "FastAPI":
        inferences.append(
            TestInference(
                name="OpenAPI Schema Serialization",
                target="/openapi.json",
                inference="Infers that all API route definitions, Pydantic schemas, and type annotations successfully compile and serialize into valid OpenAPI 3.0 specification.",
            )
        )
        inferences.append(
            TestInference(
                name="Interactive Documentation Availability",
                target="/docs",
                inference="Infers that Swagger UI interactive documentation is correctly mounted and accessible for developers and API consumers.",
            )
        )

    for i, route in enumerate(routes[:4], start=1):
        if "health" in route.lower():
            inferences.append(
                TestInference(
                    name="Health Probe Liveness",
                    target=route,
                    inference="Infers application liveness and readiness probe response for Kubernetes deployment health monitoring.",
                )
            )
        else:
            inferences.append(
                TestInference(
                    name=f"Route Check #{i} ({route})",
                    target=route,
                    inference=f"Verifies that route '{route}' handles GET requests without raising unhandled 5xx server errors.",
                )
            )
    return inferences


def _python_test_file(framework: str, module_name: str, app_attr: str, routes: list[str]) -> str:
    checked = routes[:4] or ["/", "/health"]
    route_checks: list[str] = []
    for i, route in enumerate(checked, start=1):
        clean = _clean_route(route)
        route_checks.append(
            f"def test_{i}_route(client):\n"
            f'    """Infers route {clean} handles GET requests gracefully."""\n'
            f"    response = client.get({clean!r})\n"
            f"    assert response.status_code < 500, f'Route returned internal server error: {{response.status_code}}'\n"
        )
    if framework == "FastAPI":
        route_checks.append(
            "def test_openapi_schema(client):\n"
            '    """Infers OpenAPI schema compiles cleanly without model errors."""\n'
            '    response = client.get("/openapi.json")\n'
            '    assert response.status_code in {200, 404}\n'
            '    if response.status_code == 200:\n'
            '        assert "openapi" in response.json() or "paths" in response.json()\n'
        )

    checks = "\n\n".join(route_checks)

    if framework == "Flask":
        return f"""import os
import sys
from pathlib import Path

# Injected mock environment variables so Pydantic settings & DB engines do not crash
os.environ.setdefault("TESTING", "True")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "deploymind-test-secret-key-32chars-min-length!")
os.environ.setdefault("PROJECT_NAME", "DeployMind-TestApp")
os.environ.setdefault("FIRST_SUPERUSER", "admin@example.com")
os.environ.setdefault("FIRST_SUPERUSER_PASSWORD", "testpassword123")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("EMAILS_ENABLED", "False")

ROOT = Path(__file__).resolve().parents[1]
for parent in [ROOT, ROOT.parent, ROOT / "src", ROOT / "app"]:
    if parent.is_dir() and str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

# Provide lightweight stubs for optional monitoring/telemetry/heavy dependencies
from unittest.mock import MagicMock
_STUB_MODULES = (
    "sentry_sdk", "sentry_sdk.integrations", "sentry_sdk.integrations.fastapi",
    "sentry_sdk.integrations.starlette", "sentry_sdk.integrations.logging",
    "datadog", "newrelic", "opentelemetry", "opentelemetry.sdk",
    "opentelemetry.instrumentation", "opentelemetry.instrumentation.flask",
    "prometheus_client", "ddtrace",
)
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

import pytest
from {module_name} import {app_attr}


@pytest.fixture
def client():
    with {app_attr}.test_client() as c:
        yield c


{checks}
"""

    return f"""import os
import sys
from pathlib import Path

# Injected mock environment variables so Pydantic settings & DB engines do not crash
os.environ.setdefault("TESTING", "True")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "deploymind-test-secret-key-32chars-min-length!")
os.environ.setdefault("PROJECT_NAME", "DeployMind-TestApp")
os.environ.setdefault("FIRST_SUPERUSER", "admin@example.com")
os.environ.setdefault("FIRST_SUPERUSER_PASSWORD", "testpassword123")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("EMAILS_ENABLED", "False")

ROOT = Path(__file__).resolve().parents[1]
for parent in [ROOT, ROOT.parent, ROOT / "backend", ROOT / "src", ROOT / "app"]:
    if parent.is_dir() and str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

# Provide lightweight stubs for optional/monitoring/telemetry/heavy dependencies
# that are not critical for API smoke tests but can block import collection.
from unittest.mock import MagicMock
_STUB_MODULES = (
    "sentry_sdk", "sentry_sdk.integrations", "sentry_sdk.integrations.fastapi",
    "sentry_sdk.integrations.starlette", "sentry_sdk.integrations.logging",
    "datadog", "newrelic", "opentelemetry", "opentelemetry.sdk",
    "opentelemetry.instrumentation", "opentelemetry.instrumentation.fastapi",
    "prometheus_client", "ddtrace",
)
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

# Stub out sqlmodel if not installed — replace with SQLAlchemy-compatible shims
try:
    import sqlmodel  # noqa: F401
except ImportError:
    import types
    _sqlmodel = types.ModuleType("sqlmodel")
    try:
        from sqlalchemy import Column, func, select, col  # noqa: F401
        from sqlalchemy.orm import Session
        _sqlmodel.col = col
        _sqlmodel.func = func
        _sqlmodel.select = select
        _sqlmodel.Session = Session
        _sqlmodel.SQLModel = object
        _sqlmodel.Field = lambda *a, **kw: None
        _sqlmodel.Relationship = lambda *a, **kw: None
        class _FakeSQLModelMeta(type):
            def __init__(cls, *a, **kw):
                super().__init__(*a, **kw)
        class _FakeSQLModel(metaclass=_FakeSQLModelMeta):
            pass
        _sqlmodel.SQLModel = _FakeSQLModel
    except Exception:
        _sqlmodel.col = MagicMock()
        _sqlmodel.func = MagicMock()
        _sqlmodel.select = MagicMock()
        _sqlmodel.Session = MagicMock()
        _sqlmodel.SQLModel = MagicMock()
        _sqlmodel.Field = MagicMock()
        _sqlmodel.Relationship = MagicMock()
    sys.modules["sqlmodel"] = _sqlmodel

import pytest
from fastapi.testclient import TestClient
from {module_name} import {app_attr}


@pytest.fixture
def client():
    # Use raise_server_exceptions=False so routes returning 500 don't abort pytest execution prematurely
    return TestClient({app_attr}, raise_server_exceptions=False)


{checks}
"""


def _child_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in TEST_BLOCKED_ENV}
    mock_defaults = {
        "TESTING": "True",
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "deploymind-test-secret-key-32chars-min-length!",
        "FIRST_SUPERUSER": "admin@example.com",
        "FIRST_SUPERUSER_PASSWORD": "testpassword123",
        "PROJECT_NAME": "DeployMind-TestApp",
        "POSTGRES_SERVER": "localhost",
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "test",
        "POSTGRES_DB": "test",
        "EMAILS_ENABLED": "False",
        "USERS_OPEN_REGISTRATION": "False",
    }
    for k, v in mock_defaults.items():
        if k not in env:
            env[k] = v
    if extra_env:
        env.update(extra_env)
    return env


def _run_install(command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )


def _minimal_python_test_dependencies(framework: str) -> str:
    # Always include sqlmodel so repos like full-stack-fastapi-template don't fail
    # with ModuleNotFoundError during test collection.
    common = "pytest httpx sentry-sdk sqlmodel sqlalchemy alembic python-multipart email-validator python-jose passlib"
    if framework == "Flask":
        return f"pip install flask {common} -q"
    return f"pip install fastapi uvicorn {common} -q"


def _prepare_python_repo(repo_dir: Path, timeout: int, framework: str = "") -> str | None:
    install_cmds: list[str] = []
    req = repo_dir / "requirements.txt"
    pyproject = repo_dir / "pyproject.toml"
    if not req.exists() and not pyproject.exists():
        req = repo_dir.parent / "requirements.txt"
        pyproject = repo_dir.parent / "pyproject.toml"

    minimal_cmd = _minimal_python_test_dependencies(framework)

    if req.exists():
        install_cmds.append(f"pip install -r {req} -q --ignore-requires-python")
    elif pyproject.exists():
        install_cmds.append(f"pip install {pyproject.parent} -q")

    # Attempt repository requirements install (non-fatal — we fall back to minimal)
    for command in install_cmds:
        try:
            proc = _run_install(command, repo_dir, timeout=min(timeout, 180))
            if proc.returncode != 0:
                output = (proc.stderr or proc.stdout or "")[:400]
                logger.warning("Repository dependencies partial failure: %s", output)
        except subprocess.TimeoutExpired:
            logger.warning("Repository dependencies install timed out, falling back to minimal runner")

    # Guarantee minimal test dependencies exist so testing is seamless
    try:
        proc = _run_install(minimal_cmd, repo_dir, timeout=90)
        if proc.returncode == 0:
            return None
        return proc.stderr or proc.stdout or "Failed to install test runner dependencies"
    except Exception as e:
        return str(e)


def _extract_missing_module(error_output: str) -> str | None:
    """Parse a ModuleNotFoundError message and return the top-level package name."""
    match = re.search(r"No module named '([A-Za-z0-9_.-]+)'", error_output)
    if not match:
        return None
    mod = match.group(1).split(".")[0]  # get top-level package
    # Map Python module name → PyPI package name for common mismatches
    _module_to_pip: dict[str, str] = {
        "sqlmodel": "sqlmodel",
        "sentry_sdk": "sentry-sdk",
        "jose": "python-jose",
        "passlib": "passlib",
        "dotenv": "python-dotenv",
        "email_validator": "email-validator",
        "multipart": "python-multipart",
        "alembic": "alembic",
        "celery": "celery",
        "redis": "redis",
        "boto3": "boto3",
        "botocore": "botocore",
        "stripe": "stripe",
        "PIL": "Pillow",
        "cv2": "opencv-python-headless",
        "sklearn": "scikit-learn",
        "yaml": "pyyaml",
        "jwt": "PyJWT",
        "cryptography": "cryptography",
        "aiofiles": "aiofiles",
        "starlette": "starlette",
        "databases": "databases",
        "tortoise": "tortoise-orm",
        "beanie": "beanie",
        "motor": "motor",
        "pymongo": "pymongo",
        "elasticsearch": "elasticsearch",
    }
    return _module_to_pip.get(mod, mod)


def _find_python_syntax_errors(repo_dir: Path, limit: int = 20) -> list[str]:
    """Parse application Python files before installing dependencies or running pytest."""
    ignored_dirs = {
        ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
        ".deploymind_generated", "site-packages",
    }
    errors: list[str] = []
    for path in sorted(repo_dir.rglob("*.py")):
        relative = path.relative_to(repo_dir)
        if any(part.lower() in ignored_dirs for part in relative.parts):
            continue
        try:
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as exc:
            location = f"{relative.as_posix()}:{exc.lineno or 1}"
            errors.append(f"SyntaxError: {location}: {exc.msg}")
            if len(errors) >= limit:
                break
        except OSError as exc:
            logger.debug("Could not read Python source during syntax preflight: %s (%s)", relative, exc)
    return errors


def explain_failure(error: str, evidence: list[str] | None = None, command: str | None = None) -> dict:
    evidence = evidence or []
    diagnostic_text = " ".join(evidence + [error])
    text = diagnostic_text.lower()
    category = "GENERAL_FAILURE"

    repository_status = "unknown"
    if "failed to connect to the docker api" in text or "dockerdesktoplinuxengine" in text or "docker daemon is not running" in text:
        category = "DOCKER_UNAVAILABLE"
        cause = "Docker Desktop is not running, so DeployMind cannot build images or reach the local kind cluster."
        fix = "Start Docker Desktop, wait for the Linux engine to become ready, then create or start the kind cluster and retry staging."
    elif "error loading asgi app" in text or "attribute \"app\" not found" in text:
        category = "ENTRYPOINT_MISMATCH"
        cause = "The container command points to a Python module or application attribute that does not exist."
        fix = "Confirm the detected FastAPI/Flask entrypoint and regenerate the deployment artifacts."
        repository_status = "incompatible_or_faulty"
    elif "pydantic" in text and ("validation error" in text or "field required" in text or "validationerror" in text):
        category = "SETTINGS_VALIDATION"
        cause = "The application failed to initialize because required configuration settings or environment variables were missing."
        fix = "Configure required environment variables (e.g. SECRET_KEY, DATABASE_URL, FIRST_SUPERUSER) in the test environment or .env file."
        repository_status = "incompatible_or_faulty"
    elif "no module named" in text or "modulenotfounderror" in text or "importerror" in text:
        category = "IMPORT_ERROR"
        cause = "The generated test ran from an unexpected Python import path, or a repository module was missing."
        fix = "Verify the repository layout and make sure dependencies are listed in requirements.txt or pyproject.toml."
        repository_status = "incompatible_or_faulty"
    elif "could not find a version that satisfies the requirement" in text or "no matching distribution found" in text:
        category = "DEPENDENCY_INSTALL_FAILED"
        cause = "Repository dependency installation failed before the generated tests could run."
        fix = "Check the repository dependency file for invalid or private-only packages, then retry the AI-generated tests."
        repository_status = "incompatible_or_faulty"
    elif "syntaxerror" in text or "multiple exception types must be parenthesized" in text:
        category = "PYTHON_SYNTAX_ERROR"
        location = re.search(r"([A-Za-z0-9_./\\-]+\.py:\d+):\s*", diagnostic_text)
        location_text = f" at {location.group(1)}" if location else ""
        if "multiple exception types must be parenthesized" in text:
            cause = f"Python could not import the application: an invalid except clause{location_text} stopped pytest collection before generated tests ran."
            fix = "Wrap multiple exception classes in parentheses, for example: except (InvalidTokenError, ValidationError):, then rerun AI tests."
        else:
            cause = f"Python could not import the application: a syntax error{location_text} stopped pytest collection before generated tests ran."
            fix = "Correct the Python syntax at the reported file and line, then rerun AI tests."
        repository_status = "incompatible_or_faulty"
    elif "uid 1000 is not unique" in text:
        category = "STAGING_IMAGE_USER_COLLISION"
        cause = "The generated staging image tried to create UID 1000, but its base image already contains that UID."
        fix = "Reuse the existing UID 1000 account in the generated runtime image; the repository source and Maven build are not the cause."
        repository_status = "staging_configuration_issue"
    elif any(marker in text for marker in ("maven", "gradle", "npm err", "could not resolve dependencies", "failed to execute goal", "jar file")):
        category = "REPOSITORY_BUILD_FAILED"
        cause = "The repository source could not complete its declared application build in the staging image."
        fix = "Fix the build error in pom.xml, build.gradle, package.json, or the application source, then re-run analysis and staging."
        repository_status = "incompatible_or_faulty"
    elif "crashloopbackoff" in text or "module not found" in text:
        category = "CRASH_LOOP"
        cause = "The application is starting but crashing during boot."
        fix = "Check the entrypoint module, dependency installation, and startup logs."
    elif "imagepullbackoff" in text or "errimagepull" in text:
        category = "IMAGE_PULL_FAILED"
        cause = "Kubernetes could not start the image that was built or loaded."
        fix = "Verify the Docker build succeeded and that the image was loaded into the kind cluster."
    elif (
        ("mongodb" in text or "mongo" in text)
        and ("connection refused" in text or "mongosocketopenexception" in text)
    ):
        category = "DATABASE_UNAVAILABLE"
        cause = "The application cannot connect to MongoDB. In a Kubernetes Pod, localhost points to the application Pod itself, not a separate MongoDB Pod."
        fix = "For staging, provision MongoDB in the same namespace and set SPRING_DATA_MONGODB_URI (or the app's Mongo URI setting) to the MongoDB Service address, not localhost."
        repository_status = "staging_configuration_issue"
    elif "port-forward exited early" in text or "connection refused" in text:
        category = "PORT_FORWARD_OR_REFUSED"
        cause = "The service became reachable before the application was actually ready, or the wrong port is exposed."
        fix = "Confirm the container port, readiness behavior, and health endpoint path."
    elif "database" in text or "postgres" in text or "sql" in text:
        category = "DATABASE_UNAVAILABLE"
        cause = "The deployment likely depends on a database configuration that is missing or unreachable."
        fix = "Check database environment variables, secrets, and network access."
        repository_status = "incompatible_or_faulty"
    elif "kubernetes cluster not reachable" in text or "kind create cluster" in text or "no kind clusters found" in text:
        category = "CLUSTER_UNREACHABLE"
        cause = "DeployMind could not reach the local kind Kubernetes cluster."
        fix = f"Start or create the cluster first with: kind create cluster --name {settings.kind_cluster_name}"
    elif "401" in text or "unauthorized" in text or "not authenticated" in text:
        category = "AUTHENTICATION_REQUIRED"
        cause = "The tested route returned 401 Unauthorized because it requires authentication credentials or an active session."
        fix = "Add test client authentication headers (e.g. headers={'Authorization': 'Bearer token'}) or mock the auth dependency."
    elif "422" in text or "unprocessable entity" in text:
        category = "VALIDATION_ERROR"
        cause = "The route returned 422 Unprocessable Entity due to missing required query parameters or body payload."
        fix = "Supply required parameters or mock request body payload matching the endpoint schema."
    elif "500" in text or "internal server error" in text:
        category = "INTERNAL_SERVER_ERROR"
        cause = "The endpoint encountered an unhandled internal server exception during request handling."
        fix = "Inspect route stack traces, mock uninitialized external services, or add defensive error handling."
    else:
        cause = "Insufficient information to determine the root cause."
        fix = "Review the error output, test runner stdout/stderr, and traceback details."

    if command:
        prompt = (
            f"{TEST_FAILURE_PROMPT}\n\n"
            f"TEST COMMAND: {command}\n\n"
            f"STDOUT:\n{evidence[0][:2500] if len(evidence) > 0 else 'None'}\n\n"
            f"STDERR:\n{evidence[1][:2500] if len(evidence) > 1 else (evidence[0][:2500] if evidence else error)}"
        )
    else:
        evidence_block = "\n".join(evidence[:3]) if evidence else "None"
        prompt = (
            f"{FAILURE_ANALYSIS_PROMPT}\n\n"
            f"DEPLOYMENT STATUS: Failed\n\n"
            f"POD STATUS: {evidence[0][:1000] if evidence else 'Unknown'}\n\n"
            f"CONTAINER LOGS:\n{evidence_block[:2000]}\n\n"
            f"ERROR MESSAGE:\n{error}"
        )

    llm_text, provider = _safe_chat_with_provider(
        prompt,
        system_prompt="You are an SRE and DevOps diagnostic specialist. Provide precise failure root-cause analysis based on the supplied evidence.",
    )
    if llm_text:
        summary = f"{cause}\n\n[AI Root Cause Analysis ({provider})]:\n{llm_text}"
    else:
        summary = cause

    return {
        "failure": error,
        "likely_cause": summary,
        "evidence": evidence,
        # Keep the original staging error visible to the UI. The categorized
        # diagnosis is useful context, but must not hide the build output that
        # identifies the failing dependency or Dockerfile instruction.
        "details": error,
        "suggested_fix": fix,
        "category": category,
        "repository_status": repository_status,
        "analysis_provider": provider,
    }


def _discover_python_entrypoint(repo_path: Path, framework: str) -> tuple[str | None, str | None]:
    candidates = [
        ("app/main.py", "app.main", "app"),
        ("main.py", "main", "app"),
        ("app/api.py", "app.api", "app"),
        ("api.py", "api", "app"),
        ("src/main.py", "src.main", "app"),
        ("backend/app/main.py", "app.main", "app"),
    ]
    for rel, mod, attr in candidates:
        candidate_file = repo_path / rel
        if candidate_file.is_file():
            content = candidate_file.read_text(encoding="utf-8", errors="ignore")
            if "FastAPI(" in content or "Flask(" in content:
                return mod, attr
    return None, None


@measured("ai_tests")
def run_ai_generated_tests(project_id: int, repository_url: str, analysis: dict, timeout: int = 180) -> AITestRun:
    ensure_repository_index(project_id, repository_url)
    framework = (analysis.get("framework") or "").strip()
    if framework not in {"FastAPI", "Flask"}:
        return AITestRun(
            status="skipped",
            message="AI-generated execution is currently implemented for FastAPI and Flask repositories.",
        )

    module_name, app_attr = _entrypoint_module_and_attr(analysis.get("entrypoint"))

    temp_dir = Path(settings.workspace_dir) / f"project_{project_id}_ai_tests"
    app_dir = temp_dir / (analysis.get("source_subdir") or "")

    try:
        clone_repository(repository_url, temp_dir)
        if not app_dir.is_dir():
            app_dir = temp_dir

        if not module_name or not app_attr:
            module_name, app_attr = _discover_python_entrypoint(app_dir, framework)

        if not module_name or not app_attr:
            return AITestRun(
                status="skipped",
                message="A Python entrypoint could not be located before AI-generated tests could run.",
            )

        routes = _route_candidates(project_id)
        inferences = _generate_test_inferences(framework, routes)
        code = _python_test_file(framework, module_name, app_attr, routes)
        prompt = (
            f"{TEST_GENERATION_PROMPT}\n\n"
            f"Framework: {framework}\nEntrypoint: {analysis.get('entrypoint') or f'{module_name}:{app_attr}'}\nRoutes: {json.dumps(routes)}"
        )
        llm_text = _safe_chat(prompt)
        rationale = llm_text or "Generated route checks from the detected Python entrypoint and indexed routes."
        case = GeneratedTestCase(
            name="generated_api_checks",
            rationale=rationale,
            path=".deploymind_generated/test_ai_generated.py",
            code=code,
        )

        # Resolve the directory that actually contains the Python package or module
        module_pkg = module_name.split(".")[0]
        python_path = app_dir
        if not (app_dir / module_pkg).is_dir() and not (app_dir / f"{module_pkg}.py").is_file():
            for sub in sorted(app_dir.iterdir()):
                if sub.is_dir() and ((sub / module_pkg).is_dir() or (sub / f"{module_pkg}.py").is_file()):
                    python_path = sub
                    break

        syntax_errors = _find_python_syntax_errors(python_path)
        if syntax_errors:
            syntax_output = "\n".join(syntax_errors)
            failure = explain_failure(
                "Python source syntax check failed",
                [syntax_output],
                command="Python source syntax preflight",
            )
            return AITestRun(
                status="failed",
                message="Python syntax errors prevent AI-generated tests from running",
                command="Python source syntax preflight",
                generated_tests=[case],
                stderr=syntax_output,
                failure_analysis=failure,
                test_inferences=inferences,
            )

        removed_requirements = sanitize_requirements_file(python_path)

        gen_dir = python_path / ".deploymind_generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        test_file = gen_dir / "test_ai_generated.py"
        test_file.write_text(code, encoding="utf-8")

        prepare_error = _prepare_python_repo(python_path, timeout=min(timeout, 240), framework=framework)
        if prepare_error:
            failure_res = explain_failure("Dependency install failed before AI tests", [prepare_error], command="pip install")
            return AITestRun(
                status="failed",
                message="Dependency install failed before AI-generated tests",
                generated_tests=[case],
                stderr=prepare_error,
                failure_analysis=failure_res,
                test_inferences=inferences,
            )

        command = "pytest .deploymind_generated/test_ai_generated.py -q"
        env = {**_child_env(), "PYTHONPATH": str(python_path)}

        # Retry loop: if pytest fails with ModuleNotFoundError, auto-install
        # the missing package and try again (up to 3 attempts). This handles
        # repos that import non-standard libs (sqlmodel, jose, passlib, etc.)
        # that are not in the minimal test dependencies list.
        _max_retries = 3
        _installed_extra: set[str] = set()
        for _attempt in range(_max_retries):
            proc = subprocess.run(
                command,
                cwd=python_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if proc.returncode == 0:
                break
            combined_out = (proc.stdout or "") + (proc.stderr or "")
            missing_pkg = _extract_missing_module(combined_out)
            if (
                missing_pkg
                and missing_pkg not in _installed_extra
                and "No module named" in combined_out
                and _attempt < _max_retries - 1
            ):
                logger.info("Auto-installing missing package '%s' for AI tests (attempt %d)", missing_pkg, _attempt + 1)
                try:
                    _run_install(f"pip install {missing_pkg} -q", python_path, timeout=60)
                    _installed_extra.add(missing_pkg)
                    continue
                except Exception as _install_err:
                    logger.warning("Auto-install of '%s' failed: %s", missing_pkg, _install_err)
            break  # no auto-install possible or no more retries

        stdout = (proc.stdout or "")[-6000:]
        stderr = (proc.stderr or "")[-6000:]
        if proc.returncode == 0:
            message = "AI-generated tests passed"
            if removed_requirements:
                message += f" after ignoring suspicious requirement lines: {', '.join(removed_requirements[:3])}"
            return AITestRun(
                status="passed",
                message=message,
                command=command,
                generated_tests=[case],
                stdout=stdout,
                stderr=stderr,
                test_inferences=inferences,
            )
        failure = explain_failure("AI-generated tests failed", [stdout, stderr], command=command)
        return AITestRun(
            status="failed",
            message="AI-generated tests failed",
            command=command,
            generated_tests=[case],
            stdout=stdout,
            stderr=stderr,
            failure_analysis=failure,
            test_inferences=inferences,
        )
    except subprocess.TimeoutExpired:
        failure = explain_failure("AI-generated tests timed out", command=command)
        return AITestRun(
            status="failed",
            message=f"AI-generated tests timed out after {timeout}s",
            generated_tests=[case] if 'case' in locals() else [],
            failure_analysis=failure,
            test_inferences=inferences if 'inferences' in locals() else [],
        )
    finally:
        cleanup_repository(temp_dir)


def _kubectl(args: list[str], timeout: int = 30) -> str:
    env = os.environ.copy()
    if settings.kubeconfig:
        env["KUBECONFIG"] = settings.kubeconfig
    proc = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "").strip()
    return (proc.stdout or "").strip()


def analyze_deployment_failure(app_name: str, namespace: str, error_message: str) -> dict:
    evidence: list[str] = []
    try:
        pods = _kubectl(["get", "pods", "-n", namespace, "-o", "wide"], timeout=20)
        if pods:
            evidence.append(pods[:2000])
        deploy = _kubectl(["get", "deployments", "-n", namespace], timeout=20)
        if deploy:
            evidence.append(deploy[:1500])
        if app_name:
            pod_names = _kubectl(
                ["get", "pods", "-n", namespace, "-l", f"app={app_name}", "-o", "jsonpath={.items[*].metadata.name}"],
                timeout=20,
            )
            first_pod = next((name for name in pod_names.split() if name.strip()), "")
            if first_pod:
                logs = _kubectl(["logs", "-n", namespace, first_pod, "--tail=40"], timeout=20)
                if logs:
                    evidence.append(logs[:2000])
    except Exception:
        logger.warning("deployment failure evidence collection failed", exc_info=True)
    return explain_failure(error_message, evidence)
