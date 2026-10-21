"""End-to-end tests for the live ``partnership`` block on the scorecard.

These drive the real scoring routes with the real database, so the partnership
is covered all the way from the delivery POST through the engine to the
``ScorecardResponse`` payload - the same path the scoring UI uses.

The match is scored ball by ball and every figure is worked out by hand from the
deliveries that were posted, so the test does not read the code's own answer
back.
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

EMAIL = "partnership_tracking@test.com"


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
    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Partnership Tracking Admin",
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
                    homeground="Tracking Ground",
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

            team_a = new_team("Track A", "TKA")
            team_b = new_team("Track B", "TKB")
            await db.flush()

            async def squad(prefix, team, base, count):
                ids = []
                for i in range(count):
                    player = Player(
                        first_name=f"P{prefix}{i}",
                        last_name="Cricketer",
                        date_of_birth=date(2001, 1, 1),
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
                        mobile_number=base + i,
                        email=f"ptrack_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    await db.flush()
                    ids.append(player.id)
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
                await db.flush()
                return ids

            a = await squad("A", team_a, 9740000000, 4)
            b = await squad("B", team_b, 9740000100, 2)

            match = Match(
                match_date=date(2026, 7, 1),
                match_time="18:00",
                venue="Tracking Ground",
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
            return {"admin": admin, "match_id": match.id, "batting": a, "bowling": b}

    return asyncio.run(setup())


@pytest.fixture
def started(client, as_admin, world):
    as_admin(world["admin"])
    resp = client.post(
        f"/v1/matches/{world['match_id']}/start-innings",
        json={"batting_order": world["batting"]},
    )
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


def test_scorecard_reports_the_opening_stand_with_each_batters_share(client, as_admin, started):
    as_admin(started["admin"])
    a, bowler, match_id = started["batting"], started["bowling"][0], started["match_id"]

    # Ball 1: A0 hits four. Ball 2: A0 takes a single, so the ends rotate.
    body = _ball(client, match_id, a[0], a[1], bowler, runs_batsman=4).json()
    card = body["partnership"]
    assert card["batter1"]["player_id"] == a[0]
    assert card["batter1"]["name"] == "PA0 Cricketer"
    assert (card["batter1"]["runs"], card["batter1"]["balls"]) == (4, 1)
    assert card["batter2"]["player_id"] == a[1]
    assert (card["batter2"]["runs"], card["batter2"]["balls"]) == (0, 0)
    assert card["total_runs"] == 4
    assert card["total_balls"] == 1
    assert card["run_rate"] == 24.0
    assert card["batter1"]["contribution_percentage"] == 100.0
    assert card["batter2"]["contribution_percentage"] == 0.0

    body = _ball(client, match_id, a[0], a[1], bowler, runs_batsman=1).json()
    # The single rotates the strike: A1 is now on strike, A0 at the other end.
    assert (body["striker_id"], body["non_striker_id"]) == (a[1], a[0])
    body = _ball(client, match_id, a[1], a[0], bowler, runs_batsman=2).json()
    card = body["partnership"]
    assert (card["batter1"]["runs"], card["batter1"]["balls"]) == (5, 2)
    assert (card["batter2"]["runs"], card["batter2"]["balls"]) == (2, 1)
    assert card["total_runs"] == 7
    assert card["total_balls"] == 3
    # 7 runs off 3 balls is 14.00 an over.
    assert card["run_rate"] == 14.0
    # Contribution is against the pair's own runs (7), 5 and 2.
    assert card["batter1"]["contribution_percentage"] == 71.43
    assert card["batter2"]["contribution_percentage"] == 28.57

    # A wide is an extra: the stand total goes up but the ball count does not,
    # and no batsman's own figures move.
    body = _ball(client, match_id, a[1], a[0], bowler, extra_type="wide", runs_extras=1).json()
    card = body["partnership"]
    assert card["total_runs"] == 8
    assert card["total_balls"] == 3
    assert (card["batter2"]["runs"], card["batter2"]["balls"]) == (2, 1)
    assert body["extras"] == 1


def test_a_wicket_leaves_no_current_partnership(client, as_admin, started):
    as_admin(started["admin"])
    bowler, match_id = started["bowling"][0], started["match_id"]

    # The first test already bowled three legal balls and a wide; the mise en
    # scène makes no assumption about the score, only about who is at the crease.
    card = client.get(f"/v1/matches/{match_id}/scorecard").json()
    striker, non_striker = card["striker_id"], card["non_striker_id"]
    assert card["partnership"] is not None

    body = _ball(
        client,
        match_id,
        striker,
        non_striker,
        bowler,
        wicket_type="bowled",
        dismissed_player_id=striker,
    ).json()

    assert body["wickets"] == card["wickets"] + 1
    # A wicket closes the stand; until the incoming batsman is picked there is no
    # partnership at the crease.
    assert body["partnership"] is None
    assert body["awaiting_batsman"] is True


def test_the_next_batsman_opens_a_brand_new_stand(client, as_admin, started):
    as_admin(started["admin"])
    a, bowler, match_id = started["batting"], started["bowling"][0], started["match_id"]

    # Pick a fresh batsman for the vacancy the wicket left.
    incoming = a[2]
    resp = client.post(f"/v1/matches/{match_id}/batting-order", json={"player_id": incoming})
    assert resp.status_code == 200, resp.text

    card = resp.json()
    striker, non_striker = card["striker_id"], card["non_striker_id"]

    body = _ball(client, match_id, striker, non_striker, bowler, runs_batsman=6).json()
    partnership = body["partnership"]
    assert partnership is not None
    assert partnership["total_runs"] == 6
    assert partnership["total_balls"] == 1
    assert partnership["run_rate"] == 36.0
    # The new stand is exactly the two batsmen now at the crease, and the incoming
    # player is one of them.
    pair = {partnership["batter1"]["player_id"], partnership["batter2"]["player_id"]}
    assert pair == {striker, non_striker}
    assert incoming in pair
