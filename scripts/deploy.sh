#!/bin/bash
# deploy.sh — Opdater og genstart bot på Hetzner
# Kør som: bash scripts/deploy.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Henter seneste kode fra GitHub..."
git pull origin main

echo "==> Bygger Docker images..."
docker compose build --no-cache

echo "==> Kører database migrations..."
docker compose run --rm monitor python -m alembic upgrade head

echo "==> Genstarter services..."
docker compose up -d

echo "==> Status:"
docker compose ps

echo "✅ Deploy færdig!"
