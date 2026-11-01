# K3s Remote Staging Operations

## Image and manifest flow

The EC2 worker clones a public GitHub repository (or a private repository using its worker-only `GITHUB_TOKEN`), prepares the analyzed build context in a temporary workspace, and builds a unique `deploymind/<app>:d<deployment-id>` image. It exports the Docker image to a temporary archive, imports it with `k3s ctr images import`, and removes the archive. The generated Deployment uses `imagePullPolicy: Never`, so K3s uses the imported image rather than trying to pull it from a registry.

The worker prepares an isolated namespace for each staging attempt, applies ResourceQuota and LimitRange, applies the existing application and dependency manifests, waits for rollout, inspects readiness, and runs the smoke test using a temporary `kubectl port-forward`. Temporary repository directories are deleted when the deployment returns or fails.

K3s installs Traefik by default. When `K3S_INGRESS_DOMAIN` is configured, the worker creates a Traefik Ingress at `app-<deployment-id>.<domain>` and reports the HTTP URL after the worker-side smoke test passes. DNS must resolve that host to the instance, and inbound TCP/80 must be deliberately enabled. HTTPS is not configured by this implementation. With no base domain, no public URL is reported; use a worker-side port-forward and an SSH tunnel for temporary access.

## Troubleshooting

- Worker offline: check `systemctl status codedeck-worker`, API reachability, TLS, token equality, and `GET /api/system/worker`.
- K3s node unavailable: on EC2 run `sudo systemctl status k3s` and `sudo k3s kubectl get nodes`.
- Docker unavailable: run `docker info` as the worker service account and check daemon permissions.
- Image import fails: confirm worker permission for `k3s ctr`, disk space, and that the imported image tag matches the Deployment image.
- Rollout fails: inspect the persisted worker logs, then `sudo k3s kubectl describe pods -n <namespace>` and `sudo k3s kubectl logs -n <namespace> <pod>`.
- Ingress unavailable: check Traefik, wildcard DNS, `K3S_INGRESS_DOMAIN`, the HTTP security group rule, and service endpoint readiness.
- ResourceQuota exceeded: lower replicas or adjust the per-namespace limits after reviewing EC2 capacity.

The MVP retains namespaces and their workloads for deployment history; `DEPLOYMENT_RETENTION_HOURS` is configuration groundwork only and no automatic cleanup controller is implemented yet. Clean up an attempt deliberately with `sudo k3s kubectl delete namespace <namespace>` after confirming it is no longer needed.
