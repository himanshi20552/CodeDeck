# CodeDeck Documentation

Welcome to the technical documentation for **CodeDeck**, an AI-assisted software delivery and DevOps platform.

---

## Documentation Index

| Document | Description |
| :--- | :--- |
| [`AWS_EC2_SETUP.md`](AWS_EC2_SETUP.md) | EC2 bootstrap, worker service, configuration, and health verification |
| [`REMOTE_DEPLOYMENT_ARCHITECTURE.md`](REMOTE_DEPLOYMENT_ARCHITECTURE.md) | Control-plane/worker flow and implementation status |
| [`K3S_DEPLOYMENT.md`](K3S_DEPLOYMENT.md) | Docker-to-containerd image transfer, rollout, ingress, and troubleshooting |
| [`DEPLOYMENT_SECURITY.md`](DEPLOYMENT_SECURITY.md) | Worker credentials, namespace controls, and untrusted code limits |

| Document | Description |
| :--- | :--- |
| [`AWS_EC2_SETUP.md`](AWS_EC2_SETUP.md) | EC2 bootstrap, worker service, configuration, and health verification |
| [`REMOTE_DEPLOYMENT_ARCHITECTURE.md`](REMOTE_DEPLOYMENT_ARCHITECTURE.md) | Control-plane/worker flow and implementation status |
| [`K3S_DEPLOYMENT.md`](K3S_DEPLOYMENT.md) | Docker-to-containerd image transfer, rollout, ingress, and troubleshooting |
| [`DEPLOYMENT_SECURITY.md`](DEPLOYMENT_SECURITY.md) | Worker credentials, namespace controls, and untrusted code limits |

| Document | Description |
| :--- | :--- |
| [`ARCHITECTURE.md`](file:///d:/agent/docs/ARCHITECTURE.md) | High-level system architecture, component breakdown, LangGraph workflow, DB schemas, security model |
| [`TECHNOLOGY_GUIDE.md`](file:///d:/agent/docs/TECHNOLOGY_GUIDE.md) | Comprehensive technology guide covering all 30+ stack components for developers |
| [`AI_PIPELINE.md`](file:///d:/agent/docs/AI_PIPELINE.md) | Detailed breakdown of repository RAG, hash embeddings, Q&A, AI testing, and failure analysis |
| [`DATA_FLOW.md`](file:///d:/agent/docs/DATA_FLOW.md) | Data movement diagrams, storage locations, LLM data boundaries, and secret handling |
| [`KUBERNETES_GUIDE.md`](file:///d:/agent/docs/KUBERNETES_GUIDE.md) | Kubernetes concepts, local `kind` cluster integration, manifest generation, rollout, and smoke testing |
| [`MONITORING.md`](file:///d:/agent/docs/MONITORING.md) | Deployment health monitoring story, comparing current state with future APM observability target |
| [`INFRASTRUCTURE.md`](file:///d:/agent/docs/INFRASTRUCTURE.md) | Physical vs containerized topology, network routing, system requirements, and cloud migration path |
| [`COURSE_ALIGNMENT.md`](file:///d:/agent/docs/COURSE_ALIGNMENT.md) | Comprehensive mapping of course concepts, implemented tools, evidence, and gaps |
| [`FUTURE_ROADMAP.md`](file:///d:/agent/docs/FUTURE_ROADMAP.md) | Development roadmap, phase breakdown, and future target architecture diagram |
| [`VIVA_GUIDE.md`](file:///d:/agent/docs/VIVA_GUIDE.md) | Technical defense guide with 80+ Q&A pairs covering all platform dimensions |
| [`PRESENTATION_CONTENT.md`](file:///d:/agent/docs/PRESENTATION_CONTENT.md) | Structured 8-slide presentation content for technical walkthroughs |
| [`PROJECT_EXPLANATION.md`](file:///d:/agent/docs/PROJECT_EXPLANATION.md) | Core platform purpose, end-to-end delivery pipeline, and architectural goals |
| [`TROUBLESHOOTING.md`](file:///d:/agent/docs/TROUBLESHOOTING.md) | Practical fixes for common Docker, Kubernetes, database, and pipeline issues |
| [`DEMO.md`](file:///d:/agent/docs/DEMO.md) | Operational workflow and step-by-step execution guide |

---

## Quick Start (Local Direct Execution)

To run backend and frontend directly on your local machine:

1. **Backend**:
   ```bash
   cd backend
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```
2. **Frontend**:
   ```bash
   cd frontend
   npm install
   npm run dev
   ```
3. Open `http://localhost:5173` in your browser.
