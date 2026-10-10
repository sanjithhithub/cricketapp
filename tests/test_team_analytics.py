"""End-to-end tests for the team analytics endpoint.

Seeds completed matches straight into the database (batting order + deliveries,
then ``set_match_completed`` so the official result comes from the app's own
code path), then drives the real ``GET /teams/{team_id}/analytics`` route. A
live match is seeded too, to prove it is left out of the record.
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

EMAIL = "team_analytics@test.com"
TEAM_A = "Analytics CC"
TEAM_B = "Rivals CC"


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


def _run_batch(total: int) -> list[int]:
    runs = []
    remaining = total
    while remaining > 6:
        runs.append(6)
        remaining -= 6
    if remaining:
        runs.append(remaining)
    return runs


def _gen_deliveries(order_ids: list[int], bowler_id: int, total: int, wickets: int) -> list[dict]:
    """A legal ball-by-ball sequence reaching ``total`` with ``wickets`` wickets.

    Copied from the match-summary tests so the replay rebuilds exactly the same
    state: every scoring ball is a six (no crease swap), wickets dismiss the
    current striker, and over boundaries rotate the crease.
    """
    run_balls = _run_batch(total)
    in_loop_wickets = wickets - 1 if wickets >= 10 else wickets
    gap = max(1, len(run_balls) // (in_loop_wickets + 1)) if in_loop_wickets else None

    events: list[tuple[int, bool]] = []
    placed = 0
    since_wicket = 0
    for runs in run_balls:
        events.append((runs, False))
        since_wicket += 1
        if gap is not None and placed < in_loop_wickets and since_wicket >= gap:
            events.append((0, True))
            placed += 1
            since_wicket = 0
    while placed < wickets:
        events.append((0, True))
        placed += 1

    striker_i, non_striker_i = 0, 1
    next_pos = 2
    balls_in_over = 0
    over = 1
    wickets_so_far = 0
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
            wickets_so_far += 1
            if wickets_so_far < 10:
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
    db,
    match: Match,
    innings_number: int,
    batting_order: list[int],
    bowler_id: int,
    total: int,
    wickets: int,
    target: int | None = None,
) -> Innings:
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
    for d in _gen_deliveries(batting_order, bowler_id, total, wickets):
        db.add(Delivery(innings_id=innings.id, **d))
    return innings


def _new_match(db, admin, team_a, team_b, match_date, status="live"):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Team Ground",
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
                await db.execute(select(User).where(User.email == "team_analytics@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="team_analytics@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Team Analytics Admin",
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
                    homeground="Team Ground",
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

            team_a = new_team("Team Analytics A", "TAA")
            team_b = new_team("Team Analytics B", "TAB")
            team_c = new_team("Team Analytics C", "TAC")
            await db.flush()

            def make_squad(prefix):
                ids = []
                for i in range(11):
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
                        mobile_number=9700000000 + int(f"{prefix}{i}", 36),
                        email=f"team_analytics_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            squad_a = make_squad("A")
            squad_b = make_squad("B")
            await db.flush()
            squad_a_ids = [p.id for p in squad_a]
            squad_b_ids = [p.id for p in squad_b]
            for player in squad_a:
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id,
                        team_id=team_a.id,
                        level_id=level.id,
                        role="playing_11",
                    )
                )
            for player in squad_b:
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id,
                        team_id=team_b.id,
                        level_id=level.id,
                        role="playing_11",
                    )
                )
            await db.flush()

            # 1) Team A defends 120; Team B all out 110. A wins (most recent but
            #    for date ordering below it is the oldest).
            m_defend = _new_match(db, admin, team_a, team_b, date(2026, 1, 10))
            await db.flush()
            await _seed_innings(db, m_defend, 1, squad_a_ids, squad_b_ids[0], 120, 0)
            await _seed_innings(db, m_defend, 2, squad_b_ids, squad_a_ids[0], 110, 10, target=121)
            await set_match_completed(db, m_defend)

            # 2) Team A makes 90; Team B chases 91. A loses.
            m_loss = _new_match(db, admin, team_a, team_b, date(2026, 1, 20))
            await db.flush()
            await _seed_innings(db, m_loss, 1, squad_a_ids, squad_b_ids[0], 90, 0)
            await _seed_innings(db, m_loss, 2, squad_b_ids, squad_a_ids[0], 91, 0, target=91)
            await set_match_completed(db, m_loss)

            # 3) Team B makes 100; Team A chases 101. A wins. Team A is the
            #    second side here, so the innings sides are swapped.
            m_chase = _new_match(db, admin, team_b, team_a, date(2026, 1, 30))
            await db.flush()
            await _seed_innings(db, m_chase, 1, squad_b_ids, squad_a_ids[0], 100, 0)
            await _seed_innings(db, m_chase, 2, squad_a_ids, squad_b_ids[0], 101, 0, target=101)
            await set_match_completed(db, m_chase)

            # 4) A live match that must be excluded from the record.
            m_live = _new_match(db, admin, team_a, team_b, date(2026, 2, 1), status="live")
            await db.flush()
            await _seed_innings(db, m_live, 1, squad_a_ids, squad_b_ids[0], 60, 0)

            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "team_c": team_c.id,
                "top_player": squad_a_ids[0],
            }

    return asyncio.run(setup())


def _analytics(client, as_admin, world, team_id, expected=200):
    as_admin(world["admin"])
    resp = client.get(f"/v1/teams/{team_id}/analytics")
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_team_record(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])
    assert body["team_id"] == world["team_a"]
    assert body["team_name"] == "Team Analytics A"
    assert body["short_name"] == "TAA"

    assert body["matches_played"] == 3
    assert body["wins"] == 2
    assert body["losses"] == 1
    assert body["draws"] == 0
    assert body["win_percentage"] == 66.67


def test_team_run_totals(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])
    assert body["runs_scored"] == 311  # 120 + 90 + 101
    assert body["runs_conceded"] == 301  # 110 + 91 + 100
    assert body["highest_score"] == 120
    assert body["lowest_score"] == 90
    assert body["average_score"] == 103.67


def test_average_run_rate(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])
    # Team A faced 20 + 15 + 17 = 52 legal balls for 311 runs.
    assert body["average_run_rate"] == 35.88


def test_recent_form_is_most_recent_first(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])
    # 2026-01-30 win, 2026-01-20 loss, 2026-01-10 win.
    assert body["recent_form_string"] == "W-L-W"
    assert [f["result_code"] for f in body["recent_form"]] == ["W", "L", "W"]
    assert body["recent_form"][0]["opponent_name"] == "Team Analytics B"
    assert body["recent_form"][0]["opponent_id"] == world["team_b"]


def test_recent_form_limit(client, as_admin, world):
    as_admin(world["admin"])
    resp = client.get(f"/v1/teams/{world['team_a']}/analytics", params={"recent": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recent_form_string"] == "W-L"
    assert len(body["recent_form"]) == 2


def test_top_run_scorer_and_wicket_taker(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])

    # Every ball is a six, so the crease only swaps at over boundaries: the
    # opener at position 0 faces overs 1 and 3 in each innings, 191 runs total.
    top = body["top_run_scorer"]
    assert top["player_id"] == world["top_player"]
    assert top["innings"] == 3
    assert top["runs"] == 191
    assert top["highest_score"] == 72

    bowl = body["top_wicket_taker"]
    assert bowl["player_id"] == world["top_player"]
    assert bowl["innings"] == 3
    assert bowl["wickets"] == 10
    assert bowl["best_display"] == "10/110"


def test_live_match_is_excluded(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_a"])
    # Only the three completed matches count; the live 60 is not in the record.
    assert body["matches_played"] == 3
    assert body["runs_scored"] == 311


def test_no_completed_matches_is_empty(client, as_admin, world):
    body = _analytics(client, as_admin, world, world["team_c"])
    assert body["matches_played"] == 0
    assert body["wins"] == 0
    assert body["win_percentage"] == 0.0
    assert body["highest_score"] is None
    assert body["lowest_score"] is None
    assert body["average_score"] is None
    assert body["recent_form"] == []
    assert body["recent_form_string"] == ""
    assert body["top_run_scorer"] is None
    assert body["top_wicket_taker"] is None


def test_unknown_team_is_404(client, as_admin, world):
    as_admin(world["admin"])
    resp = client.get("/v1/teams/99999999/analytics")
    assert resp.status_code == 404
