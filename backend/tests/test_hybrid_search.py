import json

from app.services import repository_ai as rag


def _chunk(chunk_id: str, path: str, content: str, start: int = 1, embedding=None):
    return rag.RepositoryChunk(
        chunk_id=chunk_id,
        path=path,
        content=content,
        line_start=start,
        line_end=start + 3,
        embedding=embedding or [],
    )


def test_bm25_ranks_exact_code_symbol_and_environment_name():
    chunks = [
        _chunk("a", "app/main.py", "def start_server(): return app"),
        _chunk("b", "settings.py", "DATABASE_URL configures the database"),
        _chunk("c", "README.md", "The project runs an HTTP service"),
    ]

    assert rag._bm25_rank(chunks, "DATABASE_URL") == [1]
    assert rag._bm25_rank(chunks, "start_server") == [0]


def test_hybrid_search_fuses_and_deduplicates_by_stable_chunk_id(tmp_path, monkeypatch):
    monkeypatch.setattr(rag.settings, "workspace_dir", str(tmp_path))
    chunks = [
        _chunk("a", "src/api.py", "def lookup_customer(customer_id): return customers[customer_id]", 1, [1.0, 0.0]),
        _chunk("b", "src/service.py", "The customer lookup fetches one customer by its identifier", 8, [0.0, 1.0]),
        _chunk("c", "README.md", "This repository contains a small web application", 1, [0.0, 0.0]),
    ]
    path = rag._index_path(71)
    path.write_text(json.dumps({"chunks": [chunk.model_dump() for chunk in chunks]}), encoding="utf-8")
    monkeypatch.setattr(rag, "_semantic_rank", lambda _chunks, _query: [1, 0])

    hits = rag.search_repository(71, "lookup_customer")

    assert hits[0].path == "src/api.py"  # reciprocal ranks combine both retrieval lists
    assert {hit.path for hit in hits[:2]} == {"src/api.py", "src/service.py"}
    assert all(hit.retrieval_method == "hybrid" for hit in hits)
    assert len({(hit.path, hit.line_start) for hit in hits}) == len(hits)


def test_search_is_repository_scoped_and_empty_when_no_lexical_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(rag.settings, "workspace_dir", str(tmp_path))
    first = rag._index_path(72)
    first.write_text(json.dumps({"chunks": [_chunk("x", "private.py", "class PrivateService: pass").model_dump()]}), encoding="utf-8")
    monkeypatch.setattr(rag, "_semantic_rank", lambda _chunks, _query: [])

    assert rag.search_repository(73, "PrivateService") == []
    assert rag.search_repository(72, "nonexistent configuration term") == []
    assert rag.search_repository(72, "Is Redis configured in this repository?") == []


def test_answer_citations_are_checked_against_retrieved_sources():
    source = rag.SearchResult(
        path="app/main.py",
        snippet="app = FastAPI()",
        score=0.02,
        line_start=4,
        line_end=8,
    )
    answer = rag._ground_answer_citations("FastAPI is initialized here [fake.py:1-3].", [source])
    assert "fake.py" not in answer
    assert "[app/main.py:4-8]" in answer


def test_semantic_results_below_configured_relevance_threshold_are_discarded(monkeypatch):
    class FakeModel:
        def query_embed(self, _texts):
            return [[0.1, 0.0]]

    monkeypatch.setattr(rag, "_get_embedding_model", lambda: FakeModel())
    monkeypatch.setattr(rag.settings, "semantic_min_similarity", 0.35)
    chunks = [_chunk("x", "src/module.py", "A module with unrelated content", embedding=[1.0, 0.0])]

    assert rag._semantic_rank(chunks, "unrelated request") == []

