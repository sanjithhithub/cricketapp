"""End-to-end tests for the scoring HTTP API.

These drive the real FastAPI routes with the real database so that the wiring
between the Pydantic schemas, the engine and the ``ScorecardResponse`` payload
is covered too (the engine unit tests bypass all of it). The crucial assertion
in every test is ``striker_id`` / ``non_striker_id`` in the response: the
frontend relies on the backend for the crease instead of recomputing it, so a
correct engine that never reaches the payload is still a broken feature.
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
from app.teams.models import PlayerTeamAssignment, Team

EMAIL = "scoring_api@test.com"


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
def world():
    """Create an admin, two teams with an 11-player squad each, and a match."""

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Scoring Admin",
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

            def new_team(team, short):
                team.name = f"{short} Team"
                team.short_name = short
                team.homeground = "Test Ground"
                team.founder = "Founder"
                team.founded_year = 2020
                team.owner = "Owner"
                team.country_id = country.id
                team.state_id = state.id
                team.city_id = city.id
                team.level_id = level.id
                team.user_id = admin.id
                db.add(team)
                return team

            team_a = new_team(Team(), f"SCA {admin.id}")
            team_b = new_team(Team(), f"SCB {admin.id}")
            await db.flush()

            squads = {}
            for team, prefix in ((team_a, "A"), (team_b, "B")):
                ids = []
                for i in range(11):
                    email = f"scoring_{prefix}{i}_{admin.id}@test.com"
                    player = (
                        await db.execute(select(Player).where(Player.email == email))
                    ).scalar_one_or_none()
                    if player is None:
                        player = Player(
                            first_name=f"Bat{prefix}",
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
                            mobile_number=9900000000 + int(f"{prefix}{i}", 36),
                            email=email,
                        )
                        db.add(player)
                        await db.flush()
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
                    ids.append(player.id)
                squads[team.id] = ids

            match = Match(
                match_date=date(2026, 1, 1),
                match_time="18:00",
                venue="Test Ground",
                match_type="T20",
                result=None,
                team_a_id=team_a.id,
                team_b_id=team_b.id,
                toss_winner_id=team_a.id,
                toss_decision="bat",
                status="live",
                current_innings_number=0,
                user_id=admin.id,
            )
            db.add(match)
            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "batting": squads[team_a.id],
                "bowling": squads[team_b.id],
                "match_id": match.id,
            }

    return asyncio.run(setup())


@pytest.fixture
def match(client, as_admin, world):
    """A freshly started T20 first innings for the (reused) match."""
    as_admin(world["admin"])
    body = {"batting_order": world["batting"]}
    resp = client.post(f"/v1/matches/{world['match_id']}/start-innings", json=body)
    # 409/400 simply mean a previous test already started this innings, which is
    # fine: the assertions below all go through the delivery endpoint.
    assert resp.status_code in (200, 400, 409), resp.text
    return world


def _ball(client, match_id, striker, non_striker, bowler, **over):
    payload = {
        "striker_id": striker,
        "non_striker_id": non_striker,
        "bowler_id": bowler,
        "runs_batsman": 0,
        "runs_extras": 0,
        "extra_type": "none",
        "wicket_type": None,
        "dismissed_player_id": None,
    }
    payload.update(over)
    return client.post(f"/v1/matches/{match_id}/deliveries", json=payload)


def test_api_reports_the_crease_after_odd_runs_and_end_of_over(client, as_admin, match):
    as_admin(match["admin"])
    a = match["batting"]
    bowler = match["bowling"][0]

    # 1 run: the batters cross, so the striker changes.
    resp = _ball(client, match["match_id"], a[0], a[1], bowler, runs_batsman=1)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["striker_id"], body["non_striker_id"]) == (a[1], a[0])

    # 2 runs: no crossing.
    body = _ball(client, match["match_id"], a[1], a[0], bowler, runs_batsman=2).json()
    assert (body["striker_id"], body["non_striker_id"]) == (a[1], a[0])

    # A wide is an extra: the striker is NOT dismissed and does not change ends.
    body = _ball(
        client, match["match_id"], a[1], a[0], bowler, extra_type="wide", runs_extras=1
    ).json()
    assert (body["striker_id"], body["non_striker_id"]) == (a[1], a[0])
    assert body["extras"] == 1
    assert body["legal_balls"] == 2


def test_api_rejects_impossible_wide_and_no_ball_payloads(client, as_admin, match):
    as_admin(match["admin"])
    a, bowler = match["batting"], match["bowling"][0]

    # A wide with zero extra runs is not a legal delivery.
    bad = _ball(client, match["match_id"], a[0], a[1], bowler, extra_type="wide", runs_extras=0)
    assert bad.status_code == 422, bad.text

    # A bowled dismissal cannot happen off a no-ball.
    bad = _ball(
        client,
        match["match_id"],
        a[0],
        a[1],
        bowler,
        extra_type="no_ball",
        runs_extras=1,
        wicket_type="bowled",
        dismissed_player_id=a[0],
    )
    assert bad.status_code == 422, bad.text


def test_api_run_out_off_a_wide_credits_the_extras_and_rotates_the_crease(client, as_admin, match):
    as_admin(match["admin"])
    bowler = match["bowling"][0]
    card = client.get(f"/v1/matches/{match['match_id']}/scorecard").json()
    striker, non_striker = card["striker_id"], card["non_striker_id"]
    extras_before = card["extras"]

    # 1 wide + the non-striker is run out. The wide's single run is the penalty
    # the batters never ran, so the ends do NOT change before the dismissal.
    body = _ball(
        client,
        match["match_id"],
        striker,
        non_striker,
        bowler,
        extra_type="wide",
        runs_extras=1,
        wicket_type="run_out",
        dismissed_player_id=non_striker,
    ).json()

    assert body["extras"] == extras_before + 1
    assert body["wickets"] == card["wickets"] + 1
    # The non-striker was dismissed, so the incoming batter takes the non-striker
    # end and the original striker is still on strike.
    assert non_striker not in (body["striker_id"], body["non_striker_id"])
    assert body["striker_id"] == striker
    # A wide is not a legal ball.
    assert body["legal_balls"] == card["legal_balls"]
    # A run out is not credited to the bowler.
    assert all(bw["wickets"] == 0 or bw["player_id"] != bowler for bw in body["bowlers"])


def test_api_scorecard_totals_stay_consistent(client, as_admin, match):
    as_admin(match["admin"])
    body = client.get(f"/v1/matches/{match['match_id']}/scorecard").json()

    bat_total = sum(b["runs"] for b in body["batsmen"])
    assert body["total"] == bat_total + body["extras"], body

    # A wide is an extra but not a legal ball; a bye is a legal ball but not
    # charged to the bowler. Both hold in the numbers the client receives.
    for bw in body["bowlers"]:
        full, _, balls = bw["overs_str"].partition(".")
        assert int(full) * 6 + int(balls) == bw["balls_bowled"], bw
        if bw["balls_bowled"]:
            expected = round(bw["runs_conceded"] / (bw["balls_bowled"] / 6), 2)
            assert bw["economy"] == pytest.approx(expected, abs=0.01), bw


# --- POST /batting-order: manual next-batsman selection ---------------------
#
# The scorer picks who comes in for the next wicket. The endpoint must accept
# any genuinely available player and reject only on real unavailability.
# Each test below runs on its own match so the crease is in a known state.


@pytest.fixture
def new_match(client, as_admin, world):
    """Factory: start a fresh T20 first innings seeded with `order_size` batters.

    A short order leaves the rest of the squad as substitutes - players who are
    in the team but not in the batting order - which is the pool the manual
    picker has to offer. It also runs out quickly, so the wicket-pending state
    is easy to reach.
    """
    as_admin(world["admin"])

    def _start(order_size=2):
        async def setup():
            async with async_session() as db:
                match = Match(
                    match_date=date(2026, 1, 2),
                    match_time="18:00",
                    venue="Test Ground",
                    match_type="T20",
                    result=None,
                    team_a_id=world["team_a"],
                    team_b_id=world["team_b"],
                    toss_winner_id=world["team_a"],
                    toss_decision="bat",
                    status="live",
                    current_innings_number=0,
                    user_id=world["admin"].id,
                )
                db.add(match)
                await db.commit()
                return match.id

        match_id = asyncio.run(setup())
        resp = client.post(
            f"/v1/matches/{match_id}/start-innings",
            json={"batting_order": world["batting"][:order_size]},
        )
        assert resp.status_code == 200, resp.text
        return {**world, "match_id": match_id}

    return _start


def _wicket(client, match_id, striker, non_striker, bowler, dismissed):
    return _ball(
        client,
        match_id,
        striker,
        non_striker,
        bowler,
        wicket_type="bowled",
        dismissed_player_id=dismissed,
    )


def _pick(client, match_id, player_id):
    return client.post(f"/v1/matches/{match_id}/batting-order", json={"player_id": player_id})


def test_batting_order_rejects_a_player_who_is_out(client, new_match):
    m = new_match(3)
    a, bowler = m["batting"], m["bowling"][0]

    assert _wicket(client, m["match_id"], a[0], a[1], bowler, a[0]).status_code == 201

    resp = _pick(client, m["match_id"], a[0])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Player is dismissed"


def test_batting_order_rejects_a_player_already_at_the_crease(client, new_match):
    m = new_match(3)
    card = client.get(f"/v1/matches/{m['match_id']}/scorecard").json()

    for player_id in (card["striker_id"], card["non_striker_id"]):
        resp = _pick(client, m["match_id"], player_id)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Player is already at the crease"


def test_batting_order_rejects_a_player_from_the_other_team(client, new_match):
    m = new_match(3)

    resp = _pick(client, m["match_id"], m["bowling"][0])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Player not in team"


def test_batting_order_accepts_a_substitute_from_the_squad(client, new_match):
    """A squad player who was never in the XI is a valid pick."""
    m = new_match(2)
    substitute = m["batting"][5]
    a, match_id, bowler = m["batting"], m["match_id"], m["bowling"][0]

    # The order has run out, so there is a vacancy for the substitute to fill.
    assert _wicket(client, match_id, a[0], a[1], bowler, a[0]).json()["striker_id"] is None

    resp = _pick(client, match_id, substitute)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["striker_id"] == substitute
    assert body["awaiting_batsman"] is False

    row = next(b for b in body["batsmen"] if b["player_id"] == substitute)
    assert row["did_not_bat"] is False
    # A substitution does not disturb the score.
    assert body["total"] == 0
    assert body["wickets"] == 1


def test_batting_order_accepts_any_remaining_batter_not_just_the_next_one(client, new_match):
    """The scorer may pick from anywhere in the order, not only the next in line.

    This is the case that used to be rejected with "Player is already in the
    batting order".
    """
    m = new_match(11)
    a, match_id, bowler = m["batting"], m["match_id"], m["bowling"][0]

    # Dismiss the opener. The wicket vacates the striker end...
    body = _wicket(client, match_id, a[0], a[1], bowler, a[0]).json()
    assert body["striker_id"] is None
    assert body["awaiting_batsman"] is True

    # ...and the scorer picks the number 11 batter instead of a[2].
    resp = _pick(client, match_id, a[10])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["striker_id"] == a[10]
    assert body["non_striker_id"] == a[1]
    assert body["wickets"] == 1

    # a[2] never came in, so the card shows him as did-not-bat and keeps his
    # original position rather than being reshuffled to the top.
    by_id = {b["player_id"]: b for b in body["batsmen"]}
    assert by_id[a[2]]["did_not_bat"] is True
    assert by_id[a[2]]["position"] == 3
    assert by_id[a[10]]["did_not_bat"] is False
    assert by_id[a[10]]["position"] == 11

    # Scoring resumes with the hand-picked batter on strike.
    resp = _ball(client, match_id, a[10], a[1], bowler, runs_batsman=4)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 4
    assert next(b for b in body["batsmen"] if b["player_id"] == a[10])["fours"] == 1


def test_wicket_with_no_batter_left_leaves_striker_id_null(client, new_match):
    """The scoring UI opens its picker when striker_id is null.

    With a two-batter order, dismissing the striker leaves nobody to come in, so
    the innings must pause with a null striker rather than reporting the
    dismissed player as still at the crease.
    """
    m = new_match(2)
    a, match_id, bowler = m["batting"], m["match_id"], m["bowling"][0]

    # The order holds only the two openers, so dismissing the striker (who is
    # on strike) leaves nobody to come in.
    body = _wicket(client, match_id, a[0], a[1], bowler, a[0]).json()
    assert body["striker_id"] is None
    assert body["non_striker_id"] == a[1]
    assert body["completed"] is False
    assert body["awaiting_batsman"] is True
    assert body["wickets"] == 1

    # No delivery can be bowled until a batsman is picked.
    assert _ball(client, match_id, a[0], a[1], bowler).status_code == 400

    # Picking one puts them straight on strike.
    resp = _pick(client, match_id, a[4])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["striker_id"] == a[4]
    assert body["awaiting_batsman"] is False

    resp = _ball(client, match_id, a[4], a[1], bowler, runs_batsman=1)
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 1


def test_batting_order_returns_the_full_scorecard(client, new_match):
    """The client treats this payload as the source of truth."""
    m = new_match(2)
    body = _pick(client, m["match_id"], m["batting"][7]).json()

    for field in (
        "innings_id",
        "match_id",
        "innings_number",
        "batting_team_id",
        "bowling_team_id",
        "total",
        "wickets",
        "legal_balls",
        "overs_bowled",
        "overs_bowled_str",
        "current_run_rate",
        "extras",
        "completed",
        "striker_id",
        "non_striker_id",
        "batsmen",
        "bowlers",
    ):
        assert field in body, f"missing {field}"

    # The client never computes runs, wickets or overs itself, so the headline
    # numbers must already agree with the detail rows in the same payload.
    assert body["total"] == sum(b["runs"] for b in body["batsmen"]) + body["extras"]
    full, _, balls = body["overs_bowled_str"].partition(".")
    assert int(full) * 6 + int(balls) == body["legal_balls"]


def test_start_innings_still_requires_at_most_eleven_batters(client, new_match):
    """The 11-batter rule at start-innings is unchanged by manual selection."""
    m = new_match(2)
    twelve = m["batting"][:11] + [m["bowling"][0]]

    resp = client.post(f"/v1/matches/{m['match_id']}/start-innings", json={"batting_order": twelve})
    assert resp.status_code == 422, resp.text
