#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="docker-compose.prod.yml"
HEALTH_URL="http://localhost:8000/health"

cd "$APP_DIR"

echo "[1/5] Logging in to GHCR..."
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin

echo "[2/5] Pulling latest image..."
docker compose -f "$COMPOSE_FILE" pull

echo "[3/5] Recreating app container (migrations run automatically inside)..."
docker compose -f "$COMPOSE_FILE" up -d

echo "[4/5] Waiting for /health..."
for i in $(seq 1 30); do
  if curl -sf "$HEALTH_URL" >/dev/null; then
    echo "    app is healthy after ${i}s"
    HEALTH_OK=1
    break
  fi
  echo "    retrying... ${i}/30"
  sleep 1
done

if [ "${HEALTH_OK:-0}" -ne 1 ]; then
  echo "ERROR: /health did not return OK within 30s" >&2
  docker compose -f "$COMPOSE_FILE" ps
  exit 1
fi

echo "[5/5] Pruning old images..."
docker image prune -f >/dev/null

echo "Deploy complete."