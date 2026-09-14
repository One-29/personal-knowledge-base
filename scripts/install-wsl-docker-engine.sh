#!/usr/bin/env bash

set -euo pipefail

target_user="${1:?Pass the default WSL user name as the first argument.}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This installer must run as root." >&2
    exit 1
fi

source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "This installer supports Ubuntu WSL distributions only." >&2
    exit 1
fi
if ! id "$target_user" >/dev/null 2>&1; then
    echo "WSL user '$target_user' does not exist." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: ${architecture}
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

systemctl enable --now docker
usermod -aG docker "$target_user"

docker version --format 'Docker Engine server={{.Server.Version}} client={{.Client.Version}}'
docker compose version
