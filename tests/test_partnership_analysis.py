"""End-to-end tests for ``GET /matches/{match_id}/partnerships``.

Seeds one completed T20 with two completed innings and drives the real route.
The seeded delivery log is spelled out so the stands, runs, balls and run rates
below are worked out by hand rather than read back from the code under test.
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
from app.scoring.crud import set_match_completed
from app.scoring.enums import ExtraType, WicketType
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.teams.models import PlayerTeamAssignment, Team

EMAIL = "partnership@test.com"


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


def _events_deliveries(order_ids, bowler_id, events):
    striker_i, non_striker_i = 0, 1
    next_pos = 2
    balls_in_over = 0
    over = 1
    wickets = 0
    deliveries = []
    for runs, wicket in events:
        deliveries.append(
            {
                "over_number": over,
                "ball_number": balls_in_over + 1,
                "striker_id": order_ids[striker_i],
                "non_striker_id": order_ids[non_striker_i],
                "bowler_id": bowler_id,
                "runs_batsman": runs,
                "runs_extras": 0,
                "extra_type": ExtraType.NONE.value,
                "wicket_type": wicket,
                "dismissed_player_id": order_ids[striker_i] if wicket else None,
                "fielder_id": bowler_id if wicket == WicketType.CAUGHT.value else None,
            }
        )
        if wicket:
            wickets += 1
            if wickets < 10:
                striker_i = next_pos
                next_pos += 1
        elif runs % 2 == 1:
            striker_i, non_striker_i = non_striker_i, striker_i
        balls_in_over += 1
        if balls_in_over == 6:
            balls_in_over = 0
            over += 1
            striker_i, non_striker_i = non_striker_i, striker_i
    return deliveries


async def _seed_innings(db, match, innings_number, batting_order, bowler, events, target=None):
    batting_team_id = match.team_a_id if innings_number % 2 == 1 else match.team_b_id
    bowling_team_id = match.team_b_id if innings_number % 2 == 1 else match.team_a_id
    innings = Innings(
        match_id=match.id,
        batting_team_id=batting_team_id,
        bowling_team_id=bowling_team_id,
        innings_number=innings_number,
        target=target,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(batting_order, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    for event in _events_deliveries(batting_order, bowler, events):
        db.add(Delivery(innings_id=innings.id, **event))
    return innings


@pytest.fixture(scope="module")
def world(client):
    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Partnership Admin",
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
                    homeground="Partner Ground",
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

            team_a = new_team("Partner A", "PTA")
            team_b = new_team("Partner B", "PTB")
            await db.flush()

            async def squad(prefix, team, base):
                ids = []
                for i in range(3):
                    player = Player(
                        first_name=f"{prefix}{i}",
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
                        email=f"partner_{prefix}{i}_{admin.id}@test.com",
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

            a = await squad("A", team_a, 9730000000)
            b = await squad("B", team_b, 9730000100)

            match = Match(
                match_date=date(2026, 6, 1),
                match_time="18:00",
                venue="Partner Ground",
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
            await db.flush()

            # Innings 1: a 16-run opening stand (A0 & A1) broken by a wicket,
            # then a 13-run unbroken stand (A0 & A2). Total 29/1 off 2.0 overs.
            await _seed_innings(
                db,
                match,
                1,
                a,
                b[0],
                [
                    (4, None),
                    (1, None),
                    (6, None),
                    (2, None),
                    (2, None),
                    (1, None),
                    (0, WicketType.CAUGHT.value),
                    (6, None),
                    (1, None),
                    (2, None),
                    (2, None),
                    (2, None),
                ],
            )
            # Innings 2: the chase wins on the fifth ball, a 30-run unbroken stand.
            await _seed_innings(
                db,
                match,
                2,
                b,
                a[0],
                [(6, None), (6, None), (6, None), (6, None), (6, None)],
                target=30,
            )
            await set_match_completed(db, match)

            await db.commit()
            return {"admin": admin, "match_id": match.id, "a": a, "b": b}

    return asyncio.run(setup())


def _partnerships(client, match_id, **params):
    resp = client.get(f"/v1/matches/{match_id}/partnerships", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_innings_stands_and_callouts(client, as_admin, world):
    as_admin(world["admin"])
    body = _partnerships(client, world["match_id"])
    a = world["a"]

    assert body["match_id"] == world["match_id"]
    assert len(body["innings"]) == 2

    first = body["innings"][0]
    assert first["innings_number"] == 1
    assert first["total"] == 29
    assert first["wickets"] == 1
    assert first["legal_balls"] == 12
    assert first["overs_bowled_str"] == "2.0"
    assert first["partnership_count"] == 2

    stand1, stand2 = first["partnerships"]
    assert stand1["partnership_number"] == 1
    assert stand1["runs"] == 16
    assert stand1["balls"] == 7
    assert stand1["run_rate"] == 13.71
    assert stand1["for_wicket"] == 1
    assert stand1["unbroken"] is False
    assert {stand1["batsman_1_id"], stand1["batsman_2_id"]} == {a[0], a[1]}
    assert stand1["player_out_id"] == a[1]
    assert stand1["player_out_name"]

    assert stand2["partnership_number"] == 2
    assert stand2["runs"] == 13
    assert stand2["balls"] == 5
    assert stand2["run_rate"] == 15.6
    assert stand2["unbroken"] is True
    assert stand2["player_out_id"] is None
    assert {stand2["batsman_1_id"], stand2["batsman_2_id"]} == {a[0], a[2]}

    # The opening stand is the biggest in this innings.
    assert first["opening_partnership"]["runs"] == 16
    assert first["highest_partnership"]["runs"] == 16


def test_chase_stand_is_unbroken_and_match_best(client, as_admin, world):
    as_admin(world["admin"])
    body = _partnerships(client, world["match_id"])
    chase = body["innings"][1]

    assert chase["total"] == 30
    assert chase["wickets"] == 0
    assert chase["legal_balls"] == 5
    assert chase["overs_bowled_str"] == "0.5"
    assert len(chase["partnerships"]) == 1

    stand = chase["partnerships"][0]
    assert stand["runs"] == 30
    assert stand["balls"] == 5
    assert stand["run_rate"] == 36.0
    assert stand["unbroken"] is True

    # 30 is the biggest stand anywhere in the match.
    assert body["highest_partnership"]["runs"] == 30


def test_innings_number_filter(client, as_admin, world):
    as_admin(world["admin"])
    body = _partnerships(client, world["match_id"], innings_number=1)
    assert [i["innings_number"] for i in body["innings"]] == [1]
    assert body["highest_partnership"]["runs"] == 16


def test_each_stand_reports_the_two_batters_own_contribution(client, as_admin, world):
    """The per-batter split, worked out by hand from the seeded delivery log.

    Innings 1, opening stand: A0 5 off 2, A1 11 off 5 (16 runs, 7 balls).
    Innings 1, second stand: A2 7 off 2, A0 6 off 3 (13 runs, 5 balls).
    The contributions are shares of the pair's own runs, which here equal the
    stand total because neither stand had an extra.
    """
    as_admin(world["admin"])
    a = world["a"]
    body = _partnerships(client, world["match_id"])
    first = body["innings"][0]
    stand1, stand2 = first["partnerships"]

    assert stand1["total_runs"] == 16
    assert stand1["total_balls"] == 7
    assert stand1["batter1"]["player_id"] == a[0]
    assert (stand1["batter1"]["runs"], stand1["batter1"]["balls"]) == (5, 2)
    assert stand1["batter1"]["contribution_percentage"] == 31.25
    assert stand1["batter2"]["player_id"] == a[1]
    assert (stand1["batter2"]["runs"], stand1["batter2"]["balls"]) == (11, 5)
    assert stand1["batter2"]["contribution_percentage"] == 68.75

    assert stand2["total_runs"] == 13
    assert {stand2["batter1"]["player_id"], stand2["batter2"]["player_id"]} == {a[0], a[2]}
    runs_by_id = {
        stand2["batter1"]["player_id"]: stand2["batter1"]["runs"],
        stand2["batter2"]["player_id"]: stand2["batter2"]["runs"],
    }
    assert runs_by_id == {a[0]: 6, a[2]: 7}


def test_current_partnership_is_the_unbroken_trailing_stand(client, as_admin, world):
    as_admin(world["admin"])
    body = _partnerships(client, world["match_id"])

    first = body["innings"][0]
    # The second stand of innings 1 was still standing when the innings ended.
    assert first["current_partnership"]["partnership_number"] == 2
    assert first["current_partnership"]["runs"] == 13

    # The chase is a single unbroken stand of 30 (five sixes, all off B0).
    chase = body["innings"][1]["current_partnership"]
    assert chase["total_runs"] == 30
    assert chase["batter1"]["contribution_percentage"] == 100.0
    assert chase["batter2"]["contribution_percentage"] == 0.0


def test_partnerships_404_for_other_accounts(client, as_admin, world):
    as_admin(world["admin"])
    assert client.get("/v1/matches/999999/partnerships").status_code == 404
