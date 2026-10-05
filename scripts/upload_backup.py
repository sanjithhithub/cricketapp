"""Upload a Postgres dump to S3 and prune old ones.

Runs inside the app container, which already has boto3 and the instance profile
(cricketapp-s3-upload). That role is scoped to this one bucket and backups/ is
under it, so this needs no extra AWS permissions and no console change.

The local copy on the host is not a backup on its own: it lives on the same
instance and the same disk as the database. This copy is the one that survives.

The dump arrives on stdin rather than as a path, because the caller is a host
script and the host filesystem is not visible inside this container. Passing a
path would silently fail with "no such file" from a different machine's point of
view. A path still works for running this by hand inside the container.

Restoring:

    docker exec -i cricketdb pg_restore -U postgres -d moviedb --clean --if-exists < dump.dump
"""

import argparse
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())

import boto3  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backup")

PREFIX = "backups/"
DB_NAME = "moviedb"


def normalise_stamp(raw: str | None) -> str:
    """A filename-safe UTC stamp.

    The caller passes one in so the S3 key matches the local filename stem, which
    makes the two copies identifiable as a pair. Falls back to now.
    """
    if not raw:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Anything that could escape the backups/ prefix or break a filename is
    # rejected rather than sanitised, so a malformed value fails loudly.
    if not all(c.isdigit() or c == "T" or c == "Z" for c in raw):
        raise ValueError(f"refusing to use {raw!r} as a timestamp")
    return raw


def object_key(stamp: str) -> str:
    return f"{PREFIX}{DB_NAME}_{stamp}.dump"


def spool_stdin() -> Path:
    """Write stdin to a temp file so upload and size check share one code path."""
    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as handle:
        name = handle.name
        # shutil is not needed: copyfileobj from sys.stdin.buffer keeps this to
        # one import and handles the read loop.
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            handle.write(chunk)
    return Path(name)


def prune(s3, bucket: str, days: int) -> int:
    """Delete dumps older than `days`. Only ever touches the backups/ prefix."""
    if days < 1:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            # Belt and braces. The paginator is asked for PREFIX, but this loop
            # can delete anything it is handed, so the prefix is re-checked here.
            # A wrong Prefix must never turn the sweeper loose on user uploads:
            # teams/team_16.png is older than any retention window and would go.
            if not key.startswith(PREFIX):
                continue
            # LastModified is absent on anything that is not a real object; skip
            # rather than guess and delete the wrong thing.
            modified = obj.get("LastModified")
            if not isinstance(modified, datetime):
                continue
            if modified < cutoff:
                s3.delete_object(Bucket=bucket, Key=key)
                logger.info("  pruned s3://%s/%s", bucket, key)
                removed += 1

    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dump",
        nargs="?",
        default="-",
        help="path to a .dump file inside this container, or '-' to read stdin",
    )
    parser.add_argument(
        "--stamp",
        default=None,
        help="UTC stamp for the object name, so it matches the local filename",
    )
    parser.add_argument(
        "--retain-days",
        type=int,
        default=int(os.getenv("BACKUP_S3_RETAIN_DAYS", "30")),
        help="delete S3 dumps older than this (0 disables pruning)",
    )
    args = parser.parse_args()

    bucket = os.getenv("S3_UPLOAD_BUCKET")
    if not bucket:
        logger.error("S3_UPLOAD_BUCKET is not set")
        return 1

    temp_path = None
    if args.dump == "-":
        temp_path = spool_stdin()
        path = temp_path
    else:
        path = Path(args.dump)
        if not path.is_file():
            logger.error("No such dump: %s", path)
            return 1

    try:
        size = path.stat().st_size
        if size == 0:
            # An empty dump means pg_dump produced nothing but the caller did not
            # fail, and uploading it would spend the retention window on nothing.
            logger.error("Dump is empty")
            return 1

        try:
            key = object_key(normalise_stamp(args.stamp))
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

        s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1"))

        logger.info("Uploading %d bytes -> s3://%s/%s", size, bucket, key)
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ServerSideEncryption": "AES256",
                "ContentType": "application/octet-stream",
            },
        )

        # Confirm the object is really there and the same size, so a truncated
        # upload cannot be mistaken for a successful backup.
        head = s3.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] != size:
            logger.error("Size mismatch: sent %d bytes, S3 has %d", size, head["ContentLength"])
            return 1

        logger.info("Verified s3://%s/%s (%d bytes)", bucket, key, head["ContentLength"])
        prune(s3, bucket, args.retain_days)
        return 0
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
