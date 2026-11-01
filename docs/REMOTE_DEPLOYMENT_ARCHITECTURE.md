# Remote Deployment Architecture

## Implemented path

```text
React dashboard -> FastAPI -> PostgreSQL remote job row
                               ^             |
                               | progress    | authenticated polling
                               +------------- EC2 worker
                                             | Docker build + docker save
                                             | k3s ctr image import
                                             | kubectl apply / rollout / smoke test
```

FastAPI validates the project's existing deployment contract and creates a `QUEUED` row in the existing `deployments` table. The worker authenticates with a bearer token, claims one queued row, and reports `RUNNING`, `BUILDING`, `DEPLOYING`, `VERIFYING`, then `SUCCEEDED` or `FAILED`. State, timestamps, bounded progress logs, smoke-test output, and failure reasons live in PostgreSQL. The existing `WorkerHeartbeat` table records health. A restart that reports a previously active job without that active job marks the interrupted deployment failed.

The worker has a small pull API: heartbeat, claim, progress, and completion. It does not accept shell commands or arbitrary kubectl commands from the backend. Job input is project ID, validated GitHub URL, analysis JSON, generated Kubernetes files, and a generated namespace. Worker authentication is shared-secret bearer authentication; use TLS and network ACLs between worker and backend. The token is never sent to the browser.

## Current limits

- The first worker uses the existing staging build/manifest/rollout implementation. Production blue-green deployment, approval, rollback, and the local preview proxy still follow their pre-migration code paths and are not remote-worker workflows yet.
- The worker is a single polling process. The queue is persisted and claim operations use row locking, but there is no broker, autoscaling, or automatic requeue of interrupted builds. Interrupted running work is marked failed for an explicit retry.
- Deployment rows reuse the existing JSON `details` column; `create_all` adds the worker-heartbeat table without resetting deployment history.
- `remote` is the default. The dashboard selector offers local kind only when the backend has `LOCAL_KIND_ENABLED=true`; a ContextVar scopes local/remote behavior to that deployment operation.
- Public DNS names are emitted only when `K3S_INGRESS_DOMAIN` is configured. The current Traefik ingress uses HTTP; HTTPS certificates are future work. With no domain, smoke tests run through worker-local port-forwarding, and temporary interactive access requires an operator-managed SSH tunnel/worker-side port-forward.

## Isolation and observability

Remote namespaces use project ID plus a random attempt suffix. ResourceQuota and LimitRange are applied in each namespace. Remote manifests are checked to reject unsupported cluster-wide resource kinds, hostPath, host namespaces, and privileged containers. Namespaces alone are not a security boundary for hostile code. Docker builds execute untrusted Dockerfiles on the EC2 worker; use a separate VM or hardened isolated builder before accepting hostile multi-tenant workloads. Prometheus and Grafana remain control-plane observability; Langfuse remains for AI traces. Worker metrics with job labels are not yet implemented.
