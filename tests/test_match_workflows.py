"""End-to-end tests for the Create Match and Start-Fixture-Match workflows.

These drive the real FastAPI routes with the real database so that the wiring
between the Pydantic schemas, the CRUD layer and the HTTP responses is covered
exactly as the frontend consumes it. The two workflows under test are:

* **Create Match** - the standalone ``POST /v1/matches`` form. Covers valid
  creation, every class of invalid payload the backend refuses, and the
  "appears in the matches list with its real id" guarantee the UI relies on.

* **Start Match from a fixture** - ``POST /v1/tournaments/{id}/fixtures/{fid}/match``.
  Covers creating-and-linking from an unlinked fixture, the idempotent reuse of
  an already-linked match (repeated clicks never duplicate), the toss/date/venue
  fallbacks onto the fixture, and every refusal the backend can raise.

The setup workflow (playing XI -> start-innings -> live) and the status
transitions (scheduled -> live -> completed, and "never restart a completed
match") are asserted through the scoring endpoints so the whole chain from
"click Start Match" to "record the first ball" is covered here.

Every test creates its own fixture through ``POST /tournaments/{id}/fixtures``
so the tests are order-independent: no test can be broken by another test
having already claimed a shared generated fixture.
"""

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.matches.models import Match
from app.models import City, Country, State
from app.players.models import Player
from app.scoring.models import Innings
from app.teams.models import PlayerTeamAssignment, Team
from app.tournaments.models import Tournament, TournamentTeam

EMAIL = "match_workflows@test.com"
TEAM_NAMES = ["Workflow Alpha", "Workflow Beta", "Workflow Gamma", "Workflow Delta"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def as_admin():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(scope="module")
def world(client):
    """An admin, four 11-player squads, and a league tournament.

    The squads matter: ``start-innings`` validates that every batter in the
    batting order belongs to the batting side, so the tests can prove a mixed
    or foreign order is refused rather than silently accepted.
    """

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Workflow Admin",
                    role="admin",
                    is_active=True,
                    is_verified=True,
                )
                db.add(admin)
                await db.flush()

            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()

            def new_team(name, short):
                team = Team(
                    name=name,
                    short_name=short,
                    homeground="Workflow Ground",
                    founder="Founder",
                    founded_year=2020,
                    owner="Owner",
                    country_id=country.id,
                    state_id=state.id,
                    city_id=city.id,
                    level_id=level.id,
                    user_id=admin.id,
                )
                db.add(team)
                return team

            teams = [new_team(name, f"WF{i}") for i, name in enumerate(TEAM_NAMES)]
            await db.flush()

            def make_squad(prefix):
                ids = []
                for i in range(11):
                    player = Player(
                        first_name=f"WF{prefix}",
                        last_name=f"Player{i}",
                        date_of_birth=date(2000, 1, 1),
                        gender="male",
                        profile_image=None,
                        batting_hand="right",
                        batting_position="middle",
                        bowling_hand="right",
                        bowling_type="medium",
                        country_id=country.id,
                        state_id=state.id,
                        city_id=city.id,
                        height=175.0,
                        weight=70.0,
                        country_code="+91",
                        mobile_number=9600000000 + int(f"{prefix}{i}", 36),
                        email=f"workflow_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            squads = [make_squad(chr(65 + i)) for i in range(4)]
            await db.flush()
            for squad, team in zip(squads, teams, strict=True):
                for player in squad:
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
            await db.flush()

            tournament = Tournament(
                name="Workflow Cup",
                description="Fixture-start test competition",
                format="league",
                status="ongoing",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                user_id=admin.id,
            )
            db.add(tournament)
            await db.flush()
            for team in teams:
                db.add(TournamentTeam(tournament_id=tournament.id, team_id=team.id))
            await db.commit()

            return {
                "admin": admin,
                "tournament_id": tournament.id,
                "team_ids": [t.id for t in teams],
                "squads": {
                    t.id: [p.id for p in squad] for t, squad in zip(teams, squads, strict=True)
                },
            }

    return asyncio.run(setup())


# --- helpers -----------------------------------------------------------------


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def _match_payload(world, team_a=None, team_b=None, **overrides):
    """A valid POST /v1/matches payload using the world's first two teams."""
    ids = world["team_ids"]
    payload = {
        "match_date": "2026-06-01",
        "match_time": "19:00",
        "venue": "Workflow Stadium",
        "match_type": "T20",
        "result": "",
        "team_a_id": ids[0] if team_a is None else team_a,
        "team_b_id": ids[1] if team_b is None else team_b,
        "toss_winner_id": ids[0] if team_a is None else team_a,
        "toss_decision": "bat",
        "total_overs": None,
        "super_over_enabled": False,
        "super_over_repeat": True,
    }
    payload.update(overrides)
    return payload


def _new_fixture(client, world, team_a, team_b, **overrides):
    """Create a fresh, unlinked fixture so each test starts from a clean slate.

    It is given a schedule by default, mirroring how the real UI always sends
    date/time/venue when starting a fixture (the modal requires them). Tests
    that want to exercise the fixture-fallback pass their own values or clear
    them explicitly.
    """
    body = {
        "team_a_id": team_a,
        "team_b_id": team_b,
        "stage": "league",
        "scheduled_date": "2026-06-15",
        "scheduled_time": "19:00",
        "venue": "Workflow Ground",
    }
    body.update(overrides)
    resp = client.post(f"/v1/tournaments/{world['tournament_id']}/fixtures", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _start_url(world, fixture_id):
    return f"/v1/tournaments/{world['tournament_id']}/fixtures/{fixture_id}/match"


def _start_fixture(client, world, fixture_id, toss_winner_id, **overrides):
    payload = {"toss_winner_id": toss_winner_id, "toss_decision": "bat"}
    payload.update(overrides)
    return client.post(_start_url(world, fixture_id), json=payload)


def _mark_completed(match_id):
    async def _set():
        async with async_session() as db:
            match = (await db.execute(select(Match).where(Match.id == match_id))).scalar_one()
            match.status = "completed"
            await db.commit()

    asyncio.run(_set())


def _innings_count(match_id):
    async def _count():
        async with async_session() as db:
            rows = (
                (await db.execute(select(Innings).where(Innings.match_id == match_id)))
                .scalars()
                .all()
            )
            return len(rows)

    return asyncio.run(_count())


# --- Create Match (standalone) ----------------------------------------------


def test_create_a_valid_match_returns_201_and_persists(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post("/v1/matches", json=_match_payload(world))
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # The returned id is the real database id, not a fabricated one.
    assert body["id"] > 0
    assert body["status"] == "scheduled"
    assert body["team_a"]["id"] == world["team_ids"][0]
    assert body["team_b"]["id"] == world["team_ids"][1]
    assert body["venue"] == "Workflow Stadium"
    assert body["match_date"] == "2026-06-01"
    assert body["match_time"] == "19:00"

    # It is readable back through GET /matches/{id} with the same id.
    followup = client.get(f"/v1/matches/{body['id']}")
    assert followup.status_code == 200, followup.text
    assert followup.json()["id"] == body["id"]


def test_created_match_appears_in_the_matches_list(client, as_admin, world):
    _use_admin(as_admin, world)
    created = client.post("/v1/matches", json=_match_payload(world)).json()

    listing = client.get("/v1/matches", params={"limit": 500})
    assert listing.status_code == 200, listing.text
    ids = {m["id"] for m in listing.json()}
    assert created["id"] in ids


def test_create_match_without_required_fields_is_422(client, as_admin, world):
    _use_admin(as_admin, world)
    payload = _match_payload(world)
    # Remove two required fields the form must supply.
    payload.pop("venue")
    payload.pop("match_time")
    resp = client.post("/v1/matches", json=payload)
    assert resp.status_code == 422, resp.text
    missing = {err["loc"][-1] for err in resp.json()["detail"]}
    assert {"venue", "match_time"} <= missing


def test_create_match_with_a_bad_time_format_is_422(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post("/v1/matches", json=_match_payload(world, match_time="25:99"))
    assert resp.status_code == 422, resp.text


def test_create_match_with_an_unknown_team_is_400(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post("/v1/matches", json=_match_payload(world, team_b=99999999))
    assert resp.status_code == 400, resp.text
    assert "not found" in resp.json()["detail"].lower()


def test_create_match_with_an_unknown_toss_winner_is_400(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post("/v1/matches", json=_match_payload(world, toss_winner_id=99999999))
    assert resp.status_code == 400, resp.text


def test_create_match_with_a_toss_winner_outside_the_two_teams_is_400(client, as_admin, world):
    """The toss winner must be one of the two teams playing."""
    _use_admin(as_admin, world)
    ids = world["team_ids"]
    resp = client.post(
        "/v1/matches",
        json=_match_payload(world, team_a=ids[0], team_b=ids[1], toss_winner_id=ids[2]),
    )
    assert resp.status_code == 400, resp.text


def test_create_match_selecting_the_same_team_twice_is_400(client, as_admin, world):
    _use_admin(as_admin, world)
    same = world["team_ids"][0]
    resp = client.post("/v1/matches", json=_match_payload(world, team_a=same, team_b=same))
    assert resp.status_code == 400, resp.text
    assert "different teams" in resp.json()["detail"]


def test_create_match_requires_authentication(client, world):
    """Without an auth override the backend refuses the write (401)."""
    app.dependency_overrides.pop(get_current_user, None)
    resp = client.post("/v1/matches", json=_match_payload(world))
    assert resp.status_code == 401, resp.text


# --- Start Match from an unlinked fixture -----------------------------------


def test_start_an_unlinked_fixture_creates_and_links_a_match(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    resp = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["fixture_id"] == fixture_id
    assert body["created"] is True
    assert body["match_status"] == "scheduled"
    match_id = body["match_id"]
    assert match_id > 0

    # The created match uses the fixture's two teams.
    match = client.get(f"/v1/matches/{match_id}").json()
    assert {match["team_a"]["id"], match["team_b"]["id"]} == {team_a, team_b}

    # The fixture is now linked to that exact match in the database.
    fixtures = client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    row = next(f for f in fixtures if f["id"] == fixture_id)
    assert row["match_id"] == match_id


def test_start_fixture_falls_back_to_the_fixture_date_time_and_venue(client, as_admin, world):
    """Omitting date/time/venue in the request inherits them from the fixture."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][2], world["team_ids"][3]
    fixture_id = _new_fixture(
        client,
        world,
        team_a,
        team_b,
        scheduled_date="2026-07-02",
        scheduled_time="15:30",
        venue="Fallback Oval",
    )

    resp = _start_fixture(client, world, fixture_id, toss_winner_id=team_a, toss_decision="bowl")
    assert resp.status_code == 200, resp.text
    match_id = resp.json()["match_id"]

    match = client.get(f"/v1/matches/{match_id}").json()
    assert match["match_date"] == "2026-07-02"
    assert match["match_time"] == "15:30"
    assert match["venue"] == "Fallback Oval"
    assert match["toss_decision"] == "bowl"


def test_start_fixture_refuses_a_toss_winner_outside_the_two_teams(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    outsider = world["team_ids"][2]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    resp = _start_fixture(client, world, fixture_id, toss_winner_id=outsider)
    assert resp.status_code == 400, resp.text
    assert "toss winner" in resp.json()["detail"].lower()

    # The refused call linked nothing.
    fixtures = client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    row = next(f for f in fixtures if f["id"] == fixture_id)
    assert row["match_id"] is None


def test_start_fixture_requires_authentication(client, as_admin, world):
    # Create the fixture while still authenticated...
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    # ...then drop auth: the start call itself must be refused.
    app.dependency_overrides.pop(get_current_user, None)
    resp = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert resp.status_code == 401, resp.text


# --- Idempotency: repeated Start Match clicks -------------------------------


def test_starting_a_fixture_twice_reuses_the_same_match(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    before = client.get("/v1/matches", params={"limit": 500}).json()

    first = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["created"] is True

    second = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["match_id"] == first_body["match_id"]

    after = client.get("/v1/matches", params={"limit": 500}).json()
    # Exactly one new match was created no matter how many times it was clicked.
    assert len(after) == len(before) + 1


def test_repeated_clicks_never_create_duplicate_matches(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][2], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    ids = set()
    for _ in range(5):
        resp = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
        assert resp.status_code == 200, resp.text
        ids.add(resp.json()["match_id"])

    assert len(ids) == 1, f"repeated clicks produced {len(ids)} distinct matches"

    # The fixture still points at exactly one match.
    fixtures = client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    row = next(f for f in fixtures if f["id"] == fixture_id)
    assert row["match_id"] in ids


# --- Starting a fixture whose match is already linked -----------------------


def test_starting_a_fixture_with_an_existing_linked_match_returns_that_match(
    client, as_admin, world
):
    """A fixture linked to an already-completed match reuses it, not a new one."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    # Build a completed match between the same two teams, then link it.
    created = client.post(
        "/v1/matches", json=_match_payload(world, team_a=team_a, team_b=team_b)
    ).json()
    match_id = created["id"]
    _mark_completed(match_id)

    link = client.post(
        f"/v1/tournaments/{world['tournament_id']}/fixtures/{fixture_id}/result",
        json={"match_id": match_id},
    )
    assert link.status_code == 200, link.text

    # Now "Start Match" on that fixture must reuse the linked match.
    resp = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is False
    assert body["match_id"] == match_id
    assert body["match_status"] == "completed"


def test_starting_an_unknown_fixture_is_404(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = _start_fixture(client, world, 99999999, toss_winner_id=world["team_ids"][0])
    assert resp.status_code == 404, resp.text


def test_starting_a_fixture_of_an_unknown_tournament_is_404(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post(
        "/v1/tournaments/99999999/fixtures/1/match",
        json={"toss_winner_id": world["team_ids"][0], "toss_decision": "bat"},
    )
    assert resp.status_code == 404, resp.text


def test_fixture_associations_survive_starting_a_match(client, as_admin, world):
    """Starting one fixture leaves every other fixture's association intact."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][1], world["team_ids"][2]

    # Two pre-linked fixtures whose association must not move.
    done_a, done_b = world["team_ids"][0], world["team_ids"][3]
    fx1 = _new_fixture(client, world, done_a, done_b)
    m1 = client.post(
        "/v1/matches", json=_match_payload(world, team_a=done_a, team_b=done_b)
    ).json()["id"]
    _mark_completed(m1)
    assert (
        client.post(
            f"/v1/tournaments/{world['tournament_id']}/fixtures/{fx1}/result",
            json={"match_id": m1},
        ).status_code
        == 200
    )

    fixtures_before = {
        f["id"]: f["match_id"]
        for f in client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    }

    # Now start a brand-new fixture and confirm nothing else moved.
    fx2 = _new_fixture(client, world, team_a, team_b)
    resp = _start_fixture(client, world, fx2, toss_winner_id=team_a)
    assert resp.status_code == 200, resp.text
    new_match_id = resp.json()["match_id"]

    fixtures_after = {
        f["id"]: f["match_id"]
        for f in client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    }

    assert fixtures_after[fx2] == new_match_id
    for fid, mid in fixtures_before.items():
        if fid == fx2:
            continue
        assert fixtures_after[fid] == mid, f"fixture {fid} association changed"


def test_unlinking_a_fixture_keeps_the_match(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    unlink = client.delete(f"/v1/tournaments/{world['tournament_id']}/fixtures/{fixture_id}/result")
    assert unlink.status_code == 204, unlink.text

    # The fixture is free again...
    fixtures = client.get(f"/v1/tournaments/{world['tournament_id']}/fixtures").json()
    row = next(f for f in fixtures if f["id"] == fixture_id)
    assert row["match_id"] is None

    # ...but the match itself still exists.
    assert client.get(f"/v1/matches/{match_id}").status_code == 200


# --- Match setup workflow: playing XI -> toss -> start innings --------------


def test_saving_the_setup_starts_the_first_innings(client, as_admin, world):
    """POST start-innings with a valid playing XI opens innings 1 and goes live."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    # Toss winner bats, so the batting side is team_a; use its full squad.
    batting_order = world["squads"][team_a]
    resp = client.post(
        f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order}
    )
    assert resp.status_code == 200, resp.text
    scorecard = resp.json()

    # The first innings is open and the crease is populated for the frontend.
    assert scorecard["innings_number"] == 1
    assert scorecard["batting_team_id"] == team_a
    assert scorecard["striker_id"] == batting_order[0]
    assert scorecard["non_striker_id"] == batting_order[1]

    # The match itself moved scheduled -> live with innings counter at 1.
    match = client.get(f"/v1/matches/{match_id}").json()
    assert match["status"] == "live"
    assert match["current_innings_number"] == 1


def test_start_innings_rejects_a_player_from_the_wrong_team(client, as_admin, world):
    """A setup that mixes in the other side's players is refused, not saved."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][2], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    # team_a bats (toss winner, bat): include two of team_b's players.
    bad_order = world["squads"][team_a][:9] + world["squads"][team_b][:2]
    resp = client.post(f"/v1/matches/{match_id}/start-innings", json={"batting_order": bad_order})
    assert resp.status_code == 400, resp.text
    assert "not in the batting team" in resp.json()["detail"]

    # The match was NOT marked live by the failed setup.
    match = client.get(f"/v1/matches/{match_id}").json()
    assert match["status"] == "scheduled"
    assert match["current_innings_number"] == 0


def test_start_innings_rejects_a_single_player(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][2]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    resp = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": [world["squads"][team_a][0]]},
    )
    assert resp.status_code == 422, resp.text


def test_start_innings_rejects_duplicate_players(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][1], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    squad = world["squads"][team_a]
    resp = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": [squad[0], squad[0]]},
    )
    assert resp.status_code == 422, resp.text


# --- Continuing an in-progress match ----------------------------------------


def test_an_innings_can_be_scored_after_the_setup(client, as_admin, world):
    """A delivery is accepted once the setup has opened the first innings."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    batting_order = world["squads"][team_a]
    setup = client.post(
        f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order}
    )
    assert setup.status_code == 200, setup.text

    striker, non_striker = batting_order[0], batting_order[1]
    bowler = world["squads"][team_b][0]
    resp = client.post(
        f"/v1/matches/{match_id}/deliveries",
        json={
            "striker_id": striker,
            "non_striker_id": non_striker,
            "bowler_id": bowler,
            "extra_type": "none",
            "runs_batsman": 4,
            "runs_extras": 0,
        },
    )
    assert resp.status_code == 201, resp.text
    scorecard = resp.json()
    assert scorecard["total"] >= 4

    # The match stays live while balls are being recorded.
    match = client.get(f"/v1/matches/{match_id}").json()
    assert match["status"] == "live"


def test_reopening_the_setup_of_a_live_match_reuses_the_innings(client, as_admin, world):
    """Calling start-innings again on a live match does not open a second innings.

    The second call is refused with a clear message rather than silently
    duplicating the innings or overwriting the recorded order.
    """
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][2], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    batting_order = world["squads"][team_a]
    first = client.post(
        f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order}
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order}
    )
    # The order is already recorded, so the restart is refused - not duplicated.
    assert second.status_code == 400, second.text
    assert "already set" in second.json()["detail"]

    # Still exactly one innings row for this match.
    assert _innings_count(match_id) == 1


# --- Completed matches are never restarted ----------------------------------


def test_a_completed_match_cannot_be_scored_again(client, as_admin, world):
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][3]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    # Open the innings, then complete the match out-of-band.
    batting_order = world["squads"][team_a]
    client.post(f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order})
    _mark_completed(match_id)

    # A completed match refuses both further deliveries and a restart.
    delivery = client.post(
        f"/v1/matches/{match_id}/deliveries",
        json={
            "striker_id": batting_order[0],
            "non_striker_id": batting_order[1],
            "bowler_id": world["squads"][team_b][0],
            "extra_type": "none",
            "runs_batsman": 1,
            "runs_extras": 0,
        },
    )
    assert delivery.status_code == 409, delivery.text

    restart = client.post(
        f"/v1/matches/{match_id}/start-innings", json={"batting_order": batting_order}
    )
    assert restart.status_code == 409, restart.text


def test_starting_a_fixture_with_a_completed_match_does_not_reopen_it(client, as_admin, world):
    """Clicking Start Match on a fixture whose match is completed reuses it as-is."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][1], world["team_ids"][2]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]
    _mark_completed(match_id)

    # A second click returns the same, still-completed match: no new match, no
    # status regression to scheduled/live.
    again = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["created"] is False
    assert body["match_id"] == match_id
    assert body["match_status"] == "completed"

    # And the stored status is unchanged.
    match = client.get(f"/v1/matches/{match_id}").json()
    assert match["status"] == "completed"


# --- Tournament label on a match ---------------------------------------------


def test_standalone_match_reports_no_tournament(client, as_admin, world):
    """A match created from the Create Match form belongs to no tournament."""
    _use_admin(as_admin, world)
    resp = client.post("/v1/matches", json=_match_payload(world))
    assert resp.status_code == 201, resp.text
    assert resp.json()["tournament"] is None


def test_fixture_started_match_reports_its_tournament(client, as_admin, world):
    """A match started from a fixture carries that tournament's id and name."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]

    match = client.get(f"/v1/matches/{match_id}").json()
    tournament = match["tournament"]
    assert tournament is not None
    assert tournament["id"] == world["tournament_id"]
    assert tournament["name"] == "Workflow Cup"


def test_the_match_list_labels_tournament_and_standalone_matches(client, as_admin, world):
    """The list page shows the tournament only for fixture-linked matches."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][2], world["team_ids"][3]

    # One standalone match, one fixture-linked match, in the same list.
    standalone = client.post("/v1/matches", json=_match_payload(world, team_a, team_b))
    assert standalone.status_code == 201, standalone.text

    fixture_id = _new_fixture(client, world, team_a, team_b)
    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    linked_id = start.json()["match_id"]

    rows = client.get("/v1/matches").json()
    by_id = {row["id"]: row for row in rows}

    # The list uses the same serializer, so both are labelled in one pass.
    assert by_id[standalone.json()["id"]]["tournament"] is None
    linked = by_id[linked_id]["tournament"]
    assert linked is not None
    assert linked["id"] == world["tournament_id"]
    assert linked["name"] == "Workflow Cup"


def test_unlinking_a_fixture_clears_the_tournament_label(client, as_admin, world):
    """Detaching the fixture makes the match standalone again."""
    _use_admin(as_admin, world)
    team_a, team_b = world["team_ids"][1], world["team_ids"][2]
    fixture_id = _new_fixture(client, world, team_a, team_b)

    start = _start_fixture(client, world, fixture_id, toss_winner_id=team_a)
    assert start.status_code == 200, start.text
    match_id = start.json()["match_id"]
    assert client.get(f"/v1/matches/{match_id}").json()["tournament"] is not None

    unlink = client.delete(f"/v1/tournaments/{world['tournament_id']}/fixtures/{fixture_id}/result")
    assert unlink.status_code == 204, unlink.text

    # The match survives but no longer claims a tournament.
    assert client.get(f"/v1/matches/{match_id}").json()["tournament"] is None
