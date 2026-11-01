# CodeDeck — AI-Assisted Software Development & DevOps Platform

CodeDeck is an integrated platform that accelerates software development by combining AI-driven code analysis, intelligent RAG Q&A, and automated Kubernetes deployments.

## Core Workflow

```text
GitHub Repo -> Analysis -> RAG Q&A -> AI Testing -> Docker Build -> Kubernetes Deploy -> Smoke Test -> Results
```

## Technology Stack

| Domain | Technologies |
|--------|--------------|
| **Backend** | FastAPI, Python, PostgreSQL (SQLAlchemy), LangGraph |
| **Frontend** | React 19, TypeScript, Vite, Tailwind CSS v4 |
| **AI / NLP** | OpenAI, Groq, or xAI with local semantic embeddings, BM25 and Reciprocal Rank Fusion |
| **DevOps** | Remote EC2 worker, Docker, K3s, kubectl (local kind remains optional) |

## Quick Start

### Prerequisites
- Docker & Docker Compose for running CodeDeck locally
- AWS EC2 Ubuntu host bootstrapped with K3s and the CodeDeck deployment worker for remote staging
- An API key for OpenAI, Groq, or xAI (optional; grounded heuristic Q&A remains available)

### Setup Instructions

1. **Configure the remote worker:** copy `.env.example` to `.env`, set `DEPLOYMENT_TARGET=remote`, configure `REMOTE_WORKER_URL` and a strong shared `REMOTE_WORKER_TOKEN`, then follow [AWS EC2 Setup](docs/AWS_EC2_SETUP.md). Keep worker secrets out of frontend variables.

2. **Start the control plane:**
   ```bash
   docker compose up -d --build
   ```

3. **Access the application:**
   - Frontend Dashboard: `http://localhost:3000`
   - Backend API Docs: `http://localhost:8000/docs`

4. **Configure AI-backed repository Q&A (optional):**
   - Set `OPENAI_API_KEY` for OpenAI.
   - Set `GROQ_API_KEY` for Groq, or use a `gsk_` key in the legacy `GROK_API_KEY` field; DeployMind detects it as Groq.
   - Set `GROK_API_KEY` or `XAI_API_KEY` only for an xAI key.
   - Restart the backend after changing `.env`. The Settings page displays whether answers use an LLM plus RAG or the grounded heuristic fallback.

## Supported Repository Types

| Framework / Environment | Support Level | Features |
|-------------------------|---------------|----------|
| Python (FastAPI/Flask/Django) | Excellent | Analysis, RAG, AI Tests, Auto-Deploy |
| Node.js (Express) | Good | Analysis, RAG, Auto-Deploy |
| Java (Spring Boot) | Good | Analysis, RAG, Auto-Deploy |
| React / static frontend | Good | Analysis, RAG, containerized static deployment |
| Generic Dockerfile | Standard | Auto-Deploy with deployment-contract validation |

## Project Structure

- `/backend`: FastAPI application, LangGraph workflows, and database models.
- `/frontend`: React dashboard, Vite configuration, and Tailwind styling.
- `/docs`: Architecture and system documentation.
- `/scripts`: Automation and workflow scripts.

## Documentation References

- [System Architecture](docs/ARCHITECTURE.md): Detailed component breakdown, RAG pipeline, and schemas.
- [AI & RAG Pipeline](docs/AI_PIPELINE.md): Embedding strategy, retrieval mechanism, and evaluation protocol.
- [Prompt Engineering](docs/PROMPT_ENGINEERING.md): Design decisions behind each LLM prompt.
- [Course Alignment](docs/COURSE_ALIGNMENT.md): AI DevOps concept coverage mapped to implementation files.
- [Future Roadmap](docs/FUTURE_ROADMAP.md): Phase-by-phase extension plan.

## Health, tests and monitoring

- `/health/live` checks process liveness; `/health/ready` checks database readiness.
- `/api/system/status`, `/api/system/worker`, and `/api/system/integrations` report backend and optional integration status.
- `/metrics` exposes Prometheus metrics when the dependency is installed.
- Grafana: `http://localhost:3001`; Prometheus: `http://localhost:9090`. Configure credentials in `.env`.
- Backend tests: `cd backend; ..\\.venv\\Scripts\\python.exe -m pytest -q` (or `pytest -q` in an activated environment).
- Frontend production build: `cd frontend; npm ci; npm run build`.
- Retrieval evaluation: `cd backend; ..\\.venv\\Scripts\\python.exe scripts/evaluate_retrieval.py`.

Copy `.env.example` to `.env`. The first semantic index may download FastEmbed's BGE-small model; no hosted embeddings API is used. Set `SEMANTIC_EMBEDDINGS_ENABLED=false` for lexical-only operation or deterministic tests.

## Deployment notes

Remote staging is the default target. FastAPI stores a queued deployment in PostgreSQL; the EC2 worker polls, builds, imports the image into K3s containerd, applies manifests, waits for rollout, and persists status and logs. See [Remote Deployment Architecture](docs/REMOTE_DEPLOYMENT_ARCHITECTURE.md), [K3s Deployment](docs/K3S_DEPLOYMENT.md), and [Deployment Security](docs/DEPLOYMENT_SECURITY.md). To expose the optional Local kind choice in the dashboard, set `LOCAL_KIND_ENABLED=true` on the backend and provide local Docker/kind/kubectl access. GitHub Actions do not deploy to EC2.

When a K3s ingress domain is configured, the worker reports an HTTP hostname after smoke testing. Without a domain, the worker verifies through a worker-local port-forward and reports no public URL. HTTPS and automated namespace cleanup are not implemented.

Read [Architecture](docs/ARCHITECTURE.md), [Hybrid Search](docs/HYBRID_SEARCH.md), [Langfuse](docs/LANGFUSE.md), [Monitoring](docs/MONITORING.md), [CI/CD](docs/CI_CD.md), and [Reliability](docs/RELIABILITY.md) for current implementation details and limitations.
