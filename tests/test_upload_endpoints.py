"""Upload endpoints persist and return routable paths; captains return 200.

Regressions this exists for:

1. ``POST /teams/{team_id}/upload-logo`` returned 200 but never wrote the
   ``logo`` column, so ``GET /teams/{team_id}`` still reported ``logo: null``
   and the object was orphaned - the file was the only copy of the fact.
2. Both upload endpoints returned the bare storage key (``teams/17.png``). A
   browser resolves that relative to the current page, so on a nested route
   like ``/teams/17`` it fetches ``/teams/teams/17.png``. The stored and
   returned value is now the root-relative ``/uploads/teams/17.png``.
3. ``PUT /teams/{team_id}/captains`` once replied 400 even when it applied the
   change: the message, which doubles as the success summary, was being read as
   the failure reason.
"""

import asyncio
import io
import struct
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_EMAIL = "upload_admin@test.com"


class FakeS3:
    """In-memory stand-in for boto3 so the suite needs no AWS credentials."""

    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        from botocore.exceptions import ClientError

        objects = self.objects
        if kwargs["Key"] not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Resp:
            def read(self):
                return objects[kwargs["Key"]]

        return {"Body": _Resp()}


@pytest.fixture(autouse=True)
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr("app.storage.get_client", lambda: fake)
    return fake


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def refs(client):
    async def fetch():
        async with async_session() as db:
            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()
            return level.id, country.id, state.id, city.id

    return asyncio.run(fetch())


@pytest.fixture(scope="module")
def admin(client):
    async def setup():
        async with async_session() as db:
            user = User(
                email=ADMIN_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Upload Admin",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return asyncio.run(setup())


@pytest.fixture
def as_user():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


def png_bytes():
    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _team(client, as_user, refs, name):
    level_id, country_id, state_id, city_id = refs
    r = client.post(
        "/v1/teams",
        json={
            "name": name,
            "short_name": name[:10],
            "level_id": level_id,
            "homeground": "Home",
            "founder": "Founder",
            "founded_year": 2001,
            "owner": "Owner",
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _player(client, as_user, refs, name, mobile):
    _, country_id, state_id, city_id = refs
    r = client.post(
        "/v1/players",
        json={
            "first_name": name,
            "last_name": "Player",
            "country_code": "+91",
            "mobile_number": mobile,
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
            "email": f"{name.lower()}@test.com",
            "date_of_birth": "1995-05-20",
            "gender": "male",
            "batting_hand": "Right",
            "batting_position": "Opening",
            "bowling_hand": "Right",
            "bowling_type": "Medium Fast",
            "height": 178.0,
            "weight": 72.0,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_team_logo_upload_persists_and_is_routable(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    team = _team(client, as_user, refs, "Upload Logo FC")

    up = client.post(
        f"/v1/teams/{team['id']}/upload-logo",
        files={"file": ("logo.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert up.status_code == 200, up.text

    # The response is the exact path a client can use: root-relative, so it is
    # safe on a nested page, and routable as-is.
    expected = f"/uploads/teams/{team['id']}.png"
    assert up.json()["logo"] == expected

    # Regression 1: the column is actually written, so GET sees the logo.
    detail = client.get(f"/v1/teams/{team['id']}").json()
    assert detail["logo"] == expected

    # And the object is behind that path on the serving route.
    served = client.get(expected)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert f"teams/{team['id']}.png" in fake_s3.objects


def test_player_profile_image_upload_is_routable(client, refs, admin, as_user, fake_s3):
    as_user(admin)
    player = _player(client, as_user, refs, "Profile", "9700000001")

    up = client.post(
        f"/v1/players/{player['id']}/upload-profile-image",
        files={"file": ("face.png", io.BytesIO(png_bytes()), "image/png")},
    )
    assert up.status_code == 200, up.text

    expected = f"/uploads/players/{player['id']}.png"
    assert up.json()["profile_image"] == expected
    assert client.get(f"/v1/players/{player['id']}").json()["profile_image"] == expected
    assert client.get(expected).status_code == 200
    assert f"players/{player['id']}.png" in fake_s3.objects


def test_upload_rejects_oversized_image_without_writing_the_column(client, refs, admin, as_user):
    """A rejected upload must not clobber an existing logo either."""
    as_user(admin)
    team = _team(client, as_user, refs, "Upload Reject FC")

    bad = client.post(
        f"/v1/teams/{team['id']}/upload-logo",
        files={"file": ("big.png", io.BytesIO(b"\x89PNG" + b"x" * (6 * 1024 * 1024)), "image/png")},
    )
    assert bad.status_code == 400
    assert client.get(f"/v1/teams/{team['id']}").json()["logo"] is None


def test_captains_success_returns_200(client, refs, admin, as_user):
    """Regression 3: a successful PUT must not come back as a 400."""
    as_user(admin)
    team = _team(client, as_user, refs, "Captains OK FC")
    a = _player(client, as_user, refs, "Captain", "9600000001")
    b = _player(client, as_user, refs, "Vice", "9600000002")
    bulk = client.post(
        f"/v1/teams/{team['id']}/players/bulk",
        json={"player_ids": [a["id"], b["id"]], "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text

    ok = client.put(
        f"/v1/teams/{team['id']}/captains",
        json={"captain_player_id": a["id"], "vice_captain_player_id": b["id"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["captain_player_id"] == a["id"]
    assert ok.json()["vice_captain_player_id"] == b["id"]

    # Clearing is also a 200, and a vice-only squad reports captain null.
    cleared = client.put(
        f"/v1/teams/{team['id']}/captains",
        json={"captain_player_id": None, "vice_captain_player_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["captain_player_id"] is None
