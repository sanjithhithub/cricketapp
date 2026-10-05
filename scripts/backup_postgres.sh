#!/usr/bin/env bash
#
# Nightly Postgres backup for CricketApp.
#
# Writes two copies:
#   1. locally under $BACKUP_DIR, for fast restore
#   2. to s3://$BUCKET/backups/, which is the copy that survives instance loss
#
# The local copy is not a backup by itself. It sits on the same EC2 instance and
# the same disk as the database, so an instance or volume loss takes both.
#
# Install as a root cron job on the host:
#
#   crontab -e
#   17 3 * * * /home/ubuntu/cricketapp/scripts/backup_postgres.sh >> /var/log/pg-backup.log 2>&1
#
# Off the hour on purpose: every instance in this region runs at :00, and a
# backup that competes with everything else for CPU is a backup that gets
# skipped when the box is busy.
#
# Restore:
#
#   docker exec -i cricketdb pg_restore -U postgres -d moviedb --clean --if-exists \
#     < /home/ubuntu/pgbackups/moviedb_YYYYmmddTHHMMSSZ.dump
#
# To restore from S3, download the dump first, or stream it:
#
#   docker exec cricketapp python -c "import boto3,sys; \
#     boto3.client('s3').download_file('$BUCKET','backups/<name>.dump','/tmp/b.dump')"

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/pgbackups}"
DB_CONTAINER="${DB_CONTAINER:-cricketdb}"
APP_CONTAINER="${APP_CONTAINER:-cricketapp}"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-moviedb}"
# 7 days locally. The host has 1.5 GB free of 6.7 GB, so retention is enforced
# rather than left to a human noticing the disk filling up.
LOCAL_RETAIN_DAYS="${LOCAL_RETAIN_DAYS:-7}"
S3_RETAIN_DAYS="${S3_RETAIN_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/${DB_NAME}_${STAMP}.dump"
PARTIAL="$FINAL.partial"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "starting backup of $DB_NAME"

# Dump to a .partial file and rename only on success. A run killed halfway
# through must not leave a truncated file that looks like a usable backup, and
# must not be picked up by the retention sweep as if it were a real one.
#
# pg_dump -Fc is the compressed custom format. It is smaller than plain SQL and,
# unlike a directory-format dump, it can be restored with pg_restore's selective
# flags. Redirecting on the host rather than writing inside the container avoids
# a permissions problem: the volume would be owned by the postgres user.
if docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$PARTIAL"; then
    mv "$PARTIAL" "$FINAL"
else
    rm -f "$PARTIAL"
    log "FAILED: pg_dump exited non-zero"
    exit 1
fi

SIZE=$(stat -c%s "$FINAL")
if [ "$SIZE" -lt 1024 ]; then
    # 47 MB of real data cannot compress below 1 KB. Anything this small means
    # the dump captured an empty or nearly empty database.
    log "FAILED: dump is only $SIZE bytes, refusing to keep it"
    rm -f "$FINAL"
    exit 1
fi

log "dumped $SIZE bytes to $FINAL"

# Upload from the app container, which has boto3 and the instance profile.
# The dump is piped in rather than passed as a path: this file lives on the host
# and the container has its own filesystem, so a host path would not resolve in
# there. --stamp keeps the S3 key identical to the local filename stem, so the
# two copies are identifiable as a pair.
if docker exec -i "$APP_CONTAINER" \
    python -m scripts.upload_backup --stamp "$STAMP" --retain-days "$S3_RETAIN_DAYS" - \
    < "$FINAL"; then
    log "uploaded to S3"
else
    # The local copy is still good, so this is a partial success rather than a
    # total one. Exit non-zero so cron output shows it, but do not delete the
    # dump: it is the only copy that exists.
    log "WARNING: S3 upload failed, local copy retained at $FINAL"
    exit 1
fi

# Prune locally. Only files matching this script's own naming pattern are
# touched, so an unrelated file dropped in the directory survives.
find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB_NAME}_*.dump" \
    -mtime "+$LOCAL_RETAIN_DAYS" -print -delete

log "done, retaining $LOCAL_RETAIN_DAYS day(s) locally, $S3_RETAIN_DAYS in S3"
