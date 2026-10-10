"""End-to-end tests for the player profile endpoint.

Seeds a player's innings directly (batting order + deliveries) across several
matches and levels, then drives the real ``GET /players/{player_id}/profile``
route. The expected numbers are worked out by hand from the delivery log, and a
separate live match proves the profile counts only completed matches.
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

EMAIL = "player_profile@test.com"
LEVEL_ONE = "Profile League One"
LEVEL_TWO = "Profile League Two"


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
        mobile_number=9300000000 + int(f"{prefix}{i}", 36),
        email=f"profile_{prefix}{i}_{admin.id}@test.com",
        user_id=admin.id,
    )
    db.add(player)
    return player


async def _seed(db, admin, team_a, team_b, order_a, bowler_b, deliveries, *, status):
    match = Match(
        match_date=date(2026, 3, 1),
        match_time="10:00",
        venue="Profile Ground",
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status=status,
        current_innings_number=1,
        user_id=admin.id,
    )
    db.add(match)
    await db.flush()
    innings = Innings(
        match_id=match.id,
        batting_team_id=team_a.id,
        bowling_team_id=team_b.id,
        innings_number=1,
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
    return match


@pytest.fixture(scope="module")
def world(client, as_admin):
    """One player, a0, bats for two different levels plus one live match."""

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Profile Admin",
                    role="admin",
                    is_active=True,
                    is_verified=True,
                )
                db.add(admin)
                await db.flush()

            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()

            # Levels are looked up or created so a re-run against an existing
            # database (the suite shares one) does not hit the unique name.
            level_one = (
                await db.execute(select(TeamLevel).where(TeamLevel.name == LEVEL_ONE))
            ).scalar_one_or_none()
            if level_one is None:
                level_one = TeamLevel(name=LEVEL_ONE)
                db.add(level_one)
            level_two = (
                await db.execute(select(TeamLevel).where(TeamLevel.name == LEVEL_TWO))
            ).scalar_one_or_none()
            if level_two is None:
                level_two = TeamLevel(name=LEVEL_TWO)
                db.add(level_two)
            await db.flush()

            def new_team(name, short, level):
                team = Team(
                    name=name,
                    short_name=short,
                    homeground="Profile Ground",
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

            a1 = new_team("Profile One A", "P1A", level_one)
            b1 = new_team("Profile One B", "P1B", level_one)
            a2 = new_team("Profile Two A", "P2A", level_two)
            b2 = new_team("Profile Two B", "P2B", level_two)
            await db.flush()

            a0 = _make_player(db, admin, country, state, city, "A", 0)
            mate = _make_player(db, admin, country, state, city, "A", 1)
            bowler = _make_player(db, admin, country, state, city, "B", 0)
            await db.flush()
            for team in (a1, a2):
                db.add(
                    PlayerTeamAssignment(
                        player_id=a0.id, team_id=team.id, level_id=team.level_id, role="playing_11"
                    )
                )
            db.add(
                PlayerTeamAssignment(
                    player_id=mate.id, team_id=a1.id, level_id=level_one.id, role="playing_11"
                )
            )
            db.add(
                PlayerTeamAssignment(
                    player_id=bowler.id,
                    team_id=b1.id,
                    level_id=level_one.id,
                    role="playing_11",
                )
            )
            await db.flush()

            order = [a0.id, mate.id]

            # Completed, League One: a0 single, mate bowled. a0 1 run.
            await _seed(
                db,
                admin,
                a1,
                b1,
                order,
                bowler.id,
                [
                    {"striker": a0.id, "non_striker": mate.id, "runs": 1},
                    {
                        "striker": mate.id,
                        "non_striker": a0.id,
                        "wicket": WicketType.BOWLED.value,
                        "dismissed": mate.id,
                    },
                ],
                status="completed",
            )
            # Completed, League Two: a0 four and a dot. a0 4 runs.
            await _seed(
                db,
                admin,
                a2,
                b2,
                order,
                bowler.id,
                [
                    {"striker": a0.id, "non_striker": mate.id, "runs": 4},
                    {"striker": a0.id, "non_striker": mate.id},
                ],
                status="completed",
            )
            # Live, League One: a0 six. Must NOT count on the profile.
            await _seed(
                db,
                admin,
                a1,
                b1,
                order,
                bowler.id,
                [{"striker": a0.id, "non_striker": mate.id, "runs": 6}],
                status="live",
            )
            await db.commit()

            as_admin(admin)
            return {"admin": admin, "a0": a0.id}

    return asyncio.run(setup())


def _get(client, player_id):
    resp = client.get(f"/v1/players/{player_id}/profile")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_profile_has_player_identity(client, world):
    body = _get(client, world["a0"])
    assert body["player"]["id"] == world["a0"]
    assert body["player"]["full_name"]
    assert body["player"]["player_code"]


def test_career_counts_completed_matches_only(client, world):
    career = _get(client, world["a0"])["career"]
    # Two completed matches; the live one is excluded.
    assert career["matches_played"] == 2
    assert career["batting"]["innings"] == 2
    assert career["batting"]["runs"] == 5  # 1 + 4, not the live 6
    assert career["batting"]["balls_faced"] == 3
    assert career["batting"]["fours"] == 1
    assert career["batting"]["dot_balls"] == 1
    assert career["batting"]["not_outs"] == 2
    assert career["batting"]["average"] is None


def test_competitions_split_by_level(client, world):
    body = _get(client, world["a0"])
    comps = {c["level_name"]: c for c in body["competitions"]}
    assert set(comps) == {LEVEL_ONE, LEVEL_TWO}

    one = comps[LEVEL_ONE]
    assert one["level_id"]
    assert one["matches_played"] == 1
    assert one["batting"]["runs"] == 1

    two = comps[LEVEL_TWO]
    assert two["matches_played"] == 1
    assert two["batting"]["runs"] == 4


def test_competition_totals_sum_to_career(client, world):
    body = _get(client, world["a0"])
    total_runs = sum(c["batting"]["runs"] for c in body["competitions"])
    assert total_runs == body["career"]["batting"]["runs"]
    total_matches = sum(c["matches_played"] for c in body["competitions"])
    assert total_matches == body["career"]["matches_played"]


def test_profile_fielding_unavailable(client, world):
    fielding = _get(client, world["a0"])["career"]["fielding"]
    assert fielding["available"] is False
    assert fielding["note"]


def test_unknown_player_profile_is_404(client, world):
    resp = client.get("/v1/players/99999999/profile")
    assert resp.status_code == 404
