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
