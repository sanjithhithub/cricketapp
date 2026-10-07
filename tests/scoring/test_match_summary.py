"""End-to-end tests for the completed-match summary API.

Drives the real ``GET /matches/{match_id}/summary`` route end to end. The
scenario matches are seeded straight into the database (batting order +
deliveries) and completed with ``set_match_completed`` so the official result,
winner and margin come from the same code path the app uses, and the HTTP layer
only exercises the summary route itself.
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

EMAIL = "summary_api@test.com"
TEAM_A = "Alpha CC"
TEAM_B = "Bravo CC"


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


def _gen_deliveries(
    order_ids: list[int], bowler_id: int, total: int, wickets: int
) -> list[dict]:
    """A legal ball-by-ball sequence reaching `total` runs with `wickets` wickets.

    Mirrors the generator used in the engine/result tests: every scoring ball is
    a six (even runs, no crease swap) plus a tail, wickets dismiss the current
    striker, and over boundaries rotate the crease. Replay rebuilds exactly the
    same state from these deliveries.
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
        striker = order_ids[striker_i]
        deliveries.append(
            {
                "over_number": over,
                "ball_number": balls_in_over + 1,
                "striker_id": striker,
                "non_striker_id": order_ids[non_striker_i],
                "bowler_id": bowler_id,
                "runs_batsman": runs,
                "runs_extras": 0,
                "extra_type": ExtraType.NONE.value,
                "wicket_type": WicketType.BOWLED.value if is_wicket else None,
                "dismissed_player_id": striker if is_wicket else None,
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
    is_super_over: bool = False,
) -> Innings:
    innings = Innings(
        match_id=match.id,
        batting_team_id=match.team_a_id if innings_number in (1, 3) else match.team_b_id,
        bowling_team_id=match.team_b_id if innings_number in (1, 3) else match.team_a_id,
        innings_number=innings_number,
        target=target,
        is_super_over=is_super_over,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(batting_order, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    for d in _gen_deliveries(batting_order, bowler_id, total, wickets):
        db.add(Delivery(innings_id=innings.id, **d))
    return innings


async def _seed_scenarios(db, admin, team_a, team_b, squad_a, squad_b):
    match_meta = dict(
        match_date=date(2026, 1, 10),
        match_time="18:00",
        venue="Main Ground",
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        user_id=admin.id,
        super_over_enabled=True,
    )

    matches = {}

    def new_match(**overrides) -> Match:
        match = Match(status="live", current_innings_number=0, **match_meta)
        for key, val in overrides.items():
            setattr(match, key, val)
        db.add(match)
        return match

    # m1: Team A defends 116; Team B all out for 110. A wins by 6 runs.
    m1 = new_match()
    await db.flush()
    await _seed_innings(db, m1, 1, squad_a, squad_b[0], 116, 7)
    await _seed_innings(db, m1, 2, squad_b, squad_a[0], 110, 10, target=117)
    await set_match_completed(db, m1)
    await db.refresh(m1)
    matches["runs"] = m1

    # m2: Team A 120/0, Team B chases 121 by 1 wicket. The top run scorer and the
    # best bowler are both from the losing side (Team A).
    m2 = new_match()
    await db.flush()
    await _seed_innings(db, m2, 1, squad_a, squad_b[0], 120, 0)
    await _seed_innings(db, m2, 2, squad_b, squad_a[0], 121, 9, target=121)
    await set_match_completed(db, m2)
    await db.refresh(m2)
    matches["chase"] = m2

    # m3: level main match, decided by a Super Over (Team A 10, Team B 11).
    m3 = new_match()
    await db.flush()
    await _seed_innings(db, m3, 1, squad_a, squad_b[0], 71, 8)
    await _seed_innings(db, m3, 2, squad_b, squad_a[0], 71, 10, target=72)
    await _seed_innings(db, m3, 3, squad_a, squad_b[0], 10, 0, is_super_over=True)
    await _seed_innings(db, m3, 4, squad_b, squad_a[0], 11, 0, target=11, is_super_over=True)
    await set_match_completed(db, m3)
    await db.refresh(m3)
    matches["super_over"] = m3

    # m4: scheduled, no innings started yet -> summary has no winner/margin.
    m4 = new_match(status="scheduled", current_innings_number=0)
    await db.flush()
    await db.refresh(m4)
    matches["empty"] = m4

    # m5: live, first innings in progress -> no winner/margin yet.
    m5 = new_match(status="live", current_innings_number=1)
    await db.flush()
    await _seed_innings(db, m5, 1, squad_a, squad_b[0], 40, 1)
    await db.refresh(m5)
    matches["live"] = m5

    await db.commit()
    return matches


@pytest.fixture(scope="module")
def world(client):
    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Summary Admin",
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
                    homeground="Test Ground",
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

            team_a = new_team(TEAM_A, "ALP")
            team_b = new_team(TEAM_B, "BRA")
            outsider = new_team("Outsider CC", "OUT")
            await db.flush()

            def make_player(prefix, i):
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
                    mobile_number=9900000000 + int(f"{prefix}{i}", 36),
                    email=f"summary_{prefix}{i}_{admin.id}@test.com",
                )
                db.add(player)
                return player

            squads = {}
            for team, prefix in ((team_a, "A"), (team_b, "B")):
                ids = []
                for i in range(11):
                    player = make_player(prefix, i)
                    await db.flush()
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
                    ids.append(player.id)
                squads[team.id] = ids

            outsider_ids = []
            for i in range(2):
                player = make_player("O", i)
                await db.flush()
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id,
                        team_id=outsider.id,
                        level_id=level.id,
                        role="playing_11",
                    )
                )
                outsider_ids.append(player.id)

            await db.flush()

            # A player not attached to any team (for the PATCH 400 test).
            rogue = make_player("Z", 7)
            await db.flush()

            matches = await _seed_scenarios(
                db, admin, team_a, team_b, squads[team_a.id], squads[team_b.id]
            )

            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "squad_a": squads[team_a.id],
                "squad_b": squads[team_b.id],
                "outsider_player": outsider_ids[0],
                "rogue": rogue.id,
                "matches": matches,
            }

    return asyncio.run(setup())


def _summary(client, as_admin, world, key, expected=200):
    as_admin(world["admin"])
    resp = client.get(f"/v1/matches/{world['matches'][key].id}/summary")
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_summary_defending_team_win_by_runs(client, as_admin, world):
    body = _summary(client, as_admin, world, "runs")
    assert body["status"] == "completed"
    assert body["result_text"] == f"{TEAM_A} won by 6 runs"
    assert body["winner"]["id"] == world["team_a"]
    assert body["margin"] == "6 runs"

    assert body["toss"]["winner"]["id"] == world["team_a"]
    assert body["toss"]["decision"] == "bat"
    assert body["venue"] == "Main Ground"
    assert body["match_date"] == "2026-01-10"
    assert body["match_type"] == "T20"
    assert [t["id"] for t in body["teams"]] == [world["team_a"], world["team_b"]]

    inn = body["innings"]
    assert len(inn) == 2
    assert inn[0]["innings_number"] == 1
    assert inn[0]["team"]["id"] == world["team_a"]
    assert inn[0]["total"] == 116
    assert inn[0]["wickets"] == 7
    assert inn[1]["innings_number"] == 2
    assert inn[1]["total"] == 110
    assert inn[1]["wickets"] == 10
    assert inn[1]["completed"] is True

    assert body["top_run_scorer"] is not None
    assert body["best_bowler"] is not None
    expected_types = {"top_score", "best_bowling"}
    assert expected_types <= {h["type"] for h in body["highlights"]}


def test_summary_chase_win_shows_losing_side_best_efforts(client, as_admin, world):
    """A summary must keep the losing team's top run scorer, best bowler and
    player of the match visible even though their side lost."""
    as_admin(world["admin"])
    match_id = world["matches"]["chase"].id

    # Team A lost (Team B chased 121), but A's opener is the top run scorer with
    # 72 - the losing side's best effort is the whole point of the summary.
    body = _summary(client, as_admin, world, "chase")
    assert body["result_text"] == f"{TEAM_B} won by 1 wicket"
    assert body["winner"]["id"] == world["team_b"]
    assert body["margin"] == "1 wicket"

    top = body["top_run_scorer"]
    assert top["player_id"] == world["squad_a"][0]
    assert top["team_id"] == world["team_a"]
    assert top["runs"] == 72

    best = body["best_bowler"]
    assert best["player_id"] == world["squad_a"][0]
    assert best["team_id"] == world["team_a"]
    assert best["wickets"] == 9

    # The human-chosen player of the match can be from the losing team too.
    resp = client.patch(
        f"/v1/matches/{match_id}",
        json={"player_of_match_id": world["squad_a"][0]},
    )
    assert resp.status_code == 200, resp.text

    body = _summary(client, as_admin, world, "chase")
    assert body["player_of_match"]["player_id"] == world["squad_a"][0]
    assert body["player_of_match"]["team_id"] == world["team_a"]
    assert body["player_of_match"]["team_name"] == TEAM_A


def test_summary_super_over_decides_winner(client, as_admin, world):
    body = _summary(client, as_admin, world, "super_over")
    assert body["result_text"] == f"{TEAM_B} won the Super Over"
    assert body["winner"]["id"] == world["team_b"]
    assert body["margin"] == "1 run"
    assert [i["is_super_over"] for i in body["innings"]] == [False, False, True, True]


def test_summary_before_any_innings_has_no_winner(client, as_admin, world):
    body = _summary(client, as_admin, world, "empty")
    assert body["status"] == "scheduled"
    assert body["innings"] == []
    assert body["winner"] is None
    assert body["margin"] is None
    assert body["player_of_match"] is None
    assert body["top_run_scorer"] is None
    assert body["best_bowler"] is None


def test_summary_live_match_has_no_winner_yet(client, as_admin, world):
    body = _summary(client, as_admin, world, "live")
    assert body["status"] == "live"
    assert len(body["innings"]) == 1
    assert body["innings"][0]["total"] == 40
    assert body["winner"] is None
    assert body["margin"] is None


def test_player_of_match_must_belong_to_one_of_the_two_sides(client, as_admin, world):
    as_admin(world["admin"])
    match_id = world["matches"]["runs"].id

    # A player from a third team is not a valid award.
    resp = client.patch(
        f"/v1/matches/{match_id}",
        json={"player_of_match_id": world["outsider_player"]},
    )
    assert resp.status_code == 400, resp.text
    assert "Player of the match must be a player" in resp.json()["detail"]

    # Nor is a player with no team at all.
    resp = client.patch(
        f"/v1/matches/{match_id}",
        json={"player_of_match_id": world["rogue"]},
    )
    assert resp.status_code == 400, resp.text

    # Clearing the award back to none is allowed.
    resp = client.patch(f"/v1/matches/{match_id}", json={"player_of_match_id": None})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_of_match_id"] is None
