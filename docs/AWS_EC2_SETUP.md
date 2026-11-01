# AWS EC2 K3s Worker Setup

This is the bootstrap path for the remote staging worker. The script installs Docker Engine and K3s only when they are absent; it exits on an unsupported OS and does not rewrite an existing K3s configuration.

## EC2 and network

- Use a supported Ubuntu LTS AMI on a private subnet when possible. Size the instance for the largest expected image build; the default per-deployment quota is 4 requested CPUs and 8 GiB requested memory, with an 8 CPU / 16 GiB namespace ceiling.
- Allocate a stable public address only if an ingress hostname must resolve directly to this host.
- Security group ingress: SSH (22) from an administrator CIDR; HTTP/HTTPS (80/443) only when DNS and ingress are configured. Do not expose the K3s API (6443) or Docker API (2375/2376). Keep NodePort (30000-32767) closed.
- Allow outbound HTTPS from the worker to the CodeDeck API, GitHub, and image/package registries. The backend does not connect to K3s.
- Configure DNS `*.${K3S_INGRESS_DOMAIN}` to the EC2 address before enabling ingress hostnames. TLS is not configured by this MVP.

## Bootstrap

Copy `scripts/bootstrap-ec2-k3s.sh` to the Ubuntu instance and run it as root after reviewing it:

```bash
sudo bash bootstrap-ec2-k3s.sh
sudo k3s kubectl get nodes -o wide
sudo docker info
```

Install Python 3.11+, Git, and the CodeDeck backend package on the instance. The worker uses the CodeDeck staging deployment implementation, Docker for builds, and `k3s ctr images import` to copy the image into K3s containerd. Docker and containerd image stores are separate.

Create a dedicated worker env file at `/etc/codedeck/worker.env`, mode `0600`, with the same `REMOTE_WORKER_TOKEN` and `WORKER_ID` as the control plane, a reachable `REMOTE_WORKER_URL`, `DEPLOYMENT_TARGET=remote`, `KUBECONFIG=/etc/codedeck/kube/config`, and an isolated `WORKSPACE_DIR`. Never put these values in frontend configuration. If the worker runs as an unprivileged service account, give it read-only access to this kubeconfig and a restricted sudo rule for `k3s ctr images import` (`K3S_CTR_USE_SUDO=true`); do not make the K3s API reachable from the internet.

For a root-owned kubeconfig readable only by the worker group:

```bash
sudo install -d -o root -g codedeck -m 0750 /etc/codedeck/kube
sudo install -o root -g codedeck -m 0640 /etc/rancher/k3s/k3s.yaml /etc/codedeck/kube/config
```

The copied kubeconfig contains cluster credentials. Keep the directory and file restricted, and do not publish or include them in application logs.

Example systemd unit (adjust paths and the service account to the local install):

```ini
[Unit]
Description=CodeDeck remote deployment worker
After=network-online.target docker.service k3s.service
Wants=network-online.target

[Service]
Type=simple
User=codedeck
Group=codedeck
WorkingDirectory=/opt/codedeck/backend/backend
EnvironmentFile=/etc/codedeck/worker.env
Environment=HOME=/var/lib/codedeck
Environment=DOCKER_CONFIG=/var/lib/codedeck/docker-config
ExecStart=/opt/codedeck/backend/venv/bin/python -m app.worker.runner
Restart=on-failure
RestartSec=5
StateDirectory=codedeck
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Install as `/etc/systemd/system/codedeck-worker.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codedeck-worker
sudo systemctl status codedeck-worker
```

The isolated `HOME` and `DOCKER_CONFIG` keep Docker CLI configuration lookup out of `/home` while `ProtectHome=true` is active. This avoids an interactive user's Docker configuration and credentials; no Docker `config.json` is created by this unit. When `DOCKER_CONFIG` is unset, remote Docker subprocesses use a private `docker-config` directory beside the configured workspace. If the worker needs registry authentication, provision a service-owned Docker config explicitly with restrictive permissions. Do not disable `ProtectHome` or loosen home-directory permissions to suppress a CLI warning.

The worker needs access to a Docker daemon for builds and K3s containerd for import. Membership in the Docker group is effectively root-equivalent. The current Docker build path is not a hardened sandbox for hostile Dockerfiles; use a separate build VM/account or a stronger isolated builder for multi-tenant production.

## Verify

Check `GET /api/system/worker` on the backend. It should report a current heartbeat, Docker availability, K3s availability, and node status. Then create a staging deployment from the dashboard and inspect the persisted deployment logs. A real EC2 deployment is not verified by repository unit tests.

For the service's actual runtime environment and Docker CLI diagnostics, run on EC2:

```bash
sudo systemctl cat codedeck-worker
sudo systemctl show codedeck-worker -p User -p Group -p WorkingDirectory -p ExecStart -p Environment -p ProtectHome
sudo -u codedeck -H /usr/bin/docker version
sudo -u codedeck -H /usr/bin/docker buildx version
sudo -u codedeck -H /usr/bin/docker context show
sudo journalctl -u codedeck-worker -n 200 --no-pager
```

Compare the service's `HOME`, `DOCKER_CONFIG`, `PATH`, and `DOCKER_HOST` with the interactive session. Never print `config.json`, `/etc/codedeck/worker.env`, or the worker token while collecting diagnostics.
