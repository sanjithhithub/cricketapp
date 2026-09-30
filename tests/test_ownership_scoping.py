import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_A_EMAIL = "admin_a@test.com"
ADMIN_B_EMAIL = "admin_b@test.com"
VIEWER_EMAIL = "viewer@test.com"


def _team_payload(refs):
    level_id, country_id, state_id, city_id = refs
    return {
        "name": "Chennai Super Kings",
        "short_name": "CSK",
        "logo": None,
        "homeground": "Chepauk",
        "founder": "N Srinivasan",
        "founded_year": 2008,
        "owner": "India Cements",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
        "level_id": level_id,
    }


def _player_payload(refs, suffix):
    level_id, country_id, state_id, city_id = refs
    return {
        "first_name": "Virat",
        "last_name": f"Kohli{suffix}",
        "date_of_birth": "1988-11-05",
        "gender": "male",
        "profile_image": None,
        "batting_hand": "right",
        "batting_position": "opening",
        "bowling_hand": "right",
        "bowling_type": "medium",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
        "height": 175.0,
        "weight": 68.0,
        "country_code": "+91",
        "mobile_number": 9876500000 + suffix,
        "email": f"player{suffix}@test.com",
        "team_id": None,
        "role": "playing_11",
    }


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
def users(client):
    async def setup():
        async with async_session() as db:
            a = User(
                email=ADMIN_A_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Admin A",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            b = User(
                email=ADMIN_B_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Admin B",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            v = User(
                email=VIEWER_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Viewer",
                role="viewer",
                is_active=True,
                is_verified=True,
            )
            db.add_all([a, b, v])
            await db.commit()
            await db.refresh(a)
            await db.refresh(b)
            await db.refresh(v)
            return {"admin_a": a, "admin_b": b, "viewer": v}

    return asyncio.run(setup())


@pytest.fixture
def as_user():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


def test_teams_are_scoped_per_account(client, users, refs, as_user):
    admin_a = users["admin_a"]
    admin_b = users["admin_b"]

    as_user(admin_a)
    create_resp = client.post("/v1/teams", json=_team_payload(refs))
    assert create_resp.status_code == 201
    team_id = create_resp.json()["id"]

    as_user(admin_a)
    own_teams = client.get("/v1/teams").json()
    assert any(t["id"] == team_id for t in own_teams)

    as_user(admin_b)
    other_teams = client.get("/v1/teams").json()
    assert other_teams == []
    assert client.get(f"/v1/teams/{team_id}").status_code == 404
    assert client.get(f"/v1/teams/{team_id}/squad").status_code == 404


def test_viewer_cannot_write(client, users, refs, as_user):
    viewer = users["viewer"]
    as_user(viewer)
    assert client.get("/v1/teams").status_code == 200
    assert client.post("/v1/teams", json=_team_payload(refs)).status_code == 403


def test_non_owner_cannot_edit_or_delete(client, users, refs, as_user):
    admin_a = users["admin_a"]
    admin_b = users["admin_b"]

    as_user(admin_a)
    team_id = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Delhi Capitals", "short_name": "DC"}
    ).json()["id"]

    as_user(admin_b)
    patch = {"short_name": "HACK"}
    assert client.patch(f"/v1/teams/{team_id}", json=patch).status_code == 404
    assert client.delete(f"/v1/teams/{team_id}").status_code == 404

    as_user(admin_a)
    assert client.delete(f"/v1/teams/{team_id}").status_code == 204


def test_team_name_unique_per_account_not_global(client, users, refs, as_user):
    admin_a = users["admin_a"]
    admin_b = users["admin_b"]

    as_user(admin_a)
    assert (
        client.post("/v1/teams", json={**_team_payload(refs), "name": "Royal Challengers"})
    ).status_code == 201

    as_user(admin_b)
    assert (
        client.post("/v1/teams", json={**_team_payload(refs), "name": "Royal Challengers"})
    ).status_code == 201

    as_user(admin_b)
    resp = client.post("/v1/teams", json={**_team_payload(refs), "name": "Royal Challengers"})
    assert resp.status_code == 409


def test_players_are_scoped_per_account(client, users, refs, as_user):
    admin_a = users["admin_a"]
    admin_b = users["admin_b"]

    as_user(admin_a)
    create_resp = client.post("/v1/players", json=_player_payload(refs, 1))
    assert create_resp.status_code == 201
    player_id = create_resp.json()["id"]

    as_user(admin_a)
    assert any(p["id"] == player_id for p in client.get("/v1/players").json())

    as_user(admin_b)
    assert client.get("/v1/players").json() == []
    assert client.get(f"/v1/players/{player_id}").status_code == 404
    assert client.post("/v1/players", json=_player_payload(refs, 2)).status_code == 201

    as_user(users["viewer"])
    assert client.post("/v1/players", json=_player_payload(refs, 3)).status_code == 403


def test_matches_are_scoped_per_account(client, users, refs, as_user):
    admin_a = users["admin_a"]
    admin_b = users["admin_b"]

    as_user(admin_a)
    team_a = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Sunrisers Hyderabad", "short_name": "SRH"},
    ).json()["id"]
    team_b = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Punjab Kings", "short_name": "PBKS"}
    ).json()["id"]
    match_payload = {
        "match_date": "2026-09-30",
        "match_time": "19:30",
        "venue": "Wankhede",
        "match_type": "T20",
        "result": None,
        "team_a_id": team_a,
        "team_b_id": team_b,
        "toss_winner_id": team_a,
        "toss_decision": "bat",
        "referee_1_name": None,
        "referee_2_name": None,
        "match_referee_name": None,
    }
    create_resp = client.post("/v1/matches", json=match_payload)
    assert create_resp.status_code == 201
    match_id = create_resp.json()["id"]

    as_user(admin_a)
    assert any(m["id"] == match_id for m in client.get("/v1/matches").json())

    as_user(admin_b)
    assert client.get("/v1/matches").json() == []
    assert client.get(f"/v1/matches/{match_id}").status_code == 404
    assert client.delete(f"/v1/matches/{match_id}").status_code == 404
