#!/bin/bash
set -e

echo "=== Bootstrap Lightsail Instance ==="
echo ""

# ── 1. Update system ──────────────────────────────────────────
echo "[1/5] Updating system packages..."
sudo dnf update -y

# ── 2. Install Docker ─────────────────────────────────────────
echo "[2/5] Installing Docker..."
sudo dnf install -y docker
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker ec2-user

echo "Docker version:"
docker --version

# ── 3. Install Docker Compose plugin ──────────────────────────
echo "[3/5] Installing Docker Compose plugin..."
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "Docker Compose version:"
docker compose version

# ── 4. Create autobots-network ────────────────────────────────
echo "[4/5] Creating autobots-network..."
if docker network ls | grep -q autobots-network; then
    echo "autobots-network already exists"
else
    docker network create autobots-network
    echo "autobots-network created"
fi

# ── 5. Verify ─────────────────────────────────────────────────
echo "[5/5] Verifying..."
echo ""
echo "Docker:"
docker --version
echo ""
echo "Docker Compose:"
docker compose version
echo ""
echo "Networks:"
docker network ls
echo ""
echo "=== Bootstrap complete ==="
echo "NOTE: Log out and back in for docker group membership to take effect."
