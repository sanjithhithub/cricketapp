"""End-to-end tests for the head-to-head analytics endpoint.

Seeds completed matches straight into the database (batting order + deliveries,
then ``set_match_completed`` so the official result comes from the app's own code
path), then drives the real
``GET /teams/{team_id}/head-to-head/{opponent_id}`` route.

The fixture builds three rivalries: three games between A and B, one between A
and C (which must not leak into the A-vs-B numbers) and one live A-vs-B game
(which must be ignored entirely).
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

EMAIL = "head_to_head@test.com"


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
        venue="Rivalry Ground",
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
                await db.execute(select(User).where(User.email == "head_to_head@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="head_to_head@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Head To Head Admin",
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
                    homeground="Rivalry Ground",
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

            team_a = new_team("Head To Head A", "HHA")
            team_b = new_team("Head To Head B", "HHB")
            team_c = new_team("Head To Head C", "HHC")
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
                        mobile_number=9710000000 + int(f"{prefix}{i}", 36),
                        email=f"head_to_head_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            squad_a = make_squad("HA")
            squad_b = make_squad("HB")
            squad_c = make_squad("HC")
            await db.flush()
            squad_a_ids = [p.id for p in squad_a]
            squad_b_ids = [p.id for p in squad_b]
            squad_c_ids = [p.id for p in squad_c]
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
            for player in squad_c:
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id,
                        team_id=team_c.id,
                        level_id=level.id,
                        role="playing_11",
                    )
                )
            await db.flush()

            # Meeting 1) A defends 150, B all out 120. A wins by 30.
            m1 = _new_match(db, admin, team_a, team_b, date(2026, 3, 10))
            await db.flush()
            await _seed_innings(db, m1, 1, squad_a_ids, squad_b_ids[0], 150, 0)
            await _seed_innings(db, m1, 2, squad_b_ids, squad_a_ids[0], 120, 10, target=151)
            await set_match_completed(db, m1)

            # Meeting 2) B makes 140; A chases 141. A wins. A is the away side.
            m2 = _new_match(db, admin, team_b, team_a, date(2026, 3, 20))
            await db.flush()
            await _seed_innings(db, m2, 1, squad_b_ids, squad_a_ids[0], 140, 0)
            await _seed_innings(db, m2, 2, squad_a_ids, squad_b_ids[0], 141, 0, target=141)
            await set_match_completed(db, m2)

            # Meeting 3) A makes 100; B chases 101. B wins. B's opener is player
            # of the match, chosen by hand.
            m3 = _new_match(db, admin, team_a, team_b, date(2026, 3, 30))
            await db.flush()
            await _seed_innings(db, m3, 1, squad_a_ids, squad_b_ids[0], 100, 0)
            await _seed_innings(db, m3, 2, squad_b_ids, squad_a_ids[0], 101, 0, target=101)
            m3.player_of_match_id = squad_b_ids[0]
            await set_match_completed(db, m3)

            # A vs C: A posts 200 and rolls C for 50. Must never appear in the
            # A-vs-B numbers.
            m_c = _new_match(db, admin, team_a, team_c, date(2026, 4, 1))
            await db.flush()
            await _seed_innings(db, m_c, 1, squad_a_ids, squad_c_ids[0], 200, 0)
            await _seed_innings(db, m_c, 2, squad_c_ids, squad_a_ids[0], 50, 10, target=201)
            await set_match_completed(db, m_c)

            # A live A-vs-B game that must be excluded.
            m_live = _new_match(db, admin, team_a, team_b, date(2026, 4, 10), status="live")
            await db.flush()
            await _seed_innings(db, m_live, 1, squad_a_ids, squad_b_ids[0], 80, 0)

            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "team_c": team_c.id,
                "match_1": m1.id,
                "match_3": m3.id,
                "top_player": squad_a_ids[0],
                "pom_player": squad_b_ids[0],
            }

    return asyncio.run(setup())


def _h2h(client, as_admin, world, team_id, opponent_id, params=None, expected=200):
    as_admin(world["admin"])
    resp = client.get(f"/v1/teams/{team_id}/head-to-head/{opponent_id}", params=params or {})
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_record_and_scores(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"])
    assert body["team_id"] == world["team_a"]
    assert body["team_name"] == "Head To Head A"
    assert body["team_short_name"] == "HHA"
    assert body["opponent_id"] == world["team_b"]
    assert body["opponent_name"] == "Head To Head B"

    assert body["matches_played"] == 3
    assert body["team_wins"] == 2
    assert body["opponent_wins"] == 1
    assert body["draws"] == 0
    assert body["team_win_percentage"] == 66.67

    assert body["team_highest_score"] == 150
    assert body["team_lowest_score"] == 100
    assert body["opponent_highest_score"] == 140
    assert body["opponent_lowest_score"] == 101


def test_recent_results_most_recent_first(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"])
    # 03-30 loss, 03-20 win, 03-10 win.
    assert body["recent_form_string"] == "L-W-W"
    assert [r["result_code"] for r in body["recent_results"]] == ["L", "W", "W"]

    latest = body["recent_results"][0]
    assert latest["match_id"] == world["match_3"]
    assert latest["result"] == "loss"
    assert latest["team_score"] == 100
    assert latest["opponent_score"] == 101
    assert latest["venue"] == "Rivalry Ground"
    assert latest["player_of_match_id"] == world["pom_player"]
    assert latest["player_of_match_name"] == "HBBat Player0"


def test_recent_limit(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"], params={"recent": 2})
    assert body["recent_form_string"] == "L-W"
    assert len(body["recent_results"]) == 2


def test_perspective_swaps_when_other_team_is_first(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_b"], world["team_a"])
    # B sees the same three games the other way round.
    assert body["team_wins"] == 1
    assert body["opponent_wins"] == 2
    assert body["recent_form_string"] == "W-L-L"
    assert body["team_highest_score"] == 140
    assert body["team_lowest_score"] == 101


def test_top_batting_performances(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"])
    assert body["top_batting"], "expected at least one batting performance"

    runs = [p["runs"] for p in body["top_batting"]]
    assert runs == sorted(runs, reverse=True)

    top = body["top_batting"][0]
    # A's opener faces overs 1 and 3 of the 150, so 13 sixes = 78. The 200 in the
    # A-vs-C match is deliberately excluded but would have beaten it.
    assert top["player_id"] == world["top_player"]
    assert top["team_id"] == world["team_a"]
    assert top["match_id"] == world["match_1"]
    assert top["runs"] == 78
    assert top["fours"] == 0
    assert top["sixes"] == 13


def test_top_bowling_performances(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"])
    assert body["top_bowling"], "expected at least one bowling performance"

    wickets = [p["wickets"] for p in body["top_bowling"]]
    assert wickets == sorted(wickets, reverse=True)

    top = body["top_bowling"][0]
    # A's bowler took all ten in meeting 1; the 10/50 in the A-vs-C game is out.
    assert top["player_id"] == world["top_player"]
    assert top["team_id"] == world["team_a"]
    assert top["match_id"] == world["match_1"]
    assert top["wickets"] == 10
    assert top["runs_conceded"] == 120


def test_top_limit(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_a"], world["team_b"], params={"top": 1})
    assert len(body["top_batting"]) == 1
    assert len(body["top_bowling"]) == 1


def test_unplayed_pair_is_empty(client, as_admin, world):
    body = _h2h(client, as_admin, world, world["team_b"], world["team_c"])
    assert body["matches_played"] == 0
    assert body["team_wins"] == 0
    assert body["opponent_wins"] == 0
    assert body["team_win_percentage"] == 0.0
    assert body["team_highest_score"] is None
    assert body["opponent_highest_score"] is None
    assert body["recent_results"] == []
    assert body["recent_form_string"] == ""
    assert body["top_batting"] == []
    assert body["top_bowling"] == []


def test_same_team_is_400(client, as_admin, world):
    as_admin(world["admin"])
    resp = client.get(f"/v1/teams/{world['team_a']}/head-to-head/{world['team_a']}")
    assert resp.status_code == 400


def test_unknown_teams_are_404(client, as_admin, world):
    as_admin(world["admin"])
    assert client.get(f"/v1/teams/99999999/head-to-head/{world['team_b']}").status_code == 404
    assert client.get(f"/v1/teams/{world['team_a']}/head-to-head/99999999").status_code == 404
