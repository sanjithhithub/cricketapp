"""Tests for the /uploads serving route.

The route replaced a StaticFiles mount, so two regressions matter: a traversal
must not escape the bucket, and the legacy on-disk layout must keep serving
during the transition.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

import app.main as main_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def cdn_off(monkeypatch):
    """Streaming tests must not depend on the developer's own environment.

    UPLOADS_CDN_BASE_URL is read per request, so a developer who has set it
    locally would otherwise see every assertion below flip to a 307.
    """
    monkeypatch.delenv("UPLOADS_CDN_BASE_URL", raising=False)


@pytest.fixture
def cdn_on(monkeypatch):
    monkeypatch.setenv("UPLOADS_CDN_BASE_URL", "https://media.cricketapp.in")


# Payloads that must never read outside the bucket or the uploads directory.
TRAVERSAL = [
    "../.env",
    "../../etc/passwd",
    "teams/../../.env",
    "%2e%2e%2f.env",
    "teams/%2e%2e/%2e%2e/.env",
]


@pytest.mark.parametrize("payload", TRAVERSAL)
def test_traversal_rejected(client, payload):
    response = client.get(f"/uploads/{payload}")
    assert response.status_code in (400, 404), response.text
    # The specific thing that must never happen is leaking file contents.
    assert "DATABASE_URL" not in response.text
    assert "root:" not in response.text


def test_known_object_is_served(client, monkeypatch):
    captured = {}

    async def fake_read_image(key):
        captured["key"] = key
        return b"\x89PNG-fake"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/teams/16.png")

    assert response.status_code == 200
    assert captured["key"] == "teams/16.png"
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers.get("cache-control", "")


def test_uploads_prefixed_key_is_stripped(client, monkeypatch):
    """DB rows written before the migration still carry the uploads/ prefix."""
    captured = {}

    async def fake_read_image(key):
        captured["key"] = key
        return b"\x89PNG-fake"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/uploads/teams/16.png")

    assert response.status_code == 200
    assert captured["key"] == "teams/16.png"


def test_missing_object_is_404_not_500(client, monkeypatch):
    async def fake_read_image(key):
        return None

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/players/9999.png")

    assert response.status_code == 404


def test_non_image_extension_is_refused(client, monkeypatch):
    """An arbitrary object must not be re-served with a guessed content type."""

    async def fake_read_image(key):
        return b"#!/bin/sh"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/teams/16.sh")

    assert response.status_code == 415


def test_content_type_per_extension(client, monkeypatch):
    async def fake_read_image(key):
        return b"bytes"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    for ext, expected in (
        ("png", "image/png"),
        ("jpg", "image/jpeg"),
        ("jpeg", "image/jpeg"),
        ("webp", "image/webp"),
        ("gif", "image/gif"),
    ):
        response = client.get(f"/uploads/teams/16.{ext}")
        assert response.headers["content-type"] == expected


# CloudFront mode. The app must not stream bytes when a CDN is configured, and
# must still refuse to build a redirect for a key it would not have served.


def test_cdn_off_streams_from_s3(client, monkeypatch):
    called = []

    async def fake_read_image(key):
        called.append(key)
        return b"\x89PNG-fake"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/teams/16.png")

    assert response.status_code == 200
    assert called == ["teams/16.png"]


def test_cdn_on_redirects_instead_of_streaming(client, monkeypatch, cdn_on):
    """The whole point: zero bytes through the API when the CDN is configured."""
    called = []

    async def fake_read_image(key):
        called.append(key)
        return b"\x89PNG-fake"

    monkeypatch.setattr(main_module, "read_image", fake_read_image)

    response = client.get("/uploads/teams/16.png", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://media.cricketapp.in/teams/16.png"
    assert called == [], "must not read from S3 when redirecting"


def test_cdn_on_keeps_legacy_prefix_stripped(client, monkeypatch, cdn_on):
    """Rows written before the migration still resolve to the same CDN path."""
    monkeypatch.setattr(main_module, "read_image", None)

    response = client.get("/uploads/uploads/teams/16.png", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://media.cricketapp.in/teams/16.png"


def test_cdn_on_still_rejects_traversal(client, monkeypatch, cdn_on):
    """A 400 must not become a redirect, or the CDN URL becomes an open proxy."""
    monkeypatch.setattr(main_module, "read_image", None)

    for payload in ("../.env", "teams/%2e%2e/%2e%2e/.env"):
        response = client.get(f"/uploads/{payload}", follow_redirects=False)
        assert response.status_code in (400, 404), response.text
        assert "media.cricketapp.in" not in response.headers.get("location", "")


def test_cdn_trailing_slash_is_trimmed(client, monkeypatch):
    """A trailing slash in .env must not produce a double slash in the key."""
    monkeypatch.setenv("UPLOADS_CDN_BASE_URL", "https://media.cricketapp.in/")
    monkeypatch.setattr(main_module, "read_image", None)

    response = client.get("/uploads/teams/16.png", follow_redirects=False)

    assert response.headers["location"] == "https://media.cricketapp.in/teams/16.png"


def test_cdn_url_special_characters_are_encoded(client, monkeypatch, cdn_on):
    """The key must not be able to inject a query string into the CDN URL."""
    monkeypatch.setattr(main_module, "read_image", None)

    response = client.get("/uploads/teams/a b?x=1.png", follow_redirects=False)

    assert response.status_code == 307
    location = response.headers["location"]
    assert " " not in location
    assert "?" not in location.removeprefix("https://media.cricketapp.in/")


def test_local_file_is_served_from_disk_even_when_cdn_on(client, monkeypatch, cdn_on, tmp_path):
    """Pre-migration leftovers are not in the bucket, so a redirect would 404.

    This is the case that would silently break an image if it were handled the
    other way round: the file exists, the CDN does not have it.
    """
    monkeypatch.setattr(main_module, "UPLOADS_DIR", str(tmp_path))
    (tmp_path / "teams").mkdir()
    (tmp_path / "teams" / "team_16.png").write_bytes(b"\x89PNG-local")

    monkeypatch.setattr(main_module, "read_image", None)

    response = client.get("/uploads/teams/team_16.png", follow_redirects=False)

    assert response.status_code == 200
    assert response.content == b"\x89PNG-local"


def test_cdn_env_untouched_by_whitespace(monkeypatch):
    monkeypatch.setenv("UPLOADS_CDN_BASE_URL", "  https://media.cricketapp.in  ")
    assert main_module._cdn_base_url() == "https://media.cricketapp.in"

    monkeypatch.setenv("UPLOADS_CDN_BASE_URL", "   ")
    assert main_module._cdn_base_url() == ""
