# Remote Deployment Security

## Implemented controls

- Worker endpoints require a configured bearer secret and compare it in constant time. The pull API accepts structured deployment jobs and progress; it exposes no shell or general kubectl command endpoint.
- The frontend receives no worker token, AWS credentials, private key, or kubeconfig. Keep `REMOTE_WORKER_TOKEN`, `GITHUB_TOKEN`, and K3s credentials on the relevant backend/worker hosts only.
- GitHub URLs are validated by the existing allowlist parser. Private clone credentials are read from worker-side configuration and redacted from clone failures. Generated deployment namespaces are based on project IDs and random suffixes rather than user input.
- Remote manifests reject cluster-wide resource kinds, hostPath volumes, host networking/PID/IPC, and privileged containers. Namespace quotas and default/max container requests and limits are applied.
- Applications do not get the Docker socket, host filesystem mounts, host networking, or host PID namespace from CodeDeck-generated manifests.
- Worker progress logs are bounded and GitHub token-shaped strings are redacted before persistence.

## Limits and required operational controls

Kubernetes namespaces do not safely isolate arbitrary hostile code. A repository can include complex build instructions, consume network/disk/CPU during Docker builds, or exploit a vulnerable build/runtime component. The initial worker uses Docker Engine on the EC2 host and does not yet run builds in a separate VM, rootless builder, or sandbox. Treat this as a trusted-user MVP; for public multi-tenant use, move builds to disposable isolated VMs or a hardened build service before accepting arbitrary code.

The worker's K3s access is local to EC2 and the cluster API must not be public. The current worker requires enough permissions to create namespaces and apply resources; scope the service account and sudo rules to the required operations. K3s admin kubeconfig is not copied to the backend. Keep K3s config readable only by the worker identity, encrypt the EC2 volume, patch the host, restrict SSH by CIDR, and rotate the worker token.

Traefik exposure is HTTP only in this MVP. Open only intended ingress ports. Keep the NodePort range and K3s API closed. Review DNS and security-group changes before enabling public traffic. No HTTPS certificate automation, NetworkPolicy enforcement, automatic namespace expiry, build concurrency scheduler, or resource usage billing is implemented yet.
