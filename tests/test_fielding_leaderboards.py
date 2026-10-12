"""End-to-end tests for the account-wide fielding leaderboards.

Seeds two completed matches whose catch / run-out / stumping dismissals name a
fielder, plus one live match that must never reach a board. The chase innings of
the first match ends on a caught delivery that also reaches the target, proving
the winning ball's wicket is never credited - exactly as the score engine
ignores it.
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

TEAMS = ["Team A", "Team B", "Team C", "Team D"]


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


def _events_deliveries(
    order_ids: list[int], bowler_id: int, events: list[tuple[int, str | None, int | None]]
):
    """A legal delivery log from ``(runs, wicket_type, fielder_id)`` events."""
    striker_i, non_striker_i = 0, 1
    next_pos = 2
    balls_in_over = 0
    over = 1
    wickets = 0
    deliveries = []
    for runs, wicket, fielder_id in events:
        dismissed = order_ids[striker_i] if wicket else None
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
                "dismissed_player_id": dismissed,
                "fielder_id": fielder_id,
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


async def _seed_innings(db, match, innings_number, batting_order, bowlers, events, target=None):
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
    for event in _events_deliveries(batting_order, bowlers, events):
        db.add(Delivery(innings_id=innings.id, **event))
    return innings


def _new_match(db, admin, team_a, team_b, match_date, status="live"):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Fielding Ground",
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status=status,
        current_innings_number=0,
        user_id=admin.id,
    )
    db.add(match)
    return match


@pytest.fixture(scope="module")
def world(client):
    async def setup():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == "fielding@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="fielding@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Fielding Admin",
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
                    homeground="Fielding Ground",
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

            teams = [new_team(name, f"{name[-1]}") for name in TEAMS]
            await db.flush()
            team_ids = {name: team.id for name, team in zip(TEAMS, teams, strict=True)}

            squad_ids: dict[str, int] = {}
            for team, prefix in zip(teams, ["A", "B", "C", "D"], strict=True):
                for i in range(6):
                    player = Player(
                        first_name=f"{prefix}{i}",
                        last_name="Player",
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
                        mobile_number=9730000000 + int(f"{prefix}{i}", 36),
                        email=f"fielding_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    await db.flush()
                    squad_ids[f"{prefix}{i}"] = player.id
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
            await db.flush()

            A = [squad_ids[f"A{i}"] for i in range(6)]
            B = [squad_ids[f"B{i}"] for i in range(6)]
            C = [squad_ids[f"C{i}"] for i in range(6)]
            D = [squad_ids[f"D{i}"] for i in range(6)]

            # M1: B0 takes a catch and a stumping; B1 a run out. The chase ends
            # with a caught delivery that reaches the target - that fielder is
            # never credited.
            m1 = _new_match(db, admin, teams[0], teams[1], date(2026, 6, 1))
            await db.flush()
            await _seed_innings(
                db,
                m1,
                1,
                A,
                B[0],
                [
                    (0, WicketType.CAUGHT.value, B[0]),
                    (1, WicketType.RUN_OUT.value, B[1]),
                    (4, WicketType.STUMPED.value, B[0]),
                    (2, None, None),
                    (6, None, None),
                    (1, None, None),
                ],
            )
            await _seed_innings(
                db,
                m1,
                2,
                B,
                A[0],
                [(6, None, None), (6, None, None), (4, WicketType.CAUGHT.value, A[0])],
                target=15,
            )
            await set_match_completed(db, m1)

            # M2: C0 (catch + run out) and C1 (catch + stumping). A1's catch on
            # the winning ball of the chase does not count either.
            m2 = _new_match(db, admin, teams[0], teams[2], date(2026, 6, 2))
            await db.flush()
            await _seed_innings(
                db,
                m2,
                1,
                A,
                C[0],
                [
                    (0, WicketType.CAUGHT.value, C[0]),
                    (0, WicketType.CAUGHT.value, C[1]),
                    (2, WicketType.RUN_OUT.value, C[0]),
                    (1, WicketType.STUMPED.value, C[1]),
                    (6, None, None),
                    (6, None, None),
                ],
            )
            await _seed_innings(
                db,
                m2,
                2,
                C,
                A[0],
                [(6, None, None), (6, None, None), (4, WicketType.CAUGHT.value, A[1])],
                target=16,
            )
            await set_match_completed(db, m2)

            # M3: LIVE - D0's catch must never reach a board.
            m3 = _new_match(db, admin, teams[0], teams[3], date(2026, 6, 3), status="live")
            await db.flush()
            await _seed_innings(db, m3, 1, A, D[0], [(0, WicketType.CAUGHT.value, D[0])])

            await db.commit()
            return {
                "admin": admin,
                "players": squad_ids,
                "teams": team_ids,
            }

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def _ids(board):
    return {row["player_id"] for row in board}


def test_fielding_leaderboards_full(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/leaderboards/fielding", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "account"
    assert body["matches_considered"] == 2

    p = world["players"]

    catches = body["most_catches"]
    assert [r["value"] for r in catches] == [1.0, 1.0, 1.0]
    assert _ids(catches) == {p["B0"], p["C0"], p["C1"]}
    assert next(r for r in catches if r["player_id"] == p["B0"])["team_name"] == "Team B"
    assert all(r["catches"] == 1 and r["total_dismissals"] == 2 for r in catches)

    run_outs = body["most_run_outs"]
    assert [r["value"] for r in run_outs] == [1.0, 1.0]
    assert _ids(run_outs) == {p["B1"], p["C0"]}

    stumpings = body["most_stumpings"]
    assert [r["value"] for r in stumpings] == [1.0, 1.0]
    assert _ids(stumpings) == {p["B0"], p["C1"]}

    # The winning-ball catches never credit anyone, and the live D0 catch is
    # nowhere either.
    shown = _ids(catches) | _ids(run_outs) | _ids(stumpings)
    for excluded in (p["A0"], p["A1"], p["D0"]):
        assert excluded not in shown, excluded
