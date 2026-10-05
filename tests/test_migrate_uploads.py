"""Tests for the local-to-S3 upload migration script.

The property that matters is that no row is ever left pointing at nothing: the
local file must outlive a failed upload, and an already-migrated row must not
be reprocessed.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))

import migrate_uploads_to_s3 as mig  # noqa: E402

# Stored values the old code could produce. os.path.splitext on an attacker
# supplied filename is how "../../evil.py" got here.
LEGACY_VALUES = [
    "uploads/teams/team_16.png",
    "uploads/players/player_7.jpg",
    "uploads/teams/../../etc/passwd.png",
    "/etc/passwd",
    "../../../root/.ssh/id_rsa.png",
]


@pytest.mark.parametrize("stored", LEGACY_VALUES)
def test_paths_outside_uploads_are_refused(stored, tmp_path, monkeypatch):
    """A stored value must not be able to read or delete outside uploads/."""
    monkeypatch.setattr(mig, "LOCAL_ROOT", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    # The real target file exists, but it is outside the uploads root.
    (tmp_path / "secret.png").write_bytes(b"secret")

    result = mig.local_path_for(stored)

    assert result is None or str(result).startswith(str(tmp_path / "uploads"))


def test_absolute_path_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mig, "LOCAL_ROOT", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    assert mig.local_path_for("/etc/passwd") is None


def test_uploads_prefix_is_stripped(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(mig, "LOCAL_ROOT", root)
    (root / "teams").mkdir(parents=True)
    target = root / "teams" / "team_16.png"
    target.write_bytes(b"x")

    found = mig.local_path_for("uploads/teams/team_16.png")

    assert found == target


def test_bare_key_resolves_too(tmp_path, monkeypatch):
    """Values without the uploads/ prefix must still work."""
    root = tmp_path / "uploads"
    monkeypatch.setattr(mig, "LOCAL_ROOT", root)
    (root / "teams").mkdir(parents=True)
    target = root / "teams" / "16.png"
    target.write_bytes(b"x")

    assert mig.local_path_for("teams/16.png") == target


def test_missing_file_returns_none(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(mig, "LOCAL_ROOT", root)
    (root / "teams").mkdir(parents=True)

    assert mig.local_path_for("uploads/teams/team_99.png") is None


def test_s3_key_strips_prefix():
    assert mig.s3_key_for("uploads/teams/team_16.png") == "teams/team_16.png"
    assert mig.s3_key_for("teams/16.png") == "teams/16.png"


def test_extension_maps_to_content_type():
    assert mig.CONTENT_TYPES[".png"] == "image/png"
    assert mig.CONTENT_TYPES[".jpg"] == "image/jpeg"
    # Anything that could be executed or interpreted must be absent.
    for bad in (".php", ".sh", ".py", ".html", ".svg", ""):
        assert bad not in mig.CONTENT_TYPES


def test_failed_upload_leaves_file_and_row_alone(tmp_path, monkeypatch):
    """If put_image raises, the disk file must survive so a re-run can retry."""
    root = tmp_path / "uploads"
    monkeypatch.setattr(mig, "LOCAL_ROOT", root)
    (root / "teams").mkdir(parents=True)
    target = root / "teams" / "team_16.png"
    target.write_bytes(b"pixels")

    class FakePlayer:
        id = 16
        profile_image = None

    class FakeTeam:
        __table__ = None
        id = 16
        logo = "uploads/teams/team_16.png"

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        async def execute(self, stmt):
            return FakeResult()

        async def get(self, model, entity_id):
            return FakeTeam()

        async def commit(self):
            raise AssertionError("must not commit when the upload failed")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mig, "async_session", lambda: FakeSession())
    monkeypatch.setattr(mig, "get_client", lambda: object())

    async def boom(key, data, content_type):
        raise mig.InvalidImage("too big")

    monkeypatch.setattr(mig, "put_image", boom)

    asyncio.run(mig.migrate(dry_run=False))

    assert target.exists(), "local copy must survive a failed upload"
    assert target.read_bytes() == b"pixels"


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(mig, "LOCAL_ROOT", root)
    (root / "teams").mkdir(parents=True)
    target = root / "teams" / "team_16.png"
    target.write_bytes(b"pixels")

    uploaded = []

    async def record(key, data, content_type):
        uploaded.append(key)

    monkeypatch.setattr(mig, "put_image", record)
    monkeypatch.setattr(mig, "get_client", lambda: object())

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        async def execute(self, stmt):
            return FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mig, "async_session", lambda: FakeSession())

    asyncio.run(mig.migrate(dry_run=True))

    assert uploaded == [], "dry run must not upload"
    assert target.exists(), "dry run must not delete"
