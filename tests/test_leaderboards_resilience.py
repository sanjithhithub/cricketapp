"""The account-wide boards must survive an innings that no longer replays.

A delivery log recorded before the current end-change rules (or any log the
engine now rejects) used to take the whole Leaderboards page down with a 500,
because the batting and bowling builders called ``get_scorecard`` unguarded. The
fix skips the bad innings the same way the team, player and tournament
aggregates already do. This seeds one clean completed match and one whose innings
cannot be replayed, then drives the real routes.
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


def _events_deliveries(order_ids: list[int], bowler_id: int, events: list[tuple[int, bool]]):
    """A legal, evenly-run delivery log the engine replays cleanly."""
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


async def _seed_innings(db, match, innings_number, batting_order, bowler_id, events, target=None):
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


def _new_match(db, admin, team_a, team_b, match_date):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Resilience Ground",
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
            admin = User(
                email="leaderboards_resilience@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Resilience Admin",
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
                    homeground="Resilience Ground",
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

            teams = [new_team("Resilience A", "RA"), new_team("Resilience B", "RB")]
            await db.flush()

            squad_ids: dict[str, int] = {}
            for team, prefix in zip(teams, ["A", "B"], strict=True):
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
                        mobile_number=9810000000 + int(f"{prefix}{i}", 36),
                        email=f"resilience_{prefix}{i}_{admin.id}@test.com",
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

            # A clean completed match that must still be counted.
            good = _new_match(db, admin, teams[0], teams[1], date(2026, 6, 1))
            await db.flush()
            await _seed_innings(db, good, 1, A, B[0], [(4, False), (2, False), (0, True)])
            await set_match_completed(db, good)

            # A completed match whose innings cannot be replayed: the dismissal
            # names a player who was never at the crease, which the engine
            # rejects during replay.
            bad = _new_match(db, admin, teams[1], teams[0], date(2026, 6, 2))
            await db.flush()
            bad_innings = Innings(
                match_id=bad.id,
                batting_team_id=bad.team_a_id,
                bowling_team_id=bad.team_b_id,
                innings_number=1,
            )
            db.add(bad_innings)
            await db.flush()
            for position, player_id in enumerate(B, start=1):
                db.add(
                    InningsBatsman(
                        innings_id=bad_innings.id, player_id=player_id, position=position
                    )
                )
            db.add(
                Delivery(
                    innings_id=bad_innings.id,
                    over_number=1,
                    ball_number=1,
                    striker_id=B[0],
                    non_striker_id=B[1],
                    bowler_id=A[0],
                    runs_batsman=0,
                    runs_extras=0,
                    extra_type=ExtraType.NONE.value,
                    wicket_type=WicketType.BOWLED.value,
                    dismissed_player_id=B[3],
                )
            )
            await set_match_completed(db, bad)

            await db.commit()
            return {"admin": admin, "players": squad_ids}

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def test_batting_boards_skip_an_unreplayable_innings(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]

    resp = client.get("/v1/leaderboards/batting", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Both completed matches are considered, but only the scoreable one's
    # batters reach a board: the impossible innings is skipped, not fatal.
    assert body["matches_considered"] == 2
    shown = {
        r["player_id"]
        for board in (
            body["most_runs"],
            body["most_fours"],
            body["highest_score"],
        )
        for r in board
    }
    assert p["A0"] in shown
    assert {p["B0"], p["B1"], p["B2"], p["B3"]}.isdisjoint(shown)


def test_bowling_boards_skip_an_unreplayable_innings(client, as_admin, world):
    _use_admin(as_admin, world)
    p = world["players"]

    resp = client.get("/v1/leaderboards/bowling", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["matches_considered"] == 2
    shown = {
        r["player_id"]
        for board in (body["most_wickets"], body["best_bowling_figures"])
        for r in board
    }
    assert p["B0"] in shown  # bowled the clean innings
    assert p["A0"] not in shown  # only bowled the unreplayable innings


def test_fielding_boards_are_unaffected(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/leaderboards/fielding")
    assert resp.status_code == 200, resp.text
    assert resp.json()["matches_considered"] == 2
