"""End-to-end tests for the player performance analytics endpoint.

Seeds a player's innings directly (batting order + deliveries), then drives the
real ``GET /players/{player_id}/performance`` route. The expected numbers are
worked out by hand from the delivery log so the aggregation is checked against
the actual match data, not against the code under test.
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
from app.scoring.enums import ExtraType, WicketType
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.teams.models import PlayerTeamAssignment, Team

EMAIL = "player_perf@test.com"


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


def _make_player(db, admin, country, state, city, prefix, i):
    player = Player(
        first_name=f"{prefix}Bat",
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
        mobile_number=9000000000 + int(f"{prefix}{i}", 36),
        email=f"perf_{prefix}{i}_{admin.id}@test.com",
        user_id=admin.id,
    )
    db.add(player)
    return player


async def _seed(db, admin, team_a, team_b, order_a, bowler_b, deliveries, innings_number=1):
    match = Match(
        match_date=date(2026, 2, 1),
        match_time="10:00",
        venue="Perf Ground",
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status="live",
        current_innings_number=innings_number,
        user_id=admin.id,
    )
    db.add(match)
    await db.flush()
    innings = Innings(
        match_id=match.id,
        batting_team_id=team_a.id,
        bowling_team_id=team_b.id,
        innings_number=innings_number,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(order_a, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    over = 1
    balls_in_over = 0
    for d in deliveries:
        legal = d.get("extra", ExtraType.NONE.value) not in (
            ExtraType.WIDE.value,
            ExtraType.NO_BALL.value,
        )
        db.add(
            Delivery(
                innings_id=innings.id,
                over_number=over,
                ball_number=balls_in_over + 1,
                striker_id=d["striker"],
                non_striker_id=d["non_striker"],
                bowler_id=bowler_b,
                runs_batsman=d.get("runs", 0),
                runs_extras=d.get("extras", 0),
                extra_type=d.get("extra", ExtraType.NONE.value),
                wicket_type=d.get("wicket"),
                dismissed_player_id=d.get("dismissed"),
            )
        )
        if legal:
            balls_in_over += 1
            if balls_in_over == 6:
                balls_in_over = 0
                over += 1
    await db.commit()
    return match, innings


@pytest.fixture(scope="module")
def world(client, as_admin):
    """One admin, three Team A batters and one Team B bowler, across two matches."""

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Perf Admin",
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
                    homeground="Perf Ground",
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

            team_a = new_team("Perf A", "PFA")
            team_b = new_team("Perf B", "PFB")
            await db.flush()

            a0 = _make_player(db, admin, country, state, city, "A", 0)
            a1 = _make_player(db, admin, country, state, city, "A", 1)
            a2 = _make_player(db, admin, country, state, city, "A", 2)
            b0 = _make_player(db, admin, country, state, city, "B", 0)
            await db.flush()
            for player in (a0, a1, a2):
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id, team_id=team_a.id, level_id=level.id, role="playing_11"
                    )
                )
            db.add(
                PlayerTeamAssignment(
                    player_id=b0.id, team_id=team_b.id, level_id=level.id, role="playing_11"
                )
            )
            await db.flush()

            order = [a0.id, a1.id, a2.id]

            # Match 1: a wide (1), a no-ball (1), 4, 6, dot, single, two, then a
            # bowled wicket. Team total 15, A0 11* off 5, A1 2 (out).
            m1 = await _seed(
                db,
                admin,
                team_a,
                team_b,
                order,
                b0.id,
                [
                    {
                        "striker": a0.id,
                        "non_striker": a1.id,
                        "extras": 1,
                        "extra": ExtraType.WIDE.value,
                    },
                    {"striker": a0.id, "non_striker": a1.id, "runs": 4},
                    {
                        "striker": a0.id,
                        "non_striker": a1.id,
                        "extras": 1,
                        "extra": ExtraType.NO_BALL.value,
                    },
                    {"striker": a0.id, "non_striker": a1.id, "runs": 6},
                    {"striker": a0.id, "non_striker": a1.id},
                    {"striker": a0.id, "non_striker": a1.id, "runs": 1},
                    {"striker": a1.id, "non_striker": a0.id, "runs": 2},
                    {
                        "striker": a1.id,
                        "non_striker": a0.id,
                        "wicket": WicketType.BOWLED.value,
                        "dismissed": a1.id,
                    },
                ],
            )
            # Match 2: A0 takes a single, A1 is bowled. B0's best spell (1/1).
            m2 = await _seed(
                db,
                admin,
                team_a,
                team_b,
                order,
                b0.id,
                [
                    {"striker": a0.id, "non_striker": a1.id, "runs": 1},
                    {
                        "striker": a1.id,
                        "non_striker": a0.id,
                        "wicket": WicketType.BOWLED.value,
                        "dismissed": a1.id,
                    },
                ],
            )
            await db.commit()

            as_admin(admin)
            return {
                "admin": admin,
                "a0": a0.id,
                "a1": a1.id,
                "b0": b0.id,
                "m1": m1[0].id,
                "m2": m2[0].id,
            }

    return asyncio.run(setup())


def _get(client, player_id):
    resp = client.get(f"/v1/players/{player_id}/performance")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_batsman_not_out_summary(client, world):
    body = _get(client, world["a0"])
    assert body["player_id"] == world["a0"]
    assert body["matches_played"] == 2
    assert body["innings_played"] == 2

    bat = body["batting"]
    assert bat["innings"] == 2
    assert bat["not_outs"] == 2
    assert bat["dismissals"] == 0
    assert bat["runs"] == 12
    assert bat["balls_faced"] == 6
    # Never dismissed -> average is undefined, not zero.
    assert bat["average"] is None
    assert bat["strike_rate"] == 200.0
    assert bat["highest_score"] == 11
    assert bat["highest_score_not_out"] is True
    assert bat["highest_score_display"] == "11*"
    assert bat["fours"] == 1
    assert bat["sixes"] == 1
    assert bat["dot_balls"] == 1

    # A0 never bowled.
    bowl = body["bowling"]
    assert bowl["innings"] == 0
    assert bowl["balls_bowled"] == 0
    assert bowl["economy"] == 0.0
    assert bowl["best_display"] is None


def test_dismissed_batsman_average(client, world):
    bat = _get(client, world["a1"])["batting"]
    assert bat["innings"] == 2
    assert bat["dismissals"] == 2
    assert bat["not_outs"] == 0
    assert bat["runs"] == 2
    assert bat["balls_faced"] == 3
    assert bat["average"] == 1.0
    assert bat["strike_rate"] == 66.67
    assert bat["highest_score_display"] == "2"
    assert bat["dot_balls"] == 2


def test_bowler_summary_with_best_figures(client, world):
    body = _get(client, world["b0"])
    assert body["matches_played"] == 2
    assert body["innings_played"] == 0  # never batted

    bowl = body["bowling"]
    assert bowl["innings"] == 2
    assert bowl["balls_bowled"] == 8
    assert bowl["overs"] == 1.33
    assert bowl["overs_str"] == "1.2"
    assert bowl["runs_conceded"] == 16
    assert bowl["wickets"] == 2
    assert bowl["economy"] == 12.0
    assert bowl["average"] == 8.0
    assert bowl["strike_rate"] == 4.0
    assert bowl["dot_balls"] == 3
    assert bowl["wides"] == 1
    assert bowl["no_balls"] == 1
    # Best spell is the 1/1 in match 2, not the 1/15 in match 1.
    assert bowl["best_display"] == "1/1"
    assert bowl["best_wickets"] == 1
    assert bowl["best_runs_conceded"] == 1


def test_fielding_reported_unavailable(client, world):
    fielding = _get(client, world["a0"])["fielding"]
    assert fielding["available"] is False
    assert fielding["catches"] == 0
    assert fielding["run_outs"] == 0
    assert fielding["stumpings"] == 0
    assert fielding["note"]


def test_unknown_player_is_404(client, world):
    resp = client.get("/v1/players/99999999/performance")
    assert resp.status_code == 404
