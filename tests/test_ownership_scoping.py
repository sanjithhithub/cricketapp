import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State
from app.players.models import Player
from app.scoring.models import Delivery, Innings
from app.teams.models import PlayerTeamAssignment
from app.tournaments.models import TournamentFixture

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


def _count_assignments(team_id):
    async def count():
        async with async_session() as db:
            return (
                await db.execute(
                    select(func.count(PlayerTeamAssignment.team_id)).where(
                        PlayerTeamAssignment.team_id == team_id
                    )
                )
            ).scalar()

    return asyncio.run(count())


def test_delete_team_with_squad_removes_only_assignments(client, users, refs, as_user):
    """Deleting a team drops the squad links but keeps every player record.

    Regression: deleting a team used to 500 (team_id is part of
    player_team_assignments' composite primary key and could not be nulled), and
    a caller must never lose the player rows - only the association between the
    player and the team that is going away.
    """
    as_user(users["admin_a"])
    team = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Squad Cascade FC", "short_name": "SCF"},
    ).json()
    player = client.post("/v1/players", json=_player_payload(refs, 4)).json()

    bulk = client.post(
        f"/v1/teams/{team['id']}/players/bulk",
        json={"player_ids": [player["id"]], "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text
    assert _count_assignments(team["id"]) == 1

    assert client.delete(f"/v1/teams/{team['id']}").status_code == 204
    assert _count_assignments(team["id"]) == 0

    # The player is untouched: same id, same player code, still readable.
    row = _player_row(player["id"])
    assert row is not None
    assert row.player_code == player["player_code"]
    assert client.get(f"/v1/players/{player['id']}").status_code == 200
    assert _count_assignments_for_player(player["id"]) == 0


def test_delete_team_with_match_history_is_blocked(client, users, refs, as_user):
    """A team that has played keeps its history: the delete is refused, not applied.

    Nothing may be destroyed - not the match, not its innings or deliveries, not
    the scorecard, and not the players named in it.
    """
    as_user(users["admin_a"])
    team_a = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "History Block A", "short_name": "HBA"},
    ).json()["id"]
    team_b = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "History Block B", "short_name": "HBB"},
    ).json()["id"]
    match_id, striker, _ = _scored_match(
        client, refs, a_suffix=110, b_suffix=120, team_a=team_a, team_b=team_b
    )
    assert _count_innings(match_id) == 1
    assert _count_deliveries_for_match(match_id) == 1

    resp = client.delete(f"/v1/teams/{team_a}")
    assert resp.status_code == 409, resp.text

    # Everything is exactly where it was.
    assert client.get(f"/v1/teams/{team_a}").status_code == 200
    assert client.get(f"/v1/matches/{match_id}").status_code == 200
    assert client.get(f"/v1/matches/{match_id}/scorecard").status_code == 200
    assert _count_innings(match_id) == 1
    assert _count_deliveries_for_match(match_id) == 1
    assert client.get(f"/v1/players/{striker}").status_code == 200


def test_delete_team_in_tournament_is_blocked(client, users, refs, as_user):
    """A team entered in a tournament is refused, keeping the schedule intact."""
    as_user(users["admin_a"])
    team_a = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Tour Block A", "short_name": "TBA"},
    ).json()["id"]
    team_b = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Tour Block B", "short_name": "TBB"},
    ).json()["id"]
    team_c = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Tour Block C", "short_name": "TBC"},
    ).json()["id"]
    tid = client.post(
        "/v1/tournaments",
        json={
            "name": "Preserve Cup",
            "format": "league",
            "status": "ongoing",
            "start_date": "2026-10-01",
        },
    ).json()["id"]
    for team_id in (team_a, team_b, team_c):
        assert (
            client.post(f"/v1/tournaments/{tid}/teams", json={"team_id": team_id}).status_code
            == 201
        )
    fixtures = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1}).json()
    assert len(fixtures) == 3

    assert client.delete(f"/v1/teams/{team_a}").status_code == 409

    assert client.get(f"/v1/teams/{team_a}").status_code == 200
    assert client.get(f"/v1/tournaments/{tid}").status_code == 200
    assert len(client.get(f"/v1/tournaments/{tid}/fixtures").json()) == 3


def test_reassign_player_after_its_team_is_deleted(client, users, refs, as_user):
    """A player whose team is deleted survives and can join a new team.

    Moving a player is a new association on the same player row - never a second
    player with the same details.
    """
    as_user(users["admin_a"])
    team_old = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Moving From", "short_name": "MFR"},
    ).json()
    team_new = client.post(
        "/v1/teams",
        json={**_team_payload(refs), "name": "Moving To", "short_name": "MTO"},
    ).json()
    player = client.post("/v1/players", json=_player_payload(refs, 130)).json()
    assert (
        client.post(
            f"/v1/teams/{team_old['id']}/players/bulk",
            json={"player_ids": [player["id"]], "role": "playing_11"},
        ).status_code
        == 200
    )

    assert client.delete(f"/v1/teams/{team_old['id']}").status_code == 204

    assign = client.post(
        f"/v1/players/{player['id']}/teams",
        json={"team_id": team_new["id"], "level_id": refs[0], "role": "playing_11"},
    )
    assert assign.status_code == 201, assign.text

    assert client.get(f"/v1/players/{player['id']}").status_code == 200
    assert _player_row(player["id"]).player_code == player["player_code"]
    # Exactly one player with this code, and exactly one live assignment.
    assert _count_players_with_code(player["player_code"]) == 1
    assert _count_assignments_for_player(player["id"]) == 1
    teams = client.get(f"/v1/players/{player['id']}/teams").json()
    assert [t["team_id"] for t in teams] == [team_new["id"]]


def _count_innings(match_id):
    async def count():
        async with async_session() as db:
            return (
                await db.execute(select(func.count(Innings.id)).where(Innings.match_id == match_id))
            ).scalar()

    return asyncio.run(count())


def _count_deliveries_for_match(match_id):
    async def count():
        async with async_session() as db:
            innings_ids = select(Innings.id).where(Innings.match_id == match_id)
            return (
                await db.execute(
                    select(func.count(Delivery.id)).where(Delivery.innings_id.in_(innings_ids))
                )
            ).scalar()

    return asyncio.run(count())


def _count_assignments_for_player(player_id):
    async def count():
        async with async_session() as db:
            return (
                await db.execute(
                    select(func.count(PlayerTeamAssignment.player_id)).where(
                        PlayerTeamAssignment.player_id == player_id
                    )
                )
            ).scalar()

    return asyncio.run(count())


def _player_row(player_id):
    """The raw player row, so a test can prove the identity survived a delete."""

    async def fetch():
        async with async_session() as db:
            return (
                await db.execute(select(Player).where(Player.id == player_id))
            ).scalar_one_or_none()

    return asyncio.run(fetch())


def _count_players_with_code(player_code):
    async def count():
        async with async_session() as db:
            return (
                await db.execute(
                    select(func.count(Player.id)).where(Player.player_code == player_code)
                )
            ).scalar()

    return asyncio.run(count())


def _fixture_match_id(fixture_id):
    async def fetch():
        async with async_session() as db:
            return (
                await db.execute(
                    select(TournamentFixture.match_id).where(TournamentFixture.id == fixture_id)
                )
            ).scalar()

    return asyncio.run(fetch())


def _scored_match(client, refs, *, a_suffix, b_suffix, team_a, team_b):
    """Two players a side, a one-over match with one ball bowled.

    Returns (match_id, striker_id, bowler_id) so a test can delete the match or
    a player who has actually appeared in it.
    """
    players_a = [
        client.post("/v1/players", json=_player_payload(refs, a_suffix + i)).json()["id"]
        for i in range(2)
    ]
    players_b = [
        client.post("/v1/players", json=_player_payload(refs, b_suffix + i)).json()["id"]
        for i in range(2)
    ]
    bulk = client.post(
        f"/v1/teams/{team_a}/players/bulk",
        json={"player_ids": players_a, "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text
    bulk = client.post(
        f"/v1/teams/{team_b}/players/bulk",
        json={"player_ids": players_b, "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text

    match = client.post(
        "/v1/matches",
        json={
            "match_date": "2026-10-01",
            "match_time": "19:30",
            "venue": "Wankhede",
            "match_type": "T20",
            "team_a_id": team_a,
            "team_b_id": team_b,
            "toss_winner_id": team_a,
            "toss_decision": "bat",
            "total_overs": 1,
        },
    )
    assert match.status_code == 201, match.text
    match_id = match.json()["id"]

    start = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": players_a},
    )
    assert start.status_code == 200, start.text
    card = start.json()

    ball = client.post(
        f"/v1/matches/{match_id}/deliveries",
        json={
            "striker_id": card["striker_id"],
            "non_striker_id": card["non_striker_id"],
            "bowler_id": players_b[0],
            "runs_batsman": 1,
        },
    )
    assert ball.status_code == 201, ball.text
    return match_id, card["striker_id"], players_b[0]


def test_delete_match_removes_its_scoring(client, users, refs, as_user):
    """Regression: deleting a match used to leave orphan innings and deliveries.

    The orphans do not merely linger: SQLite reuses the id of the deleted match,
    so the next match created inherits the old innings and starting it fails with
    "a first innings already exists". Deleting a match now clears its ball log.
    """
    as_user(users["admin_a"])
    team_a = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Match Delete A", "short_name": "MDA"}
    ).json()["id"]
    team_b = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Match Delete B", "short_name": "MDB"}
    ).json()["id"]

    match_id, _, _ = _scored_match(
        client, refs, a_suffix=40, b_suffix=50, team_a=team_a, team_b=team_b
    )
    assert _count_innings(match_id) == 1
    assert _count_deliveries_for_match(match_id) == 1

    assert client.delete(f"/v1/matches/{match_id}").status_code == 204
    assert _count_innings(match_id) == 0
    assert _count_deliveries_for_match(match_id) == 0


def test_delete_match_unlinks_tournament_fixture(client, users, refs, as_user):
    """A deleted match must drop out of its fixture instead of dangling."""
    as_user(users["admin_a"])
    team_a = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Fixture Unlink A", "short_name": "FUA"}
    ).json()["id"]
    team_b = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Fixture Unlink B", "short_name": "FUB"}
    ).json()["id"]

    match_id, _, _ = _scored_match(
        client, refs, a_suffix=60, b_suffix=70, team_a=team_a, team_b=team_b
    )

    tournament = client.post(
        "/v1/tournaments",
        json={
            "name": "Unlink Cup",
            "format": "league",
            "status": "ongoing",
            "start_date": "2026-10-01",
        },
    ).json()
    tid = tournament["id"]
    for team_id in (team_a, team_b):
        assert (
            client.post(f"/v1/tournaments/{tid}/teams", json={"team_id": team_id}).status_code
            == 201
        )
    fixtures = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1}).json()
    fixture = fixtures[0]
    assert (
        client.post(
            f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result", json={"match_id": match_id}
        ).status_code
        == 200
    )
    assert _fixture_match_id(fixture["id"]) == match_id

    assert client.delete(f"/v1/matches/{match_id}").status_code == 204
    assert _fixture_match_id(fixture["id"]) is None
    table = client.get(f"/v1/tournaments/{tid}/points-table").json()["table"]
    assert all(row["played"] == 0 for row in table)


def test_delete_player_with_match_data_returns_409(client, users, refs, as_user):
    """A player in the ball log cannot be deleted without corrupting a scorecard.

    The row is still referenced by deliveries, so a bare DELETE would orphan
    them and every analysis built from the ball log would break. The endpoint
    refuses and the scorecard stays readable.
    """
    as_user(users["admin_a"])
    team_a = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Player Delete A", "short_name": "PDA"}
    ).json()["id"]
    team_b = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Player Delete B", "short_name": "PDB"}
    ).json()["id"]

    match_id, striker, _ = _scored_match(
        client, refs, a_suffix=80, b_suffix=90, team_a=team_a, team_b=team_b
    )
    resp = client.delete(f"/v1/players/{striker}")
    assert resp.status_code == 409, resp.text
    assert client.get(f"/v1/matches/{match_id}/scorecard").status_code == 200


def test_delete_player_without_match_data_removes_assignments(client, users, refs, as_user):
    """A player with no scored data is deleted, taking their squad rows with them."""
    as_user(users["admin_a"])
    team = client.post(
        "/v1/teams", json={**_team_payload(refs), "name": "Clean Player FC", "short_name": "CPF"}
    ).json()
    player = client.post("/v1/players", json=_player_payload(refs, 99)).json()
    assert (
        client.post(
            f"/v1/teams/{team['id']}/players/bulk",
            json={"player_ids": [player["id"]], "role": "playing_11"},
        ).status_code
        == 200
    )
    assert _count_assignments_for_player(player["id"]) == 1

    assert client.delete(f"/v1/players/{player['id']}").status_code == 204
    assert _count_assignments_for_player(player["id"]) == 0
    assert client.get(f"/v1/players/{player['id']}").status_code == 404
