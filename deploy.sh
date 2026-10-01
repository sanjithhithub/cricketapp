#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="docker-compose.prod.yml"
HEALTH_URL="http://127.0.0.1:8000/health"

# A first deploy has to pull the image, initialise Postgres and run every pending
# Alembic migration before uvicorn serves /health. 30s is not enough for that on a
# small instance, and the script would report a failure for a deploy that is
# actually still succeeding. 180s covers the cold path; later deploys return on the
# first successful probe because the loop breaks as soon as /health answers.
HEALTH_ATTEMPTS=180

cd "$APP_DIR"

echo "[1/5] Logging in to GHCR..."
# Without this the reason for a failed login never reached the CI log at all,
# because `set -e` aborted at the pipeline with only docker's own stderr, which
# is easy to miss several lines above a remote tar. Name the likely causes so the
# failure is diagnosable from the workflow log alone.
if ! echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin; then
  echo "ERROR: docker login to ghcr.io failed." >&2
  echo "  - GHCR_PAT must be a CLASSIC token with read:packages." >&2
  echo "    A fine-grained PAT cannot read packages, even with the package" >&2
  echo "    granted, and is the usual cause of 'authentication required'." >&2
  echo "  - The token may have expired or been revoked." >&2
  echo "  - GHCR_USERNAME must be the account that owns the image:" >&2
  echo "    $GHCR_USERNAME" >&2
  echo "  - Check for a stray newline or trailing space in the secret." >&2
  exit 1
fi
echo "    logged in to ghcr.io as $GHCR_USERNAME"

echo "[2/5] Pulling latest image..."
docker compose -f "$COMPOSE_FILE" pull

echo "[3/5] Recreating app container (migrations run automatically inside)..."
docker compose -f "$COMPOSE_FILE" up -d

echo "[4/5] Waiting for /health (up to ${HEALTH_ATTEMPTS}s)..."
for i in $(seq 1 "$HEALTH_ATTEMPTS"); do
  if curl -sf "$HEALTH_URL" >/dev/null; then
    echo "    app is healthy after ${i}s"
    HEALTH_OK=1
    break
  fi
  # A restarting container will never pass this check, so surface why now instead
  # of waiting out the full timeout and then reporting a bare failure.
  if [ $((i % 30)) -eq 0 ]; then
    echo "    retrying... ${i}/${HEALTH_ATTEMPTS}"
    docker compose -f "$COMPOSE_FILE" ps
  fi
  sleep 1
done

if [ "${HEALTH_OK:-0}" -ne 1 ]; then
  echo "ERROR: /health did not return OK within ${HEALTH_ATTEMPTS}s" >&2
  docker compose -f "$COMPOSE_FILE" ps
  echo "--- recent app logs ---" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail 50 cricketapp >&2 2>&1 || true
  exit 1
fi

echo "[5/5] Pruning old images..."
docker image prune -f >/dev/null

echo "Deploy complete."