"""A full end-to-end journey across every section of the API.

Where the rest of the suite tests one behaviour at a time with seeded rows,
this drives the real HTTP API end to end in the order a real user would: sign
in, read reference data, create teams and squads, create a match, score it
ball by ball to a result, then read every analytics/visualization/leaderboard
surface and run a tournament over its result. It is the integration check that
the sections still agree with each other.
"""

import asyncio
import io
import struct
import zlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_EMAIL = "e2e_journey@test.com"
PASSWORD = "Test@1234"


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        from botocore.exceptions import ClientError

        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Resp:
            def __init__(self, store, key):
                self._store = store
                self._key = key

            def read(self):
                return self._store[self._key]

        return {"Body": _Resp(self.objects, kwargs["Key"])}


def _png_bytes():
    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


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
            return (level.id, country.id, state.id, city.id)

    return asyncio.run(fetch())


@pytest.fixture(scope="module")
def admin(client):
    async def setup():
        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.email == ADMIN_EMAIL))
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    email=ADMIN_EMAIL,
                    hashed_password=hash_password(PASSWORD),
                    full_name="E2E Admin",
                    role="admin",
                    is_active=True,
                    is_verified=True,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
            return user

    return asyncio.run(setup())


@pytest.fixture(scope="module")
def headers(client, admin):
    resp = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _team_payload(refs, name, short):
    level_id, country_id, state_id, city_id = refs
    return {
        "name": name,
        "short_name": short,
        "level_id": level_id,
        "homeground": "E2E Ground",
        "founder": "Founder",
        "founded_year": 2001,
        "owner": "Owner",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
    }


def _player_payload(refs, *, first, last, mobile, team_id, email):
    _, country_id, state_id, city_id = refs
    return {
        "first_name": first,
        "last_name": last,
        "date_of_birth": "1995-05-20",
        "gender": "male",
        "batting_hand": "right",
        "batting_position": "opening",
        "bowling_hand": "right",
        "bowling_type": "medium",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
        "height": 178.0,
        "weight": 72.0,
        "country_code": "+91",
        "mobile_number": mobile,
        "email": email,
        "team_id": team_id,
        "role": "playing_11",
    }


def test_full_journey(client, refs, headers):
    h = headers

    # --- reference --------------------------------------------------------
    loc = client.get("/v1/locations", headers=h)
    assert loc.status_code == 200 and loc.json(), loc.text
    levels = client.get("/v1/team-levels", headers=h)
    assert levels.status_code == 200 and levels.json(), levels.text
    health = client.get("/health")
    assert health.status_code == 200

    # --- teams ------------------------------------------------------------
    teams = {}
    for name, short in [("Alpha", "ALP"), ("Bravo", "BRA"), ("Charlie", "CHA"), ("Delta", "DEL")]:
        r = client.post("/v1/teams", json=_team_payload(refs, name, short), headers=h)
        assert r.status_code == 201, r.text
        teams[name] = r.json()

    assert client.get("/v1/teams", headers=h, params={"limit": 50}).status_code == 200
    assert client.get("/v1/teams/options", headers=h).status_code == 200
    assert client.get("/v1/teams/with-counts", headers=h).status_code == 200
    alpha_id = teams["Alpha"]["id"]
    detail = client.get(f"/v1/teams/{alpha_id}", headers=h)
    assert detail.status_code == 200 and detail.json()["name"] == "Alpha"
    patched = client.patch(f"/v1/teams/{alpha_id}", json={"homeground": "Alpha Park"}, headers=h)
    assert patched.status_code == 200 and patched.json()["homeground"] == "Alpha Park"

    # --- players ----------------------------------------------------------
    squads = {}
    for team_name, prefix in (("Alpha", "A"), ("Bravo", "B")):
        ids = []
        tid_team = teams[team_name]["id"]
        for i in range(11):
            payload = _player_payload(
                refs,
                first=f"{prefix}Player{i}",
                last=f"{team_name}",
                mobile=f"9865{i:02d}{tid_team:04d}",
                team_id=tid_team,
                email=f"e2e_{prefix}{i}_{tid_team}@test.com",
            )
            r = client.post("/v1/players", json=payload, headers=h)
            assert r.status_code == 201, r.text
            ids.append(r.json()["id"])
        squads[team_name] = ids

    squad_a = client.get(f"/v1/teams/{teams['Alpha']['id']}/squad", headers=h)
    assert squad_a.status_code == 200
    assert len(squad_a.json()["playing_11"]) == 11

    assert client.get("/v1/players", headers=h, params={"limit": 50}).status_code == 200
    p0 = squads["Alpha"][0]
    assert client.get(f"/v1/players/{p0}", headers=h).status_code == 200
    pt = client.get(f"/v1/players/{p0}/teams", headers=h)
    assert pt.status_code == 200 and pt.json()[0]["team_id"] == teams["Alpha"]["id"]
    al = client.post(f"/v1/players/{p0}/aliases", json={"alias": "Ace"}, headers=h)
    assert al.status_code == 200 and "Ace" in al.json()["aliases"]

    # free agents bulk-add
    free_ids = []
    for i in range(2):
        payload = _player_payload(
            refs,
            first=f"Free{i}",
            last="Agent",
            mobile=f"99110{i:02d}0001",
            team_id=None,
            email=f"e2e_free{i}@test.com",
        )
        r = client.post("/v1/players", json=payload, headers=h)
        assert r.status_code == 201, r.text
        free_ids.append(r.json()["id"])
    bulk = client.post(
        f"/v1/teams/{teams['Alpha']['id']}/players/bulk",
        json={"player_ids": free_ids, "role": "substitute"},
        headers=h,
    )
    assert bulk.status_code == 200, bulk.text
    assert (
        client.get(f"/v1/teams/{teams['Alpha']['id']}/available-players", headers=h).status_code
        == 200
    )

    cap = client.put(
        f"/v1/teams/{teams['Alpha']['id']}/captains",
        json={
            "captain_player_id": squads["Alpha"][0],
            "vice_captain_player_id": squads["Alpha"][1],
        },
        headers=h,
    )
    assert cap.status_code == 200, cap.text

    # --- match ------------------------------------------------------------
    match_payload = {
        "match_date": date(2026, 7, 1).isoformat(),
        "match_time": "18:00",
        "venue": "E2E Oval",
        "match_type": "T20",
        "team_a_id": teams["Alpha"]["id"],
        "team_b_id": teams["Bravo"]["id"],
        "toss_winner_id": teams["Alpha"]["id"],
        "toss_decision": "bat",
        "total_overs": 1,
    }
    m = client.post("/v1/matches", json=match_payload, headers=h)
    assert m.status_code == 201, m.text
    match_id = m.json()["id"]
    assert client.get(f"/v1/matches/{match_id}", headers=h).status_code == 200
    upd_m = client.patch(f"/v1/matches/{match_id}", json={"venue": "E2E Final"}, headers=h)
    assert upd_m.status_code == 200 and upd_m.json()["venue"] == "E2E Final"

    def ball(card, runs, *, wicket=False, extra="none", extra_runs=0, bowler):
        payload = {
            "striker_id": card["striker_id"],
            "non_striker_id": card["non_striker_id"],
            "bowler_id": bowler,
            "runs_batsman": runs,
            "runs_extras": extra_runs,
            "extra_type": extra,
        }
        if wicket:
            payload["wicket_type"] = "bowled"
            payload["dismissed_player_id"] = card["striker_id"]
        r = client.post(f"/v1/matches/{match_id}/deliveries", json=payload, headers=h)
        assert r.status_code == 201, r.text
        return r.json()

    start = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": squads["Alpha"]},
        headers=h,
    )
    assert start.status_code == 200, start.text
    card = start.json()
    assert card["innings_number"] == 1

    bowl1 = squads["Bravo"][0]
    card = ball(card, 1, bowler=bowl1)
    card = ball(card, 0, wicket=True, bowler=bowl1)
    assert card["awaiting_batsman"] is True and card["striker_id"] is None
    pick = client.post(
        f"/v1/matches/{match_id}/batting-order",
        json={"player_id": squads["Alpha"][2]},
        headers=h,
    )
    assert pick.status_code == 200, pick.text
    card = pick.json()
    assert card["striker_id"] == squads["Alpha"][2]
    card = ball(card, 4, bowler=bowl1)
    card = ball(card, 6, bowler=bowl1)
    card = ball(card, 0, bowler=bowl1)
    card = ball(card, 1, bowler=bowl1)
    assert card["completed"] is True and card["total"] == 12 and card["wickets"] == 1

    start2 = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": squads["Bravo"]},
        headers=h,
    )
    assert start2.status_code == 200, start2.text
    card2 = start2.json()
    assert card2["innings_number"] == 2 and card2["target"] == 13
    bowl2 = squads["Alpha"][0]
    card2 = ball(card2, 6, bowler=bowl2)
    card2 = ball(card2, 6, bowler=bowl2)
    card2 = ball(card2, 1, bowler=bowl2)
    assert card2["completed"] is True and card2["total"] == 13 and card2["match_result"]

    # --- read endpoints after completion ----------------------------------
    sc = client.get(f"/v1/matches/{match_id}/scorecard", headers=h)
    assert sc.status_code == 200 and sc.json()["completed"]
    summ = client.get(f"/v1/matches/{match_id}/summary", headers=h)
    assert summ.status_code == 200 and summ.json()["status"] == "completed"
    ana = client.get(f"/v1/matches/{match_id}/analytics", headers=h)
    assert ana.status_code == 200 and len(ana.json()["innings"]) == 2
    one = client.get(f"/v1/matches/{match_id}/analytics", params={"innings_number": 1}, headers=h)
    assert one.status_code == 200 and len(one.json()["innings"]) == 1
    for path in (
        "runs-by-over",
        "innings-progression",
        "player-runs",
        "player-strike-rate",
        "bowler-economy",
        "wickets-by-bowler",
        "run-distribution",
        "partnerships",
    ):
        r = client.get(f"/v1/matches/{match_id}/visualizations/{path}", headers=h)
        assert r.status_code == 200, f"{path}: {r.text}"
    assert client.get(f"/v1/matches/{match_id}/partnerships", headers=h).status_code == 200

    ta = client.get(f"/v1/teams/{teams['Alpha']['id']}/analytics", headers=h)
    assert ta.status_code == 200 and ta.json()["matches_played"] == 1 and ta.json()["losses"] == 1
    h2h = client.get(
        f"/v1/teams/{teams['Alpha']['id']}/head-to-head/{teams['Bravo']['id']}", headers=h
    )
    assert h2h.status_code == 200 and h2h.json()["matches_played"] == 1
    assert (
        client.get(
            f"/v1/teams/{teams['Alpha']['id']}/visualizations/win-loss-trend", headers=h
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/v1/teams/{teams['Alpha']['id']}/visualizations/recent-form", headers=h
        ).status_code
        == 200
    )
    for path in ("performance", "profile", "career-stats"):
        r = client.get(f"/v1/players/{squads['Bravo'][0]}/" + path, headers=h)
        assert r.status_code == 200, f"{path}: {r.text}"

    assert client.get("/v1/leaderboards/batting", headers=h).status_code == 200
    assert client.get("/v1/leaderboards/bowling", headers=h).status_code == 200
    assert client.get("/v1/leaderboards/fielding", headers=h).status_code == 200

    # --- tournament -------------------------------------------------------
    tour = client.post(
        "/v1/tournaments",
        json={
            "name": "E2E Cup",
            "description": "Full journey cup",
            "format": "league_knockout",
            "status": "ongoing",
            "start_date": "2026-07-01",
        },
        headers=h,
    )
    assert tour.status_code == 201, tour.text
    tid = tour.json()["id"]

    for tname in ("Alpha", "Bravo", "Charlie", "Delta"):
        assert (
            client.post(
                f"/v1/tournaments/{tid}/teams",
                json={"team_id": teams[tname]["id"]},
                headers=h,
            ).status_code
            == 201
        )

    gen = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1}, headers=h)
    assert gen.status_code == 200 and len(gen.json()) == 6

    fixtures = gen.json()
    ab = next(
        f
        for f in fixtures
        if {f["team_a_id"], f["team_b_id"]} == {teams["Alpha"]["id"], teams["Bravo"]["id"]}
    )
    res = client.post(
        f"/v1/tournaments/{tid}/fixtures/{ab['id']}/result",
        json={"match_id": match_id},
        headers=h,
    )
    assert res.status_code == 200, res.text

    pt = client.get(f"/v1/tournaments/{tid}/points-table", headers=h)
    assert pt.status_code == 200 and pt.json()["table"]

    tl = client.get(f"/v1/tournaments/{tid}/leaderboards", headers=h)
    assert tl.status_code == 200

    ko = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4}, headers=h)
    assert ko.status_code == 200 and len(ko.json()) == 2

    assert (
        client.patch(
            f"/v1/tournaments/{tid}",
            json={"status": "completed"},
            headers=h,
        ).status_code
        == 200
    )
    assert client.get(f"/v1/tournaments/{tid}", headers=h).status_code == 200


@pytest.fixture
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr("app.storage.get_client", lambda: fake)
    return fake


def test_team_logo_and_player_photo_upload_roundtrip(client, refs, headers, fake_s3):
    team = client.post(
        "/v1/teams", json=_team_payload(refs, "Upload United", "UPU"), headers=headers
    )
    assert team.status_code == 201, team.text
    tid = team.json()["id"]

    up = client.post(
        f"/v1/teams/{tid}/upload-logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert up.status_code == 200, up.text
    logo = up.json()["logo"]
    assert logo == f"/uploads/teams/{tid}.png"
    assert client.get(f"/v1/teams/{tid}", headers=headers).json()["logo"] == logo
    served = client.get(logo)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert f"teams/{tid}.png" in fake_s3.objects

    pl = client.post(
        "/v1/players",
        json=_player_payload(
            refs,
            first="Upload",
            last="Photo",
            mobile="9876123456",
            team_id=tid,
            email="e2e_upload_photo@test.com",
        ),
        headers=headers,
    )
    assert pl.status_code == 201, pl.text
    pid = pl.json()["id"]

    up2 = client.post(
        f"/v1/players/{pid}/upload-profile-image",
        files={"file": ("face.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert up2.status_code == 200, up2.text
    img = up2.json()["profile_image"]
    assert img == f"/uploads/players/{pid}.png"
    assert client.get(img).status_code == 200
    assert f"players/{pid}.png" in fake_s3.objects
