# Hybrid repository search

## Pipeline

CodeDeck indexes readable, bounded-size source and configuration files after repository analysis. It skips VCS directories, virtual environments, dependency folders, generated builds and common IDE caches. Text is divided into overlapping chunks with stable SHA-256 IDs derived from path, line range and content. Each chunk stores its repository-relative path and source line range in the per-project JSON index under `WORKSPACE_DIR/generated/project_<id>/repository_index.json`.

The primary retrieval path combines:

1. Local dense embeddings from FastEmbed `BAAI/bge-small-en-v1.5` (384 dimensions; ONNX Runtime).
2. BM25 Okapi lexical ranking over source path and content, which preserves exact symbol/configuration matching.
3. Reciprocal Rank Fusion with `k=60`, followed by stable chunk-ID deduplication.

The semantic model is downloaded on first indexing when `SEMANTIC_EMBEDDINGS_ENABLED=true`. It is local inference and requires no hosted embedding API, but needs network access once and disk space for the model. Semantic candidates below `SEMANTIC_MIN_SIMILARITY` (default `0.35`) are excluded. If the dependency or model is unavailable, indexing records that semantic vectors are unavailable and Q&A reports/uses BM25-only retrieval. It does not label the fallback as hybrid. Set `SEMANTIC_EMBEDDINGS_ENABLED=false` for deterministic unit tests or intentionally lexical-only local use.

The previous 192-dimensional token-hash vectors are retained only by the evaluation baseline helper. They are not used as the application's primary semantic retriever.

## Index freshness and scope

Indexes are stored under a numeric project-specific directory, include the normalized repository URL and index version, and are reused across Q&A. Re-analysis rebuilds the index from the newly cloned snapshot; matching chunk IDs reuse previous embeddings when the repository URL and model match. An index with an older schema or another repository URL is rebuilt. Unchanged content is not fetched from GitHub on every question. Run repository analysis again to pick up remote changes. Writes use a temporary file followed by atomic replacement.

Repository chunk lists are loaded only by that project's index path. Empty or missing indexes fail as empty/insufficient evidence; they are not mixed with other projects.

## Grounded answers

The LLM receives the user question and retrieved snippets with actual `[path:line-line]` labels. Its system instructions require factual claims to cite those labels and prohibit invented paths or line ranges. No relevant lexical or semantic result produces an explicit insufficient-evidence answer without calling an LLM. The API response includes the actual retrieval method, result count, latency, and evidence objects. LLM fallback is also constrained to cite a retrieved source.

## Reproducible evaluation

The checked-in sample corpus and questions are under `backend/evals/`. Run:

```powershell
cd backend
..\.venv\Scripts\python.exe scripts/evaluate_retrieval.py
```

The command reports Recall@3, HitRate@3, MRR@3, and measured local latency for legacy hash baseline, BM25, semantic and hybrid rankings. Unanswerable examples are excluded from ranking denominators and remain visible in the total question count. A semantic ranking is marked unavailable when this machine lacks the embedding model; no score is fabricated. Unit tests cover exact symbol lookup, BM25 ranking, RRF fusion, deduplication, project isolation and no lexical evidence.

One actual run on the checked-in three-file fixture (four answerable questions, FastEmbed model available, Windows CPU) reported:

| Retriever | Recall@3 | MRR@3 | Mean latency |
|---|---:|---:|---:|
| Legacy hash baseline | 1.00 | 0.708 | 1.401 ms |
| BM25 | 0.50 | 0.500 | 0.142 ms |
| Semantic | 1.00 | 1.000 | 37.875 ms |
| Hybrid RRF | 1.00 | 1.000 | 40.768 ms |

These small-fixture scores are a regression signal, not a general quality claim. In this run semantic and hybrid had equal MRR; hybrid improved BM25 recall. Latency includes model inference, not the initial model download, and can vary with CPU load. The recorded run was repeated after the retrieval changes on 2026-10-29. Re-run the command on the same machine to measure its current state.
