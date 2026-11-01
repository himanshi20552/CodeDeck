#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root (sudo bash bootstrap-ec2-k3s.sh)." >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]] || ! grep -q '^ID=ubuntu$' /etc/os-release; then
  echo "This bootstrap script supports Ubuntu only." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl docker.io
systemctl enable --now docker

if command -v k3s >/dev/null 2>&1; then
  echo "K3s already exists; leaving its installation and configuration unchanged."
else
  if [[ -e /etc/rancher/k3s/config.yaml ]]; then
    echo "K3s config exists without a k3s binary; inspect it before installing." >&2
    exit 1
  fi
  curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_ENABLE=false sh -
fi

echo "Bootstrap complete. Verify with: systemctl status docker k3s; k3s kubectl get nodes"
