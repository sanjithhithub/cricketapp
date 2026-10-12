"""End-to-end tests for the account-wide batting leaderboards.

Seeds four matches across four teams with hand-computed per-player figures,
then drives the real route. Three of the matches are completed and one is left
live, so the boards prove both that the aggregation is right and that a match
which is still in progress never appears. Every expected number below is derived
ball-by-ball from the seeded delivery logs, so a regression in the engine or the
aggregation shows up as a mismatch.
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


def _events_deliveries(order_ids: list[int], bowler_id: int, events: list[tuple[int, bool]]):
    """A legal delivery log from ``(runs, is_wicket)`` events.

    Mirrors the proven helper in the tournament tests: wides/no-balls are never
    used, every ball is a legal one, a wicket dismisses the current striker (who
    is replaced by the next man in the order on the following ball), odd runs
    rotate the crease, and over boundaries swap the ends.
    """
    striker_i, non_striker_i = 0, 1
    next_pos = 2
    balls_in_over = 0
    over = 1
    wickets = 0
    deliveries = []
    for runs, is_wicket in events:
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
                "wicket_type": WicketType.BOWLED.value if is_wicket else None,
                "dismissed_player_id": order_ids[striker_i] if is_wicket else None,
            }
        )
        if is_wicket:
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


async def _seed_innings(
    db, match, innings_number, batting_order, bowler_id, events, target=None
):
    innings = Innings(
        match_id=match.id,
        batting_team_id=match.team_a_id if innings_number % 2 == 1 else match.team_b_id,
        bowling_team_id=match.team_b_id if innings_number % 2 == 1 else match.team_a_id,
        innings_number=innings_number,
        target=target,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(batting_order, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    for event in _events_deliveries(batting_order, bowler_id, events):
        db.add(Delivery(innings_id=innings.id, **event))
    return innings


def _new_match(db, admin, team_a, team_b, match_date, status="live"):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Leaderboard Ground",
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
                await db.execute(select(User).where(User.email == "leaderboards@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="leaderboards@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Leaderboard Admin",
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
                    homeground="Leaderboard Ground",
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
                for i in range(4):
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
                        mobile_number=9720000000 + int(f"{prefix}{i}", 36),
                        email=f"leaderboards_{prefix}{i}_{admin.id}@test.com",
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

            A = [squad_ids[f"A{i}"] for i in range(4)]
            B = [squad_ids[f"B{i}"] for i in range(4)]
            C = [squad_ids[f"C{i}"] for i in range(4)]
            D = [squad_ids[f"D{i}"] for i in range(4)]

            # M1: Team A 44 vs Team B 36. A0: 28 off 8 (5 fours, 1 six);
            # A1: 16 off 4 (2 sixes); B0: 36 off 6 (6 sixes).
            m1 = _new_match(db, admin, teams[0], teams[1], date(2026, 5, 1))
            await db.flush()
            await _seed_innings(
                db,
                m1,
                1,
                A,
                B[0],
                [
                    (4, False),
                    (4, False),
                    (6, False),
                    (1, False),
                    (6, False),
                    (6, False),
                    (4, False),
                    (4, False),
                    (4, False),
                    (1, False),
                    (2, False),
                    (2, False),
                ],
            )
            await _seed_innings(
                db, m1, 2, B, A[0], [(6, False)] * 6, target=45
            )
            await set_match_completed(db, m1)

            # M2: Team C 13 vs Team D 10. C0: 0 off 1, out (avg 0);
            # C2: 7 off 2 (1 six); D0: 4 off 2, out (avg 4).
            m2 = _new_match(db, admin, teams[2], teams[3], date(2026, 5, 2))
            await db.flush()
            await _seed_innings(
                db,
                m2,
                1,
                C,
                D[0],
                [
                    (0, True),
                    (6, False),
                    (1, False),
                    (2, False),
                    (2, False),
                    (2, False),
                ],
            )
            await _seed_innings(
                db, m2, 2, D, C[0], [(4, False), (0, True), (6, False)], target=14
            )
            await set_match_completed(db, m2)

            # M3: Team A 120 vs Team D 36. A0 and A1 each face 30 balls for 60
            # (both reach the strike-rate qualifier floor); D0 adds 36 off 6.
            m3 = _new_match(db, admin, teams[0], teams[3], date(2026, 5, 3))
            await db.flush()
            await _seed_innings(db, m3, 1, A, D[0], [(2, False)] * 60)
            await _seed_innings(db, m3, 2, D, A[0], [(6, False)] * 6, target=121)
            await set_match_completed(db, m3)

            # M4: LIVE match - B3 and B2 hit 108 off 18 each. Because it is still
            # in progress, none of it may appear on any board.
            m4 = _new_match(db, admin, teams[1], teams[3], date(2026, 5, 4), status="live")
            await db.flush()
            await _seed_innings(db, m4, 1, B, D[1], [(6, False)] * 36)

            await db.commit()
            return {
                "admin": admin,
                "players": squad_ids,
                "teams": team_ids,
            }

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def test_batting_leaderboards_full(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/leaderboards/batting", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "account"
    assert body["matches_considered"] == 3

    p = world["players"]

    runs = body["most_runs"]
    assert [r["value"] for r in runs] == [88.0, 76.0, 40.0, 36.0, 7.0, 6.0, 6.0, 0.0]
    assert runs[0]["player_id"] == p["A0"]
    assert runs[0]["team_name"] == "Team A"
    assert runs[0]["highest_score_display"] == "60*"
    assert runs[0]["strike_rate"] == 231.58

    fours = body["most_fours"]
    assert [r["value"] for r in fours] == [5.0, 1.0] + [0.0] * 6
    assert fours[0]["player_id"] == p["A0"]

    sixes = body["most_sixes"]
    assert [r["value"] for r in sixes] == [6.0, 6.0, 2.0, 1.0, 1.0, 1.0, 0.0, 0.0]
    assert sixes[0]["player_id"] == p["D0"]
    assert sixes[1]["player_id"] == p["B0"]

    top_score = body["highest_score"]
    assert [r["value"] for r in top_score] == [60.0, 60.0, 36.0, 36.0, 7.0, 6.0, 6.0, 0.0]
    assert all(r["highest_score_display"] == "60*" for r in top_score[:2])

    # The live B3/B2 innings never reach a board.
    shown = {r["player_id"] for r in runs + fours + sixes + top_score}
    assert p["B2"] not in shown
    assert p["B3"] not in shown


def test_strike_rate_qualifier_excludes_small_samples(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]

    default = client.get("/v1/leaderboards/batting").json()["highest_strike_rate"]
    names = [r["player_id"] for r in default]
    assert names == [p["A0"], p["A1"]]
    assert [r["value"] for r in default] == [231.58, 223.53]

    unqualified = client.get(
        "/v1/leaderboards/batting", params={"min_balls_faced": 0}
    ).json()["highest_strike_rate"]
    assert unqualified[0]["player_id"] == p["B0"]
    assert unqualified[0]["value"] == 600.0


def test_batting_average_qualifier(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]

    default = client.get("/v1/leaderboards/batting").json()["best_batting_average"]
    assert default == []

    relaxed = client.get(
        "/v1/leaderboards/batting", params={"min_dismissals": 1}
    ).json()["best_batting_average"]
    assert [r["player_id"] for r in relaxed] == [p["D0"], p["C0"]]
    assert [r["value"] for r in relaxed] == [40.0, 0.0]


def test_bowling_leaderboards_full(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/leaderboards/bowling", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "account"
    assert body["matches_considered"] == 3
    p = world["players"]

    wickets = body["most_wickets"]
    assert [r["player_id"] for r in wickets] == [p["C0"], p["D0"], p["B0"], p["A0"]]
    assert [r["value"] for r in wickets] == [1.0, 1.0, 0.0, 0.0]
    assert wickets[0]["team_name"] == "Team C"
    assert wickets[0]["overs_str"] == "0.3"
    assert wickets[1]["overs_str"] == "11.0"
    assert wickets[1]["economy"] == 12.09
    assert wickets[1]["runs_conceded"] == 133
    assert wickets[1]["matches"] == 2

    figures = body["best_bowling_figures"]
    assert [r["player_id"] for r in figures] == [p["C0"], p["D0"], p["A0"], p["B0"]]
    assert [r["best_figures_display"] for r in figures] == [
        "1/10",
        "1/13",
        "0/36",
        "0/44",
    ]

    dots = body["most_dot_balls"]
    assert [r["player_id"] for r in dots] == [p["C0"], p["D0"], p["B0"], p["A0"]]
    assert [r["value"] for r in dots] == [1.0, 1.0, 0.0, 0.0]

    maidens = body["most_maidens"]
    assert [r["player_id"] for r in maidens] == [p["C0"], p["D0"], p["B0"], p["A0"]]
    assert [r["value"] for r in maidens] == [0.0, 0.0, 0.0, 0.0]

    # The live D1 spell (36 balls, all sixes) never reaches a board.
    shown = {
        r["player_id"]
        for board in (wickets, figures, dots, maidens, body["best_economy"])
        for r in board
    }
    assert p["D1"] not in shown


def test_bowling_economy_qualifier(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]

    default = client.get("/v1/leaderboards/bowling").json()["best_economy"]
    assert [r["player_id"] for r in default] == [p["D0"]]
    assert default[0]["value"] == 12.09

    relaxed = client.get(
        "/v1/leaderboards/bowling", params={"min_overs": 0}
    ).json()["best_economy"]
    assert [r["player_id"] for r in relaxed] == [p["D0"], p["C0"], p["B0"], p["A0"]]
    assert [r["value"] for r in relaxed] == [12.09, 20.0, 22.0, 36.0]
