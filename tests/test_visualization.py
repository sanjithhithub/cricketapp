"""End-to-end tests for the visualization endpoints.

Seeds one completed T20 (two X-Y innings) and drives every chart route: runs by
over, innings progression, player runs and strike rates, bowler economy and
wickets, run distribution, partnerships, and the team win/loss trend and recent
form. The expected numbers below are derived ball-by-ball from the seeded
delivery log in the docstring.
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


def _events_deliveries(order_ids: list[int], bowler_id: int, events: list[tuple[int, str | None]]):
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


def _new_match(db, admin, team_a, team_b, match_date):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Viz Ground",
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
    return match


@pytest.fixture(scope="module")
def world(client):
    async def setup():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == "viz@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="viz@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Viz Admin",
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
                    homeground="Viz Ground",
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

            x_team = new_team("Team X", "X")
            y_team = new_team("Team Y", "Y")
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
                        email=f"viz_{prefix}{i}_{admin.id}@test.com",
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

            X = await squad("X", x_team, 9740000000)
            Y = await squad("Y", y_team, 9740000100)
            await db.flush()

            # X 29/1 (X1 out caught by Y1), then Y chase 30 off 5 balls.
            match = _new_match(db, admin, x_team, y_team, date(2026, 6, 1))
            await db.flush()
            await _seed_innings(
                db,
                match,
                1,
                X,
                Y[0],
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
            await _seed_innings(
                db,
                match,
                2,
                Y,
                X[0],
                [(6, None), (6, None), (6, None), (6, None), (6, None)],
                target=30,
            )
            await set_match_completed(db, match)

            await db.commit()
            return {
                "admin": admin,
                "match_id": match.id,
                "x_team": x_team,
                "y_team": y_team,
                "players": {"X": X, "Y": Y},
            }

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def test_runs_by_over(client, as_admin, world):
    _use_admin(as_admin, world)
    body = client.get(f"/v1/matches/{world['match_id']}/visualizations/runs-by-over").json()
    assert len(body["innings"]) == 2
    assert [o["runs"] for o in body["innings"][0]["overs"]] == [16, 13]
    assert [o["cumulative_runs"] for o in body["innings"][0]["overs"]] == [16, 29]
    assert body["innings"][1]["total"] == 30

    only = client.get(
        f"/v1/matches/{world['match_id']}/visualizations/runs-by-over",
        params={"innings_number": 1},
    ).json()
    assert [i["innings_number"] for i in only["innings"]] == [1]


def test_innings_progression(client, as_admin, world):
    _use_admin(as_admin, world)
    body = client.get(f"/v1/matches/{world['match_id']}/visualizations/innings-progression").json()
    inn1 = body["innings"][0]
    assert [
        (p["over_number"], p["cumulative_runs"], p["cumulative_wickets"])
        for p in inn1["progression"]
    ] == [
        (1, 16, 0),
        (2, 29, 1),
    ]
    assert inn1["progression"][-1]["run_rate"] == 14.5
    assert body["innings"][1]["progression"][0]["cumulative_runs"] == 30


def test_player_runs_and_strike_rates(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]
    body = client.get(f"/v1/matches/{world['match_id']}/visualizations/player-runs").json()
    rows = body["players"]
    assert [(r["player_id"], r["runs"]) for r in rows[:4]] == [
        (p["Y"][0], 30),
        (p["X"][0], 11),
        (p["X"][1], 11),
        (p["X"][2], 7),
    ]
    assert rows[0]["strike_rate"] == 600.0

    sr = client.get(f"/v1/matches/{world['match_id']}/visualizations/player-strike-rate").json()[
        "players"
    ]
    assert [r["player_id"] for r in sr] == [p["Y"][0], p["X"][2], p["X"][0], p["X"][1]]
    assert [r["strike_rate"] for r in sr] == [600.0, 350.0, 220.0, 220.0]


def test_bowler_charts(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]
    eco = client.get(f"/v1/matches/{world['match_id']}/visualizations/bowler-economy").json()[
        "bowlers"
    ]
    assert [(r["player_id"], r["economy"]) for r in eco] == [(p["Y"][0], 14.5), (p["X"][0], 36.0)]

    wkts = client.get(f"/v1/matches/{world['match_id']}/visualizations/wickets-by-bowler").json()[
        "bowlers"
    ]
    assert [(r["player_id"], r["wickets"]) for r in wkts] == [(p["Y"][0], 1), (p["X"][0], 0)]
    assert wkts[0]["overs_str"] == "2.0"


def test_run_distribution(client, as_admin, world):
    _use_admin(as_admin, world)
    body = client.get(f"/v1/matches/{world['match_id']}/visualizations/run-distribution").json()
    totals = body["match_totals"]
    assert totals["dot_balls"] == 1
    assert totals["ones"] == 3
    assert totals["twos"] == 5
    assert totals["fours"] == 1
    assert totals["sixes"] == 7
    assert totals["legal_balls"] == 17
    assert totals["deliveries"] == 17
    assert totals["runs_off_bat"] == 59


def test_partnerships(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]
    body = client.get(f"/v1/matches/{world['match_id']}/visualizations/partnerships").json()
    stands = body["innings"][0]["partnerships"]
    assert len(stands) == 2
    first, second = stands
    assert first["runs"] == 16
    assert first["balls"] == 7
    assert first["for_wicket"] == 1
    assert first["unbroken"] is False
    assert {first["batsman_1_id"], first["batsman_2_id"]} == {p["X"][0], p["X"][1]}
    assert {first["player_out_id"]} == {p["X"][1]}
    assert second["runs"] == 13
    assert second["balls"] == 5
    assert second["unbroken"] is True
    assert {second["batsman_1_id"], second["batsman_2_id"]} == {p["X"][0], p["X"][2]}

    chase = body["innings"][1]["partnerships"]
    assert chase[0]["runs"] == 30
    assert chase[0]["unbroken"] is True


def test_team_win_loss_trend_and_recent_form(client, as_admin, world):
    _use_admin(as_admin, world)
    trend = client.get(f"/v1/teams/{world['x_team'].id}/visualizations/win-loss-trend").json()
    assert trend["team_name"] == "Team X"
    assert trend["matches_played"] == 1
    assert (trend["wins"], trend["losses"], trend["draws"]) == (0, 1, 0)
    point = trend["trend"][0]
    assert point["result"] == "loss"
    assert point["result_code"] == "L"
    assert point["team_score"] == 29
    assert point["opponent_score"] == 30
    assert point["cumulative_matches"] == 1
    assert point["win_percentage"] == 0.0

    form = client.get(
        f"/v1/teams/{world['y_team'].id}/visualizations/recent-form", params={"limit": 5}
    ).json()
    assert form["recent_form_string"] == "W"
    assert form["recent_form"][0]["team_score"] == 30
    assert form["recent_form"][0]["opponent_score"] == 29
    assert form["recent_form"][0]["margin"] == "10 wickets"


def test_visualization_404_for_other_accounts(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/matches/999999/visualizations/player-runs")
    assert resp.status_code == 404, resp.text
    resp = client.get("/v1/teams/999999/visualizations/win-loss-trend")
    assert resp.status_code == 404, resp.text
