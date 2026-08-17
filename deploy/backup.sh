#!/bin/bash
# Nightly Postgres backup, keeps 7 days of dumps.
# Run from cron: 0 3 * * * /home/ubuntu/cricketapp/deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"
mkdir -p "$BACKUP_DIR"

docker compose -f docker-compose.prod.yml exec -T cricketdb \
  pg_dump -U cricket -d cricketdb | gzip > "$BACKUP_DIR/cricketdb-$(date +%F).sql.gz"

find "$BACKUP_DIR" -name "cricketdb-*.sql.gz" -mtime +7 -delete

echo "Backup done: $(date +%F)"
