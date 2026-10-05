"""One-off migration of uploads from the local volume to S3.

Written after uploads moved to S3: records created before the move still hold
a local path such as "uploads/teams/team_16.png", and the bytes only exist in
the uploads_data Docker volume. Losing that volume loses those images.

Safe to run repeatedly. Each row is handled independently and the local file is
only deleted after the object is confirmed present in S3 and the row is
committed, so an interruption leaves the row pointing at a file that is still on
disk rather than at nothing. Re-running picks up whatever was left.

Run inside the app container, which has the instance profile and the mounted
volume:

    docker exec cricketapp python -m scripts.migrate_uploads_to_s3

Add --dry-run first to see what would move without writing anything.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

# Importing app.main registers every ORM mapper before any model is touched.
# Importing app.players.models on its own configures mappers while
# app.teams.models is still half-loaded, which SQLAlchemy rejects with
# "expression 'TeamLevel' failed to locate a name". The app itself gets this for
# free because main imports every router; a standalone script has to.
from sqlalchemy import select  # noqa: E402

import app.main  # noqa: F401,E402
from app.database import async_session  # noqa: E402
from app.players.models import Player  # noqa: E402
from app.storage import InvalidImage, get_client, put_image  # noqa: E402
from app.teams.models import Team  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate")

LOCAL_ROOT = Path("uploads")

# Extension -> content type, matching app.storage.ALLOWED_IMAGE_TYPES. Derived
# from the file itself rather than trusted from the DB, because the old code
# took the extension straight from a client-supplied filename.
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def local_path_for(stored: str) -> Path | None:
    """Resolve a stored DB value to a file on disk, or None if not local.

    Guards against a stored value escaping LOCAL_ROOT. Values written by the
    old code are "uploads/<dir>/<file>", but a bare "<dir>/<file>" is also
    accepted so the script works if the uploads/ prefix is ever dropped.
    """
    candidate = Path(stored)
    if candidate.is_absolute():
        return None

    if stored.startswith("uploads/"):
        candidate = Path(stored[len("uploads/") :])

    resolved = (LOCAL_ROOT / candidate).resolve()
    try:
        resolved.relative_to(LOCAL_ROOT.resolve())
    except ValueError:
        logger.warning("  SKIP %s: resolves outside %s", stored, LOCAL_ROOT)
        return None

    return resolved if resolved.is_file() else None


def s3_key_for(stored: str) -> str:
    """The bare S3 key for a legacy stored value: uploads/teams/16.png -> teams/16.png."""
    return stored[len("uploads/") :] if stored.startswith("uploads/") else stored


async def migrate(dry_run: bool) -> int:
    client = get_client()
    moved = 0

    async with async_session() as db:
        targets = [
            ("team logo", Team.id, Team.logo, "teams"),
            ("player image", Player.id, Player.profile_image, "players"),
        ]

        for label, id_col, value_col, prefix in targets:
            rows = (await db.execute(select(id_col, value_col))).all()

            for entity_id, stored in rows:
                if not stored:
                    continue

                # Already migrated: the value is a bare S3 key, not a local path.
                if not stored.startswith("uploads/") and not (LOCAL_ROOT / stored).is_file():
                    continue

                path = local_path_for(stored)
                if path is None:
                    if stored.startswith("uploads/"):
                        logger.warning(
                            "  ORPHAN %s %s=%s: no file on disk", label, entity_id, stored
                        )
                    continue

                content_type = CONTENT_TYPES.get(path.suffix.lower())
                if content_type is None:
                    logger.warning("  SKIP %s %s: unsupported %s", label, entity_id, path.suffix)
                    continue

                key = s3_key_for(stored)
                size = path.stat().st_size

                if dry_run:
                    logger.info(
                        "  WOULD MOVE %s %s: %s (%d bytes) -> s3://%s/%s",
                        label,
                        entity_id,
                        stored,
                        size,
                        os.getenv("S3_UPLOAD_BUCKET", "?"),
                        key,
                    )
                    moved += 1
                    continue

                try:
                    await put_image(key, path.read_bytes(), content_type)
                except InvalidImage as exc:
                    logger.error("  FAIL %s %s: %s", label, entity_id, exc)
                    continue

                # Confirm the object landed before touching the row or the disk.
                try:
                    client.head_object(Bucket=os.environ["S3_UPLOAD_BUCKET"], Key=key)
                except Exception as exc:  # noqa: BLE001
                    logger.error("  FAIL %s %s: not in S3 after put (%s)", label, entity_id, exc)
                    continue

                entity = await db.get({"teams": Team, "players": Player}[prefix], entity_id)
                if entity is None:
                    logger.warning("  GONE %s %s: row disappeared", label, entity_id)
                    continue

                setattr(entity, value_col.key, key)
                await db.commit()

                # Only now is the disk copy redundant.
                path.unlink()
                logger.info(
                    "  MOVED %s %s: %s -> s3://%s/%s (%d bytes)",
                    label,
                    entity_id,
                    stored,
                    os.environ["S3_UPLOAD_BUCKET"],
                    key,
                    size,
                )
                moved += 1

    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would move, write nothing"
    )
    args = parser.parse_args()

    if not LOCAL_ROOT.is_dir():
        logger.error("No %s directory in this working directory", LOCAL_ROOT)
        return 1

    logger.info("Scanning %s from %s", LOCAL_ROOT, os.getcwd())
    count = asyncio.run(migrate(args.dry_run))

    verb = "would move" if args.dry_run else "moved"
    logger.info("Done: %s %d image(s)", verb, count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
