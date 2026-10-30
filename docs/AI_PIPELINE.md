# CodeDeck — AI & RAG Pipeline

This document details the core artificial intelligence and Retrieval-Augmented Generation (RAG) pipelines powering CodeDeck's intelligent features. The pipeline transforms raw codebase data into actionable insights, automated testing, and deployment guidance.

```text
┌───────────────┐     ┌───────────────┐     ┌────────────────┐
│   User Repo   │────▶│ Deterministic │────▶│ Repository     │
│   (Source)    │     │   Analysis    │     │ Indexing (RAG) │
└───────────────┘     └───────────────┘     └────────────────┘
                                                    │
                                                    ▼
┌───────────────┐     ┌───────────────┐     ┌────────────────┐
│ AI Test       │◀────│ Deployment    │◀────│ Semantic       │
│ Generation    │     │ Generation    │     │ Retrieval      │
└───────────────┘     └───────────────┘     └────────────────┘
        │
        ▼
┌───────────────┐     ┌───────────────┐     ┌────────────────┐
│ Pipeline      │────▶│ AI Failure    │────▶│ Evaluation     │
│ Execution     │     │ Analysis      │     │ Mechanism      │
└───────────────┘     └───────────────┘     └────────────────┘
```

## 1. Deterministic Repository Analysis

Before engaging external AI models, CodeDeck performs a rapid, deterministic pass over the target repository. This step uses heuristics to extract hard truths about the codebase without incurring API latency or costs.

*   **Language Detection:** Scans for standard file extensions (e.g., `.py`, `.js`, `.go`).
*   **Framework Detection:** Inspects dependency files (`requirements.txt`, `package.json`, `go.mod`) for known framework signatures (FastAPI, Flask, Express, React).
*   **Port Detection:** Utilizes regex matching on entrypoint files to identify listening ports (e.g., `app.run(port=8080)`).
*   **Entrypoint Detection:** Identifies the primary execution script (e.g., `main.py`, `app.js`).

These signals serve as a reliable fallback and provide highly structured context for subsequent LLM prompts.

## 2. Repository Indexing

To provide the LLM with relevant context from the repository, CodeDeck builds a searchable local index.

1.  **File Selection:** The system filters out non-text files, binaries, and ignored directories (e.g., `node_modules`, `.git`, `.venv`) to ensure only relevant source code and documentation are processed.
2.  **Chunking:** Files are divided into smaller segments. CodeDeck uses a chunk size of approximately 1400 characters with an overlap of 220 characters. The overlap ensures that context isn't lost if an important function or thought spans across a chunk boundary.
3.  **Vectorization:** When enabled, CodeDeck uses FastEmbed with the local `BAAI/bge-small-en-v1.5` model (384-dimensional dense vectors). It downloads the model on first use and runs inference locally. If it is unavailable, the index remains usable with BM25 lexical search and marks semantic retrieval unavailable.
4.  **Stable metadata:** Chunks have stable content/path/line-derived IDs, repository-relative paths, source line ranges, a corpus fingerprint and index version. Per-project indexes are atomically persisted under `WORKSPACE_DIR/generated/project_<id>/repository_index.json`.

## 3. Semantic Retrieval

When a user asks a question or a process requires context, CodeDeck searches the generated index.

*   The question is embedded with the same local model as the repository chunks when vectors are available.
*   A genuine Okapi BM25 ranker independently finds exact symbols, package names, configuration keys and code terms.
*   Reciprocal Rank Fusion combines the semantic and lexical ranks, and stable chunk IDs prevent duplicate context. Semantic candidates below `SEMANTIC_MIN_SIMILARITY` are excluded.

## 4. AI Q&A Workflow

The RAG workflow answers user queries contextually.

1.  **Context Retrieval:** Relevant chunks are fetched through the reported hybrid, semantic-only or BM25-only path.
2.  **Prompt Assembly:** The system constructs a strict prompt (referencing strategies defined in `prompts.py`). It injects the user's question and the retrieved codebase chunks.
3.  **LLM Execution:** CodeDeck tries the configured OpenAI-compatible provider chain (OpenAI, Groq, xAI) with a bounded timeout and one SDK retry.
4.  **Grounding:** The prompt requires facts to cite retrieved file paths and line ranges. Citations are checked against the returned evidence; unsupported paths/line numbers are removed.
5.  **Fallback Mechanism:** If no provider is configured or all attempts fail, CodeDeck produces a labeled grounded heuristic answer only when retrieved evidence exists. With no relevant evidence it returns an explicit insufficient-context response without calling an LLM.

## 5. Deployment Recommendation

CodeDeck leverages AI to determine the optimal deployment strategy.

*   The deterministic signals (language, framework, ports) and relevant file chunks (like `Dockerfile` if it exists, or `requirements.txt`) are passed to the LLM.
*   The LLM is prompted to output deployment guidance, specifically identifying missing infrastructure components (like generating a missing Dockerfile or standard Kubernetes manifests).

## 6. AI Test Generation

CodeDeck automates testing to validate deployment stability.

1.  **Route Detection:** Heuristics or AI identify API routes in the codebase.
2.  **Generation Prompt:** The LLM receives the route definitions and is instructed to generate testing code (specifically utilizing the `pytest` framework for Python projects).
3.  **Execution Environment:** The generated tests are saved to disk (e.g., `test_app.py`).
4.  **Subprocess Execution:** CodeDeck invokes a secure subprocess to run the `pytest` command. *The LLM does not execute the code.*
5.  **Parsing:** The standard output and standard error from the test runner are captured for analysis.

## 7. Deployment Failure Analysis

When an infrastructure task fails, AI accelerates the debugging process.

*   **Trigger:** A Docker build failure or a Kubernetes Pod crash (e.g., CrashLoopBackOff).
*   **Data Collection:** CodeDeck orchestrates the collection of raw error data, capturing Docker build logs or retrieving Pod logs via `kubectl logs`.
*   **Analysis Prompt:** The error trace, along with relevant codebase context (like the entrypoint script or Dockerfile), is sent to the LLM.
*   **Resolution:** The LLM responds with a root cause analysis and a proposed fix, significantly reducing Mean Time To Resolution (MTTR).

## 8. Evaluation Mechanism

To ensure the reliability of the RAG pipeline, CodeDeck includes a built-in evaluation protocol.

*   The built-in project evaluation checks common repository questions against indexed evidence.
*   `backend/scripts/evaluate_retrieval.py` compares the legacy hash baseline, BM25, semantic and RRF hybrid rankers on a checked-in fixture and reports Recall@3, HitRate@3, MRR@3 and measured latency.
*   The fixture metrics are small regression checks, not a claim of general answer quality. See [Hybrid Search](HYBRID_SEARCH.md) for the actual latest recorded run and limitations.

## Future Extension Points

*   **Agentic Workflows:** Allowing the AI not just to recommend fixes, but to execute code edits and retry failed deployments autonomously.
