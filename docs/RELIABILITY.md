# Reliability and recovery

## Health and dependencies

- `/health/live`: process is serving HTTP.
- `/health/ready`: database `SELECT 1`; 503 means CodeDeck cannot serve normal persisted workflows.
- `/api/system/status`: Docker, kind, kubectl, database and LLM configuration diagnostics.
- `/api/system/integrations`: optional Langfuse and Prometheus instrumentation status.

Database connection attempts are bounded by `DATABASE_CONNECT_TIMEOUT` (default 5 seconds). GitHub clones use `REPOSITORY_CLONE_TIMEOUT` (default 300 seconds) and remove partial checkouts on failure. OpenAI-compatible provider requests use `LLM_TIMEOUT_SECONDS` (default 45 seconds) and at most one SDK retry before the configured provider fallback. Database sessions roll back when request handling raises and always close. Optional Langfuse, Prometheus and semantic embedding failures do not prevent API startup; semantic search degrades explicitly to BM25 if the local model is unavailable.

## Timeouts, retries and cancellation

Repository test execution and Docker/Kubernetes subprocesses use operation-specific timeouts. The OpenAI-compatible SDK owns a bounded retry policy; provider fallback occurs only after errors, and Groq model alternatives are attempted only for model-not-found responses. Invalid requests and configuration are not retried.

Repository analysis, Q&A and AI test generation currently use synchronous API handlers; staging and production deployment use the existing FastAPI background task path with database-persisted progress. The frontend polls serially every three seconds and stops after 30 minutes with an explicit message rather than silently loading forever. The local staging helper similarly has a configurable timeout. Cancellation remains cooperative at supported build/rollout checkpoints; Kubernetes resource cleanup may continue after the request records cancellation.

## Common recovery

- Database unavailable: start `db` with `docker compose up -d db`, wait for its health check, then verify `/health/ready`.
- LLM outage: confirm provider configuration without sharing keys; Q&A reports the actual heuristic fallback provider when retrieval exists.
- Missing/old repository index: rerun repository analysis to clone a fresh snapshot and rebuild the versioned index.
- Semantic model unavailable: verify outbound model download access and disk space; BM25 remains available and the API response identifies the actual retrieval method.
- Docker build failure: inspect the staging pipeline's real Docker output and build context; check Docker daemon and disk before retry.
- Kubernetes rollout failure: inspect pod events, previous/current container logs, probes, service names, environment variables and dependency service DNS. Do not treat `kubectl apply` alone as deployment success.
- Cancel still cleaning: cancellation is cooperative; inspect the active staging step and namespace resources. The frontend stops its polling only after terminal state or 30 minutes.
