"""Tests for the S3 backup upload step.

A backup script that silently does nothing is worse than no backup script, so the
behaviours tested here are the failure modes: accepting an empty dump, accepting
a truncated upload, and pruning outside the backups/ prefix.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))

import upload_backup as ub  # noqa: E402


class FakeS3:
    """Records calls. head_object reports whatever was uploaded."""

    def __init__(self, region="eu-north-1"):
        self.region = region
        self.put = {}
        self.deleted = []
        self.listed_prefixes = []
        self.contents = []
        self.head_override = None
        self.upload_calls = 0

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.upload_calls += 1
        with open(path, "rb") as handle:
            self.put[key] = handle.read()
        self.bucket = bucket
        self.extra_args = ExtraArgs

    def head_object(self, Bucket, Key):
        if self.head_override is not None:
            return {"ContentLength": self.head_override}
        return {"ContentLength": len(self.put[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                fake.listed_prefixes.append(Prefix)
                return [{"Contents": fake.contents}]

        return Paginator()

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


@pytest.fixture
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(ub.boto3, "client", lambda *a, **k: fake)
    monkeypatch.setenv("S3_UPLOAD_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    return fake


def test_key_uses_the_supplied_stamp():
    """The S3 key must match the local filename stem so the two copies pair up."""
    key = ub.object_key(ub.normalise_stamp("20261005T031500Z"))
    assert key == "backups/moviedb_20261005T031500Z.dump"


def test_stamp_defaults_to_now():
    stamp = ub.normalise_stamp(None)
    assert stamp.endswith("Z")
    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "x;rm -rf /", "2026-10-05", "../../etc"])
def test_malformed_stamp_is_rejected(bad):
    """A stamp that could escape backups/ must fail loudly, not be sanitised."""
    with pytest.raises(ValueError):
        ub.normalise_stamp(bad)


def test_empty_stdin_is_refused(fake_s3, monkeypatch):
    """An empty dump must never reach S3; it would spend a retention slot."""
    monkeypatch.setattr(ub.sys, "argv", ["upload_backup.py", "-"])
    monkeypatch.setattr(ub.sys, "stdin", type("S", (), {"buffer": _reader(b"")})())

    assert ub.main() == 1
    assert fake_s3.upload_calls == 0
    assert fake_s3.put == {}


def test_missing_path_is_refused(fake_s3, monkeypatch):
    monkeypatch.setattr(ub.sys, "argv", ["upload_backup.py", "/nope/missing.dump"])
    assert ub.main() == 1
    assert fake_s3.upload_calls == 0


def test_successful_upload_is_verified(fake_s3, monkeypatch):
    payload = b"PGDMP" + b"x" * 5000
    monkeypatch.setattr(ub.sys, "stdin", type("S", (), {"buffer": _reader(payload)})())
    monkeypatch.setattr(ub.sys, "argv", ["upload_backup.py", "--stamp", "20261005T031500Z", "-"])

    assert ub.main() == 0
    assert fake_s3.put == {"backups/moviedb_20261005T031500Z.dump": payload}
    # Encryption at rest is not optional for a file holding user records.
    assert fake_s3.extra_args["ServerSideEncryption"] == "AES256"
    assert fake_s3.extra_args["ContentType"] == "application/octet-stream"


def test_truncated_upload_is_detected(fake_s3, monkeypatch):
    """A size mismatch must fail the run rather than look like a good backup."""
    payload = b"y" * 4000
    monkeypatch.setattr(ub.sys, "stdin", type("S", (), {"buffer": _reader(payload)})())
    monkeypatch.setattr(ub.sys, "argv", ["upload_backup.py", "--stamp", "20261005T031500Z", "-"])
    fake_s3.head_override = 12

    assert ub.main() == 1


def test_prune_only_touches_the_backups_prefix(fake_s3):
    fake_s3.contents = [
        {"Key": "backups/moviedb_old.dump", "LastModified": datetime.now(UTC) - timedelta(days=99)},
        {"Key": "backups/moviedb_new.dump", "LastModified": datetime.now(UTC)},
        # Anything outside the prefix must be unreachable by the sweeper, even if
        # the listing were somehow to return it.
        {"Key": "teams/team_16.png", "LastModified": datetime.now(UTC) - timedelta(days=999)},
    ]

    ub.prune(fake_s3, "test-bucket", 30)

    assert fake_s3.deleted == ["backups/moviedb_old.dump"]
    assert set(fake_s3.listed_prefixes) == {"backups/"}


def test_prune_skips_entries_without_a_usable_timestamp(fake_s3):
    """A missing LastModified means skip, never delete on a guess."""
    fake_s3.contents = [
        {"Key": "backups/moviedb_odd.dump"},
        {"Key": "backups/moviedb_other.dump", "LastModified": "not-a-datetime"},
    ]

    ub.prune(fake_s3, "test-bucket", 30)

    assert fake_s3.deleted == []


def test_prune_can_be_disabled(fake_s3):
    fake_s3.contents = [
        {"Key": "backups/moviedb_old.dump", "LastModified": datetime.now(UTC) - timedelta(days=999)}
    ]
    ub.prune(fake_s3, "test-bucket", 0)
    assert fake_s3.deleted == []


def test_temp_file_is_cleaned_up(fake_s3, monkeypatch):
    """The spooled copy holds user records and must not be left in the container."""
    # Scoped to this script's own suffix. Comparing whole directory listings also
    # catches unrelated temp files created by anything else on the machine.
    before = _spooled_names()

    payload = b"z" * 3000
    monkeypatch.setattr(ub.sys, "stdin", type("S", (), {"buffer": _reader(payload)})())
    monkeypatch.setattr(ub.sys, "argv", ["upload_backup.py", "--stamp", "20261005T031500Z", "-"])

    assert ub.main() == 0
    assert _spooled_names() - before == set()


def _spooled_names() -> set[str]:
    return {n for n in os.listdir(ub.tempfile.gettempdir()) if n.endswith(".dump")}


def _reader(data: bytes):
    """A file-like object over `data` with the .buffer attribute main() uses."""

    class Reader:
        def __init__(self):
            self._data = data
            self._pos = 0

        def read(self, size=-1):
            if size == -1:
                chunk, self._pos = self._data[self._pos :], len(self._data)
                return chunk
            chunk = self._data[self._pos : self._pos + size]
            self._pos += len(chunk)
            return chunk

    return Reader()
