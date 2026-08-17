#!/bin/bash
# Issues a Let's Encrypt certificate for the API and switches nginx to HTTPS.
# Usage: ./deploy/init-ssl.sh [domain]
set -euo pipefail

cd "$(dirname "$0")/.."

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ] && [ -f .env ]; then
    DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$DOMAIN" ]; then
    echo "ERROR: no domain given. Usage: ./deploy/init-ssl.sh api.yourdomain.com" >&2
    exit 1
fi

echo ">> Using domain: $DOMAIN"

mkdir -p deploy/certbot/conf deploy/certbot/www

echo ">> Starting nginx in HTTP-only mode for the ACME challenge..."
sed "s/__DOMAIN__/$DOMAIN/g" deploy/nginx-http.conf > deploy/nginx.conf
docker compose -f docker-compose.prod.yml up -d nginx
sleep 3

echo ">> Requesting certificate from Let's Encrypt..."
docker compose -f docker-compose.prod.yml run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    --email admin@$DOMAIN --agree-tos --no-eff-email \
    -d $DOMAIN" certbot

echo ">> Switching nginx to HTTPS..."
sed "s/__DOMAIN__/$DOMAIN/g" deploy/nginx-ssl.conf > deploy/nginx.conf
docker compose -f docker-compose.prod.yml restart nginx

echo ">> Done. HTTPS is live on https://$DOMAIN"
