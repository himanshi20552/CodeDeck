# CI and deployment

## GitHub Actions

`.github/workflows/deploymind.yml` runs on pushes and pull requests with read-only repository permissions. Backend installs the pinned requirements, compiles modules and runs pytest with local SQLite, semantic model downloads disabled and external AI/Langfuse keys blank. Frontend uses `npm ci` and production build. Configuration job parses Compose, Prometheus, Grafana and Kubernetes YAML/JSON. Gitleaks scans repository history (fake provider-key fixtures are allowlisted). Image job builds backend and frontend Docker images tagged with the commit SHA and does not push them.

The workflow does not claim a remote deployment: GitHub-hosted runners cannot reach a kind cluster on a developer laptop, and this project has no configured remote cluster or registry. Add a registry and reachable staging environment with GitHub secrets/protected environments before enabling remote publication or deployment.

## Local kind staging

Start CodeDeck and a local kind cluster with Docker available, run analysis on a project in CodeDeck, then run:

```powershell
./scripts/stage-project.ps1 -ProjectId 12
```

The script checks liveness/readiness, calls CodeDeck's existing background staging API, polls the real project status and returns non-zero on a failed rollout or timeout. The backend needs Docker socket and Kubernetes access as configured by the existing Compose setup. It uses the generated repository manifests and existing deployment smoke check; it does not create a remote cloud deployment.

## Smoke test, failure and rollback

Staging success is only recorded after the existing deployment agent finishes rollout and smoke testing. Inspect the CodeDeck Staging pipeline step and deployment details on failure. Production promotion remains a separate explicit UI/API action; CI does not deploy to production. Production blue/green switching and rollback are controlled by the existing deployment API. For a failed local deployment, use CodeDeck's rollback/cancel controls and inspect generated manifests and pod logs before retrying.
