# CodeDeck — System Architecture

CodeDeck is an AI-assisted software development and DevOps platform. This document outlines the current system architecture, components, workflows, and database schemas.

## Component Architecture

```text
+----------------+      +-------------------+      +-----------------------+
|                |      |                   |      |                       |
|   Browser /    +----->+  Nginx (Frontend) +----->+ FastAPI (Backend)     |
|   Client       |      |                   |      |                       |
+----------------+      +-------------------+      +----+---------+--------+
                                                        |         |
                                                        v         v
                                           +------------+--+   +--+------------+
                                           |               |   |               |
                                           | PostgreSQL    |   | Docker & kind |
                                           | (Relational)  |   | (Deployment)  |
                                           +---------------+   +---------------+
```

## Backend Router Structure

The backend exposes several modular routers to handle different domains of the platform:

- `POST /projects/`: Create a new project integration.
- `GET /projects/`: List all projects.
- `GET /projects/{project_id}`: Retrieve project details.
- `POST /projects/{project_id}/analyze`: Trigger repository analysis.
- `GET /projects/{project_id}/status`: Check analysis and deployment status.
- `POST /projects/{project_id}/qa`: Interact with the RAG Q&A system.
- `POST /projects/{project_id}/tests`: Generate AI tests for the project.
- `POST /projects/{project_id}/deploy`: Trigger staging deployment (Docker & Kubernetes).
- `GET /health`: System health and status check.

## Core Workflow (LangGraph Pipeline)

The system orchestrates operations using a LangGraph sequential pipeline. Each node represents a distinct phase of the project processing lifecycle.

```text
[Analyze] -> [Intelligence] -> [Tests] -> [Security] -> [Docker] -> [Kubernetes]
```

1. **Analyze**: Clones the repository, analyzes structure, and extracts metadata.
2. **Intelligence**: Builds a versioned repository index using local semantic embeddings and lexical search.
3. **Tests**: Identifies application endpoints and generates test suites (FastAPI/Flask).
4. **Security**: Scans for secrets, Dockerfile root-user issues, and Kubernetes privileges.
5. **Docker**: Builds the application container image.
6. **Kubernetes**: Loads the image into the local `kind` cluster and applies manifests.

## RAG Pipeline Detail

Our Retreival-Augmented Generation pipeline powers the intelligent Q&A and analysis features:

1. **File Selection**: Includes bounded source, configuration and documentation files while skipping dependency/build output.
2. **Chunking**: Splits text into overlapping chunks with stable IDs, repository-relative path and line ranges.
3. **Semantic Embeddings**: Uses local FastEmbed `BAAI/bge-small-en-v1.5` vectors when the model is available.
4. **Lexical Search**: Ranks exact identifiers and terms with BM25 Okapi.
5. **Fusion**: Combines semantic and BM25 ranks with Reciprocal Rank Fusion (RRF), then deduplicates stable chunk IDs.
6. **Grounding**: Sends retrieved evidence to the configured OpenAI-compatible provider and asks for citations using actual source paths and lines. With no evidence it returns an insufficient-context response.
7. **Fallback**: If semantic embeddings cannot be loaded, it reports and uses BM25-only retrieval; it does not claim semantic or hybrid retrieval is active.

## Deployment Pipeline Detail

The DevOps automation pipeline executes the following sequence:

1. **Clone**: Fetches the latest source code from the repository.
2. **Sanitize**: Ensures the workspace is clean and ready.
3. **Docker Build**: Builds the container image using the detected or provided Dockerfile.
4. **Kind Load**: Loads the built image into the local `kind` (Kubernetes in Docker) cluster.
5. **Kubectl Apply**: Generates and applies Kubernetes manifests (Deployment, Service).
6. **Rollout**: Waits for the deployment rollout to complete successfully.
7. **Smoke Test**: Verifies application health via a temporary `port-forward`.

## Database Schema

The platform uses PostgreSQL with SQLAlchemy for data persistence.

| Table | Purpose |
|-------|---------|
| `Project` | Stores project metadata, repository URLs, and current state. |
| `Analysis` | Records the outcome of the repository analysis phase. |
| `Deployment` | Tracks deployment attempts, environment status, and URLs. |
| `PipelineStep` | Logs granular details, status, and output of each pipeline node. |
| `GeneratedFile` | Stores AI-generated artifacts such as tests, manifests, and scripts. |

## Configuration

System settings are managed via `config.py`, loading variables from the environment:
- `DATABASE_URL`: PostgreSQL connection string.
- `OPENAI_API_KEY`: Key for LLM services.
- `WORKSPACE_DIR`: Local directory for cloning and processing repositories.
- `KIND_CLUSTER_NAME`: Name of the local `kind` cluster.

## Security Model

**Currently Implemented:**
- Regex-based secret scanning in repositories.
- Dockerfile non-root user verification.
- Kubernetes manifest privilege checks.
- Environment variable filtering to prevent credential leakage.

**Not Yet Implemented (Future Scope):**
- User authentication and authorization.
- Role-Based Access Control (RBAC).
- Multi-tenant data isolation at the database level.

## Extension Points

The architecture is designed for extensibility:
- **Language Support**: Adding new languages requires extending the `Analyze` node parsers.
- **LLM Providers**: The RAG pipeline abstracts embedding and generation, allowing integration with other models.
- **Deployment Targets**: Additional environments (e.g., AWS, GCP) can be added as new nodes in the LangGraph pipeline.

## Design Decisions

| Technology | Reason for Selection |
|------------|----------------------|
| **FastAPI** | High performance, async support, and auto-generated OpenAPI documentation. |
| **React & Vite** | Fast build times, robust ecosystem, and modular component design. |
| **LangGraph** | Provides a resilient, stateful workflow execution engine for complex pipelines. |
| **Kind** | Lightweight, local Kubernetes testing without cloud infrastructure costs. |
| **FastEmbed + BM25** | Local dense semantic search catches paraphrases; BM25 preserves exact symbol and configuration matching. |

## Current vs. Future Scope

**Current Scope:**
- Full pipeline execution for supported Python frameworks (FastAPI, Flask) and standard Dockerfiles.
- Local Kubernetes deployment via `kind`.
- AI-assisted Q&A and test generation.

**Future Scope:**
- Cloud provider integrations (EKS, GKE, AKS).
- Advanced static application security testing (SAST).
- Full CI/CD integration with GitHub Actions / GitLab CI.
- Multi-user collaboration features.

## Current observability additions

The FastAPI process exposes liveness/readiness routes and Prometheus metrics. Compose's optional `observability` profile starts Prometheus and Grafana with provisioned configuration. Langfuse tracing is an optional server-side SDK integration with content capture off by default. These components do not include node-exporter, cAdvisor or kube-state-metrics, so they do not report host, Docker or Kubernetes resource telemetry. See [Monitoring](MONITORING.md), [Langfuse](LANGFUSE.md), [Hybrid Search](HYBRID_SEARCH.md), [CI/CD](CI_CD.md) and [Reliability](RELIABILITY.md).
