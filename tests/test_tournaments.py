"""End-to-end tests for the tournament/league endpoints.

Seeds completed matches straight into the database (batting order + deliveries,
then ``set_match_completed``), then drives the real routes. The world tournament
has four teams and a generated round-robin, with three of its fixtures linked to
completed matches and one to a live match, so the points table, net run rate and
leaderboards have something to compute from while the unlinked fixtures prove a
plan is not a result.

Every all-out innings is seeded that way on purpose: a side bowled out is
credited its full overs for net run rate, which makes the expected NRR a clean
number instead of a function of how many balls the test happened to bowl.
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
from app.tournaments.crud import generate_fixtures
from app.tournaments.models import Tournament, TournamentFixture, TournamentTeam

TEAM_NAMES = ["Team A", "Team B", "Team C", "Team D"]


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
    db, match, innings_number, batting_order, bowler_id, total, wickets, target=None
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
    for d in _gen_deliveries(batting_order, bowler_id, total, wickets):
        db.add(Delivery(innings_id=innings.id, **d))
    return innings


def _new_match(db, admin, team_a, team_b, match_date, status="live"):
    match = Match(
        match_date=match_date,
        match_time="18:00",
        venue="Tournament Ground",
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
                await db.execute(select(User).where(User.email == "tournaments@test.com"))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email="tournaments@test.com",
                    hashed_password=hash_password("Test@1234"),
                    full_name="Tournament Admin",
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
                    homeground="Tournament Ground",
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

            teams = [new_team(name, f"T{i}") for i, name in enumerate(TEAM_NAMES)]
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
                        email=f"tournaments_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            squads = [make_squad(chr(65 + i)) for i in range(4)]
            await db.flush()
            squad_ids = [[p.id for p in squad] for squad in squads]
            for squad, team in zip(squads, teams, strict=True):
                for player in squad:
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
            await db.flush()

            team_a, team_b, team_c, team_d = teams
            sa, sb, sc, sd = squad_ids

            # M1: A 200 all out; B chases to 100 all out. A wins by 100 runs.
            m1 = _new_match(db, admin, team_a, team_b, date(2026, 3, 1))
            await db.flush()
            await _seed_innings(db, m1, 1, sa, sb[0], 200, 10)
            await _seed_innings(db, m1, 2, sb, sa[0], 100, 10, target=201)
            await set_match_completed(db, m1)

            # M2: C 150 all out; A chases to 100 all out. C wins by 50 runs.
            m2 = _new_match(db, admin, team_c, team_a, date(2026, 3, 2))
            await db.flush()
            await _seed_innings(db, m2, 1, sc, sa[0], 150, 10)
            await _seed_innings(db, m2, 2, sa, sc[0], 100, 10, target=151)
            await set_match_completed(db, m2)

            # M3: B 180 all out; C chases to 120 all out. B wins by 60 runs.
            m3 = _new_match(db, admin, team_b, team_c, date(2026, 3, 3))
            await db.flush()
            await _seed_innings(db, m3, 1, sb, sc[0], 180, 10)
            await _seed_innings(db, m3, 2, sc, sb[0], 120, 10, target=181)
            await set_match_completed(db, m3)

            # A live match (only one innings) that must not count anywhere.
            m_live = _new_match(db, admin, team_a, team_d, date(2026, 3, 4), status="live")
            await db.flush()
            await _seed_innings(db, m_live, 1, sa, sd[0], 60, 0)

            tournament = Tournament(
                name="Tournaments Cup",
                description="A test competition",
                format="league_knockout",
                status="ongoing",
                start_date=date(2026, 3, 1),
                user_id=admin.id,
            )
            db.add(tournament)
            await db.flush()
            for team in teams:
                db.add(TournamentTeam(tournament_id=tournament.id, team_id=team.id))
            await db.flush()

            _count, error = await generate_fixtures(db, tournament, legs=1, replace=False)
            assert error is None, error

            fixtures = (
                (
                    await db.execute(
                        select(TournamentFixture).where(
                            TournamentFixture.tournament_id == tournament.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_pair = {frozenset((f.team_a_id, f.team_b_id)): f for f in fixtures}
            by_pair[frozenset((team_a.id, team_b.id))].match_id = m1.id
            by_pair[frozenset((team_a.id, team_c.id))].match_id = m2.id
            by_pair[frozenset((team_b.id, team_c.id))].match_id = m3.id
            by_pair[frozenset((team_a.id, team_d.id))].match_id = m_live.id
            await db.commit()

            return {
                "admin": admin,
                "tournament": tournament.id,
                "teams": {name: team.id for name, team in zip(TEAM_NAMES, teams, strict=True)},
                "team_ids": [team.id for team in teams],
                "squads": squad_ids,
            }

    return asyncio.run(setup())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def _new_tournament(client, as_admin, world, name, **kwargs):
    _use_admin(as_admin, world)
    resp = client.post("/v1/tournaments", json={"name": name, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_team(client, tournament_id, team_id, expected=201):
    resp = client.post(f"/v1/tournaments/{tournament_id}/teams", json={"team_id": team_id})
    assert resp.status_code == expected, resp.text
    return resp


# --- tournament CRUD ---------------------------------------------------------


def test_create_and_get_tournament(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "CRUD Cup", description="hello")
    assert body["name"] == "CRUD Cup"
    assert body["team_count"] == 0
    assert body["fixture_count"] == 0
    assert body["points_win"] == 2

    resp = client.get(f"/v1/tournaments/{body['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "hello"


def test_duplicate_tournament_name_is_409(client, as_admin, world):
    _new_tournament(client, as_admin, world, "Unique Cup")
    resp = client.post("/v1/tournaments", json={"name": "Unique Cup"})
    assert resp.status_code == 409, resp.text


def test_list_tournaments_is_paginated(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get("/v1/tournaments", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert int(resp.headers["X-Total-Count"]) >= 1
    assert resp.headers["X-Has-More"] in {"true", "false"}


def test_update_and_delete_tournament(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Temp Cup")
    tid = body["id"]

    resp = client.patch(f"/v1/tournaments/{tid}", json={"status": "completed", "points_win": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    assert resp.json()["points_win"] == 3

    assert client.delete(f"/v1/tournaments/{tid}").status_code == 204
    assert client.get(f"/v1/tournaments/{tid}").status_code == 404


def test_unknown_tournament_is_404(client, as_admin, world):
    _use_admin(as_admin, world)
    assert client.get("/v1/tournaments/99999999").status_code == 404


# --- membership --------------------------------------------------------------


def test_add_list_and_reject_team(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Membership Cup")
    tid = body["id"]
    team_a = world["teams"]["Team A"]

    added = _add_team(client, tid, team_a)
    assert added.json()["team_name"] == "Team A"

    _add_team(client, tid, team_a, expected=400)  # already entered
    _add_team(client, tid, 99999999, expected=400)  # not this account's team

    resp = client.get(f"/v1/tournaments/{tid}/teams")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tournament_id"] == tid
    assert [t["team_id"] for t in resp.json()["teams"]] == [team_a]

    assert client.delete(f"/v1/tournaments/{tid}/teams/{team_a}").status_code == 204
    assert client.get(f"/v1/tournaments/{tid}/teams").json()["teams"] == []


# --- fixtures ----------------------------------------------------------------


def test_world_fixtures_are_generated(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get(f"/v1/tournaments/{world['tournament']}/fixtures")
    assert resp.status_code == 200, resp.text
    assert int(resp.headers["X-Total-Count"]) == 6
    assert len(resp.json()) == 6


def test_fixture_statuses_reflect_linked_matches(client, as_admin, world):
    _use_admin(as_admin, world)
    rows = client.get(f"/v1/tournaments/{world['tournament']}/fixtures").json()
    by_pair = {frozenset((r["team_a_id"], r["team_b_id"])): r for r in rows}
    teams = world["teams"]

    ab = by_pair[frozenset((teams["Team A"], teams["Team B"]))]
    assert ab["status"] == "completed"
    assert ab["winner_team_id"] == teams["Team A"]

    ac = by_pair[frozenset((teams["Team A"], teams["Team C"]))]
    assert ac["winner_team_id"] == teams["Team C"]

    bc = by_pair[frozenset((teams["Team B"], teams["Team C"]))]
    assert bc["winner_team_id"] == teams["Team B"]

    ad = by_pair[frozenset((teams["Team A"], teams["Team D"]))]
    assert ad["status"] == "live"
    assert ad["winner_team_id"] is None

    cd = by_pair[frozenset((teams["Team C"], teams["Team D"]))]
    assert cd["status"] == "scheduled"
    assert cd["match_id"] is None


def test_generate_fixtures_endpoint_and_replace_guard(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Generator Cup")
    tid = body["id"]
    for tid_team in world["team_ids"][:3]:
        _add_team(client, tid, tid_team)

    resp = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3  # 3 teams -> 3 matches

    again = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})
    assert again.status_code == 400, again.text

    unconfirmed = client.post(
        f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 2, "replace": True}
    )
    assert unconfirmed.status_code == 400, unconfirmed.text

    replaced = client.post(
        f"/v1/tournaments/{tid}/fixtures/generate",
        json={"legs": 2, "replace": True, "confirm": True},
    )
    assert replaced.status_code == 200, replaced.text
    assert len(replaced.json()) == 6

    not_enough = _new_tournament(client, as_admin, world, "Empty Generator Cup")
    _add_team(client, not_enough["id"], world["team_ids"][0])
    resp = client.post(f"/v1/tournaments/{not_enough['id']}/fixtures/generate", json={"legs": 1})
    assert resp.status_code == 400, resp.text


def test_manual_fixture_create_update_delete(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Manual Cup")
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)

    # A team not entered is refused.
    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": world["team_ids"][2]},
    )
    assert resp.status_code == 400, resp.text

    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b, "venue": "Neutral Ground"},
    )
    assert resp.status_code == 201, resp.text
    fixture_id = resp.json()["id"]
    assert resp.json()["venue"] == "Neutral Ground"
    assert resp.json()["stage"] == "league"

    resp = client.patch(
        f"/v1/tournaments/{tid}/fixtures/{fixture_id}",
        json={"venue": "Final Ground", "stage": "final"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["venue"] == "Final Ground"
    assert resp.json()["stage"] == "final"

    assert client.delete(f"/v1/tournaments/{tid}/fixtures/{fixture_id}").status_code == 204
    assert client.delete(f"/v1/tournaments/{tid}/fixtures/{fixture_id}").status_code == 404


def test_removing_a_team_with_fixtures_is_refused(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Locked Roster Cup")
    tid = body["id"]
    for team_id in world["team_ids"][:2]:
        _add_team(client, tid, team_id)
    client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})

    resp = client.delete(f"/v1/tournaments/{tid}/teams/{world['team_ids'][0]}")
    assert resp.status_code == 400, resp.text


# --- results -----------------------------------------------------------------


def test_link_match_and_reject_mismatch(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Result Cup")
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)
    fixture = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b},
    ).json()

    # A match between two entirely different teams does not belong on this fixture.
    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result",
        json={"match_id": 99999999},
    )
    assert resp.status_code == 400, resp.text

    resp = client.get(f"/v1/tournaments/{world['tournament']}/fixtures").json()
    ab = next(
        r
        for r in resp
        if frozenset((r["team_a_id"], r["team_b_id"])) == frozenset((team_a, team_b))
        and r["match_id"] is not None
    )
    # A match already linked to another fixture cannot be linked again: that
    # would count the same result twice.
    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result",
        json={"match_id": ab["match_id"]},
    )
    assert resp.status_code == 400, resp.text

    # Only completed matches can be linked for recovery.
    async def seed_scheduled_match():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == "tournaments@test.com"))
            ).scalar_one()
            tc = (
                await db.execute(select(Team).where(Team.id == world["team_ids"][2]))
            ).scalar_one()
            td = (
                await db.execute(select(Team).where(Team.id == world["team_ids"][3]))
            ).scalar_one()
            match = _new_match(db, admin, tc, td, date(2026, 3, 5), status="scheduled")
            await db.commit()
            return match.id

    match_id = asyncio.run(seed_scheduled_match())
    _add_team(client, tid, world["team_ids"][2])
    _add_team(client, tid, world["team_ids"][3])
    cd = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": world["team_ids"][2], "team_b_id": world["team_ids"][3]},
    ).json()
    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures/{cd['id']}/result",
        json={"match_id": match_id},
    )
    assert resp.status_code == 400, resp.text
    assert "completed" in resp.json()["detail"].lower()


# --- points table ------------------------------------------------------------


def test_points_table_positions_and_net_run_rate(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get(f"/v1/tournaments/{world['tournament']}/points-table")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tournament_name"] == "Tournaments Cup"

    table = body["table"]
    order = [row["team_name"] for row in table]
    # All three played teams are on 2 points; NRR breaks the tie: A +1.25,
    # C -0.25, B -1.0. D has not played.
    assert order == ["Team A", "Team C", "Team B", "Team D"]
    assert [row["position"] for row in table] == [1, 2, 3, 4]

    by_name = {row["team_name"]: row for row in table}
    team_a = by_name["Team A"]
    assert team_a["played"] == 2
    assert team_a["won"] == 1
    assert team_a["lost"] == 1
    assert team_a["tied"] == 0
    assert team_a["points"] == 2
    assert team_a["runs_for"] == 300  # 200 + 100
    assert team_a["runs_against"] == 250  # 100 + 150
    assert team_a["net_run_rate"] == 1.25
    assert team_a["form"] == "L-W"  # M2 (latest) loss, then M1 win

    team_d = by_name["Team D"]
    assert team_d["played"] == 0
    assert team_d["points"] == 0
    assert team_d["net_run_rate"] == 0.0
    assert team_d["form"] == ""


def test_points_table_counts_no_result_when_chase_never_finishes(client, as_admin, world):
    """A completed match whose second innings never finished is a no-result for
    both sides, not a tie."""
    _use_admin(as_admin, world)
    body = _new_tournament(client, as_admin, world, "No Result Cup")
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)
    fixture = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b},
    ).json()

    async def seed_one_innings_match():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == "tournaments@test.com"))
            ).scalar_one()
            ta = (await db.execute(select(Team).where(Team.id == team_a))).scalar_one()
            tb = (await db.execute(select(Team).where(Team.id == team_b))).scalar_one()
            match = _new_match(db, admin, ta, tb, date(2026, 4, 1), status="live")
            await db.flush()
            await _seed_innings(db, match, 1, world["squads"][0], world["squads"][1][0], 120, 10)
            await set_match_completed(db, match)
            return match.id

    match_id = asyncio.run(seed_one_innings_match())
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result", json={"match_id": match_id}
    )
    assert linked.status_code == 200, linked.text

    table = client.get(f"/v1/tournaments/{tid}/points-table").json()["table"]
    assert len(table) == 2
    for row in table:
        assert row["played"] == 1
        assert row["no_result"] == 1
        assert row["won"] == 0
        assert row["lost"] == 0
        assert row["tied"] == 0
        assert row["points"] == 1  # the default points_no_result
        assert row["form"] == "N"


# --- leaderboards ------------------------------------------------------------


def test_leaderboards_aggregate_the_whole_tournament(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.get(f"/v1/tournaments/{world['tournament']}/leaderboards", params={"top": 50})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 200 + 100 + 150 + 100 + 180 + 120, every ball a six.
    assert sum(p["runs"] for p in body["top_run_scorers"]) == 850
    # Ten wickets in each of the six innings.
    assert sum(p["wickets"] for p in body["top_wicket_takers"]) == 60

    runs = [p["runs"] for p in body["top_run_scorers"]]
    assert runs == sorted(runs, reverse=True)
    wickets = [p["wickets"] for p in body["top_wicket_takers"]]
    assert wickets == sorted(wickets, reverse=True)

    # Every entry carries its side's name and a tournament match count.
    assert all(p["team_name"] in TEAM_NAMES for p in body["top_run_scorers"])
    assert all(p["matches"] >= 1 for p in body["top_run_scorers"])
    assert body["matches_considered"] == 3
    ids = [p["player_id"] for p in body["top_run_scorers"]]
    assert len(ids) == len(set(ids))


# --- knockout ----------------------------------------------------------------


def test_knockout_draw_seeds_from_the_points_table(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post(
        f"/v1/tournaments/{world['tournament']}/fixtures/knockout", json={"teams": 4}
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 3
    assert {r["stage"] for r in rows} == {"semifinal", "final"}

    semis = [r for r in rows if r["stage"] == "semifinal"]
    final = next(r for r in rows if r["stage"] == "final")
    assert len(semis) == 2
    assert final["team_a_id"] is None and final["team_b_id"] is None
    assert final["is_tbd"] is True

    # Seeds 1v4 and 2v3, from the table order A, C, B, D.
    seeds = [
        world["teams"]["Team A"],
        world["teams"]["Team C"],
        world["teams"]["Team B"],
        world["teams"]["Team D"],
    ]
    pairs = {frozenset((r["team_a_id"], r["team_b_id"])) for r in semis}
    assert pairs == {frozenset((seeds[0], seeds[3])), frozenset((seeds[1], seeds[2]))}

    # Both semi-finals feed the final, one into each slot.
    assert {r["next_fixture_id"] for r in semis} == {final["id"]}
    assert {r["next_slot"] for r in semis} == {"a", "b"}
    assert final["next_fixture_id"] is None

    # Regenerating an unlinked draw replaces it with the same bracket.
    again = client.post(
        f"/v1/tournaments/{world['tournament']}/fixtures/knockout", json={"teams": 4}
    )
    assert again.status_code == 200, again.text
    assert len(again.json()) == 3


def test_knockout_needs_enough_teams(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Tiny Draw Cup")
    tid = body["id"]
    _add_team(client, tid, world["team_ids"][0])
    resp = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4})
    assert resp.status_code == 400, resp.text


def _squad(world, team_id):
    return world["squads"][world["team_ids"].index(team_id)]


def _seed_completed_match(admin_id, team_a_id, team_b_id, squad_a, squad_b, score_a, score_b, when):
    """Seed a two-innings completed match and return its id.

    Equal scores with the chase all out is a tie (the second innings is complete
    but neither side is ahead), which is the case a bracket must not advance.
    """

    async def run():
        async with async_session() as db:
            admin = await db.get(User, admin_id)
            team_a = await db.get(Team, team_a_id)
            team_b = await db.get(Team, team_b_id)
            match = _new_match(db, admin, team_a, team_b, when)
            await db.flush()
            await _seed_innings(db, match, 1, squad_a, squad_b[0], score_a, 10)
            await _seed_innings(db, match, 2, squad_b, squad_a[0], score_b, 10, target=score_a + 1)
            await set_match_completed(db, match)
            return match.id

    return asyncio.run(run())


def _create_extra_teams(admin_id, base_team_id, count, prefix):
    async def run():
        async with async_session() as db:
            base = await db.get(Team, base_team_id)
            created = []
            for index in range(count):
                team = Team(
                    name=f"{prefix} {index + 1}",
                    short_name=f"{prefix[:2]}{index + 1}",
                    homeground="Extra Ground",
                    founder="Founder",
                    founded_year=2020,
                    owner="Owner",
                    country_id=base.country_id,
                    state_id=base.state_id,
                    city_id=base.city_id,
                    level_id=base.level_id,
                    user_id=admin_id,
                )
                db.add(team)
                created.append(team)
            await db.commit()
            return [team.id for team in created]

    return asyncio.run(run())


def test_knockout_advances_only_a_decisive_winner(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Bracket Cup", format="knockout")
    tid = body["id"]
    for team_id in world["team_ids"]:
        _add_team(client, tid, team_id)

    resp = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4})
    assert resp.status_code == 200, resp.text
    bracket = resp.json()
    semis = sorted(
        (row for row in bracket if row["stage"] == "semifinal"),
        key=lambda row: row["next_slot"],
    )
    final = next(row for row in bracket if row["stage"] == "final")
    assert len(semis) == 2
    assert final["team_a_id"] is None and final["team_b_id"] is None

    # A decisive semi-final fills exactly the slot it feeds.
    semi_a = semis[0]
    winner_a = semi_a["team_a_id"]
    match_a = _seed_completed_match(
        world["admin"].id,
        semi_a["team_a_id"],
        semi_a["team_b_id"],
        _squad(world, semi_a["team_a_id"]),
        _squad(world, semi_a["team_b_id"]),
        200,
        100,
        date(2026, 3, 10),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{semi_a['id']}/result", json={"match_id": match_a}
    )
    assert linked.status_code == 200, linked.text

    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    final = next(row for row in rows if row["id"] == final["id"])
    slot = "team_a_id" if semi_a["next_slot"] == "a" else "team_b_id"
    other = "team_b_id" if slot == "team_a_id" else "team_a_id"
    assert final[slot] == winner_a
    assert final[other] is None

    # A tie is not a win: the winner of the other semi-final cannot advance.
    semi_b = semis[1]
    tied = _seed_completed_match(
        world["admin"].id,
        semi_b["team_a_id"],
        semi_b["team_b_id"],
        _squad(world, semi_b["team_a_id"]),
        _squad(world, semi_b["team_b_id"]),
        150,
        150,
        date(2026, 3, 11),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{semi_b['id']}/result", json={"match_id": tied}
    )
    assert linked.status_code == 200, linked.text
    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    final = next(row for row in rows if row["id"] == final["id"])
    b_slot = "team_a_id" if semi_b["next_slot"] == "a" else "team_b_id"
    assert final[b_slot] is None
    assert final["is_tbd"] is True

    # Replacing the tie with a decisive result does advance that one slot.
    winner_b = semi_b["team_b_id"]
    decisive = _seed_completed_match(
        world["admin"].id,
        semi_b["team_a_id"],
        semi_b["team_b_id"],
        _squad(world, semi_b["team_a_id"]),
        _squad(world, semi_b["team_b_id"]),
        120,
        180,
        date(2026, 3, 12),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{semi_b['id']}/result", json={"match_id": decisive}
    )
    assert linked.status_code == 200, linked.text
    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    final = next(row for row in rows if row["id"] == final["id"])
    assert final[b_slot] == winner_b
    assert final["team_a_id"] != final["team_b_id"]

    # Once the final has a result, changing an upstream winner is refused.
    final_match = _seed_completed_match(
        world["admin"].id,
        final["team_a_id"],
        final["team_b_id"],
        _squad(world, final["team_a_id"]),
        _squad(world, final["team_b_id"]),
        160,
        140,
        date(2026, 3, 13),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{final['id']}/result", json={"match_id": final_match}
    )
    assert linked.status_code == 200, linked.text

    other_winner = _seed_completed_match(
        world["admin"].id,
        semi_a["team_a_id"],
        semi_a["team_b_id"],
        _squad(world, semi_a["team_a_id"]),
        _squad(world, semi_a["team_b_id"]),
        90,
        210,
        date(2026, 3, 14),
    )
    blocked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{semi_a['id']}/result",
        json={"match_id": other_winner},
    )
    assert blocked.status_code == 400, blocked.text
    assert "already has a linked result" in blocked.json()["detail"]

    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    final = next(row for row in rows if row["id"] == final["id"])
    assert final[slot] == winner_a
    assert final["match_id"] == final_match


def test_legacy_match_type_still_serialises_in_the_match_list(client, as_admin, world):
    """A stored format this build no longer offers must not break the list.

    Existing databases can hold values from an older build (``limited_overs``
    here). Validating the response against the input enum made every page that
    reads matches fail with a 500, so the response echoes the stored string
    while the request schemas stay closed.
    """

    async def seed():
        async with async_session() as db:
            teams = (
                (
                    await db.execute(
                        select(Team).where(Team.id.in_(world["team_ids"][:2])).order_by(Team.id)
                    )
                )
                .scalars()
                .all()
            )
            match = _new_match(
                db, world["admin"], teams[0], teams[1], date(2026, 3, 20), status="completed"
            )
            match.match_type = "limited_overs"
            db.add(match)
            await db.commit()
            await db.refresh(match)
            return match.id

    match_id = asyncio.run(seed())
    _use_admin(as_admin, world)

    listed = client.get("/v1/matches?skip=0&limit=500")
    assert listed.status_code == 200, listed.text
    legacy = next(m for m in listed.json() if m["id"] == match_id)
    assert legacy["match_type"] == "limited_overs"

    single = client.get(f"/v1/matches/{match_id}")
    assert single.status_code == 200, single.text
    assert single.json()["match_type"] == "limited_overs"


def test_unlink_result_keeps_match_and_clears_the_next_round(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Unlink Cup", format="knockout")
    tid = body["id"]
    for team_id in world["team_ids"]:
        _add_team(client, tid, team_id)
    bracket = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4}).json()
    semi = next(row for row in bracket if row["stage"] == "semifinal")
    final = next(row for row in bracket if row["stage"] == "final")

    match_id = _seed_completed_match(
        world["admin"].id,
        semi["team_a_id"],
        semi["team_b_id"],
        _squad(world, semi["team_a_id"]),
        _squad(world, semi["team_b_id"]),
        200,
        100,
        date(2026, 3, 17),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{semi['id']}/result", json={"match_id": match_id}
    )
    assert linked.status_code == 200, linked.text
    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    final = next(row for row in rows if row["id"] == final["id"])
    slot = "team_a_id" if semi["next_slot"] == "a" else "team_b_id"
    assert final[slot] == semi["team_a_id"]

    unlinked = client.delete(f"/v1/tournaments/{tid}/fixtures/{semi['id']}/result")
    assert unlinked.status_code == 204, unlinked.text
    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    semi = next(row for row in rows if row["id"] == semi["id"])
    final = next(row for row in rows if row["id"] == final["id"])
    assert semi["match_id"] is None
    assert final[slot] is None

    # The match and its scorecard still exist; only the link is gone.
    async def match_exists():
        async with async_session() as db:
            match = await db.get(Match, match_id)
            innings = (
                (await db.execute(select(Innings).where(Innings.match_id == match_id)))
                .scalars()
                .all()
            )
            return match is not None, len(innings)

    exists, innings_count = asyncio.run(match_exists())
    assert exists is True
    assert innings_count == 2


def test_two_and_eight_team_brackets_have_the_right_shape(client, as_admin, world):
    two = _new_tournament(client, as_admin, world, "Two Team Final Cup", format="knockout")
    for team_id in world["team_ids"][:2]:
        _add_team(client, two["id"], team_id)
    resp = client.post(f"/v1/tournaments/{two['id']}/fixtures/knockout", json={"teams": 2})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    final = resp.json()[0]
    assert final["stage"] == "final"
    assert final["team_a_id"] is not None and final["team_b_id"] is not None
    assert final["team_a_id"] != final["team_b_id"]
    assert final["next_fixture_id"] is None
    assert final["is_tbd"] is False

    extra_ids = _create_extra_teams(world["admin"].id, world["team_ids"][0], 4, "Bracket")
    eight = _new_tournament(client, as_admin, world, "Eight Team Cup", format="knockout")
    for team_id in [*world["team_ids"], *extra_ids]:
        _add_team(client, eight["id"], team_id)
    resp = client.post(f"/v1/tournaments/{eight['id']}/fixtures/knockout", json={"teams": 8})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    quarters = [row for row in rows if row["stage"] == "quarterfinal"]
    semis = [row for row in rows if row["stage"] == "semifinal"]
    finals = [row for row in rows if row["stage"] == "final"]
    assert [len(quarters), len(semis), len(finals)] == [4, 2, 1]
    assert len({row["id"] for row in rows}) == 7

    # Every QF side is a distinct entered team, and every later round is TBD.
    qf_teams = [team_id for row in quarters for team_id in (row["team_a_id"], row["team_b_id"])]
    assert len(qf_teams) == 8
    assert len(set(qf_teams)) == 8
    assert all(row["is_tbd"] for row in semis + finals)
    assert {row["next_fixture_id"] for row in quarters} == {row["id"] for row in semis}
    assert {row["next_fixture_id"] for row in semis} == {finals[0]["id"]}
    assert {row["next_slot"] for row in quarters} == {"a", "b"}


def test_knockout_points_table_is_not_applicable_and_summary_is_zero(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Knockout Summary Cup", format="knockout")
    tid = body["id"]
    for team_id in world["team_ids"]:
        _add_team(client, tid, team_id)
    generated = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4})
    assert generated.status_code == 200, generated.text

    table = client.get(f"/v1/tournaments/{tid}/points-table")
    assert table.status_code == 200, table.text
    assert table.json()["table"] == []
    assert table.json()["applicable"] is False
    assert table.json()["message"] == "Not applicable for knockout format."

    summary = client.get(f"/v1/tournaments/{tid}/summary")
    assert summary.status_code == 200, summary.text
    data = summary.json()
    assert data["matches_played"] == 0
    assert data["wins"] == data["losses"] == data["ties"] == data["no_results"] == 0
    assert data["points_table_applicable"] is False
    assert data["fixture_count"] == 3
    assert data["team_count"] == 4

    # Round-robin and knockout generators are format-gated.
    rr = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})
    assert rr.status_code == 400, rr.text

    league = _new_tournament(client, as_admin, world, "League Only Cup", format="league")
    for team_id in world["team_ids"]:
        _add_team(client, league["id"], team_id)
    ko = client.post(f"/v1/tournaments/{league['id']}/fixtures/knockout", json={"teams": 4})
    assert ko.status_code == 400, ko.text


def test_summary_counts_team_results_once(client, as_admin, world):
    _use_admin(as_admin, world)
    data = client.get(f"/v1/tournaments/{world['tournament']}/summary").json()
    assert data["matches_played"] == 3
    assert data["wins"] == 3
    assert data["losses"] == 3
    assert data["ties"] == 0
    assert data["no_results"] == 0
    assert (
        data["wins"] + data["losses"] + data["ties"] + data["no_results"]
        == 2 * data["matches_played"]
    )
    assert data["points_table_applicable"] is True
    assert data["message"] is None


def test_fixture_schedule_validation_warnings_and_unscheduled(client, as_admin, world):
    body = _new_tournament(
        client,
        as_admin,
        world,
        "Window Cup",
        start_date="2026-10-23",
        end_date="2026-10-24",
    )
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)

    before = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b, "scheduled_date": "2026-10-22"},
    )
    assert before.status_code == 400, before.text
    after = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b, "scheduled_date": "2026-10-25"},
    )
    assert after.status_code == 400, after.text
    time_only = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b, "scheduled_time": "18:00"},
    )
    assert time_only.status_code == 400, time_only.text
    bad_time = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={
            "team_a_id": team_a,
            "team_b_id": team_b,
            "scheduled_date": "2026-10-23",
            "scheduled_time": "25:00",
        },
    )
    assert bad_time.status_code == 400, bad_time.text

    # Unscheduled is allowed and nothing is invented for it.
    unscheduled = client.post(
        f"/v1/tournaments/{tid}/fixtures", json={"team_a_id": team_a, "team_b_id": team_b}
    )
    assert unscheduled.status_code == 201, unscheduled.text
    assert unscheduled.json()["scheduled_date"] is None
    assert unscheduled.json()["scheduled_time"] is None
    assert unscheduled.json()["venue"] is None
    assert unscheduled.json()["status"] == "scheduled"

    first = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={
            "team_a_id": team_a,
            "team_b_id": team_b,
            "scheduled_date": "2026-10-23",
            "scheduled_time": "18:00",
            "venue": "Window Ground",
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/v1/tournaments/{tid}/fixtures",
        json={
            "team_a_id": team_a,
            "team_b_id": team_b,
            "scheduled_date": "2026-10-23",
            "scheduled_time": "18:00",
            "venue": "Window Ground",
        },
    )
    assert second.status_code == 201, second.text
    assert first.json()["warnings"] == []

    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    scheduled = [row for row in rows if row["scheduled_time"] == "18:00"]
    assert len(scheduled) == 2
    for row in scheduled:
        assert any("Team clash" in warning for warning in row["warnings"])
        assert any("Venue clash" in warning for warning in row["warnings"])

    # Clearing a date while keeping a time is refused rather than stored.
    cleared = client.patch(
        f"/v1/tournaments/{tid}/fixtures/{first.json()['id']}",
        json={"scheduled_date": None},
    )
    assert cleared.status_code == 400, cleared.text

    # A date outside the window is refused on edit too.
    moved = client.patch(
        f"/v1/tournaments/{tid}/fixtures/{first.json()['id']}",
        json={"scheduled_date": "2026-10-30"},
    )
    assert moved.status_code == 400, moved.text


def test_team_removal_needs_confirmation_and_keeps_players(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Roster Cup")
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)
    generated = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})
    assert generated.status_code == 200 and len(generated.json()) == 1

    async def snapshot():
        async with async_session() as db:
            team = await db.get(Team, team_a)
            players = (
                (
                    await db.execute(
                        select(Player)
                        .join(PlayerTeamAssignment, PlayerTeamAssignment.player_id == Player.id)
                        .where(PlayerTeamAssignment.team_id == team_a)
                    )
                )
                .scalars()
                .all()
            )
            return team is not None, len(players)

    before = asyncio.run(snapshot())
    refused = client.delete(f"/v1/tournaments/{tid}/teams/{team_a}")
    assert refused.status_code == 400, refused.text
    assert "Confirm removal" in refused.json()["detail"]

    removed = client.delete(f"/v1/tournaments/{tid}/teams/{team_a}", params={"confirm": "true"})
    assert removed.status_code == 204, removed.text
    after = asyncio.run(snapshot())
    assert after == before  # the team and its players are untouched
    assert [t["team_id"] for t in client.get(f"/v1/tournaments/{tid}/teams").json()["teams"]] == [
        team_b
    ]
    assert client.get(f"/v1/tournaments/{tid}/fixtures").json() == []

    # A team with a linked result cannot be removed even with confirmation.
    played = _new_tournament(client, as_admin, world, "Played Roster Cup")
    _add_team(client, played["id"], team_a)
    _add_team(client, played["id"], team_b)
    fixture = client.post(
        f"/v1/tournaments/{played['id']}/fixtures",
        json={"team_a_id": team_a, "team_b_id": team_b},
    ).json()
    match_id = _seed_completed_match(
        world["admin"].id,
        team_a,
        team_b,
        _squad(world, team_a),
        _squad(world, team_b),
        140,
        100,
        date(2026, 3, 15),
    )
    linked = client.post(
        f"/v1/tournaments/{played['id']}/fixtures/{fixture['id']}/result",
        json={"match_id": match_id},
    )
    assert linked.status_code == 200, linked.text
    blocked = client.delete(
        f"/v1/tournaments/{played['id']}/teams/{team_a}", params={"confirm": "true"}
    )
    assert blocked.status_code == 400, blocked.text
    assert "linked to a match" in blocked.json()["detail"]


def test_replacement_is_blocked_when_a_fixture_has_a_result(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Replace Guard Cup")
    tid = body["id"]
    team_a, team_b = world["team_ids"][0], world["team_ids"][1]
    _add_team(client, tid, team_a)
    _add_team(client, tid, team_b)
    generated = client.post(f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 1})
    assert generated.status_code == 200 and len(generated.json()) == 1
    fixture = generated.json()[0]

    match_id = _seed_completed_match(
        world["admin"].id,
        team_a,
        team_b,
        _squad(world, team_a),
        _squad(world, team_b),
        200,
        120,
        date(2026, 3, 16),
    )
    linked = client.post(
        f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result", json={"match_id": match_id}
    )
    assert linked.status_code == 200, linked.text

    blocked = client.post(
        f"/v1/tournaments/{tid}/fixtures/generate",
        json={"legs": 1, "replace": True, "confirm": True},
    )
    assert blocked.status_code == 400, blocked.text
    assert "already have a linked match" in blocked.json()["detail"]

    rows = client.get(f"/v1/tournaments/{tid}/fixtures").json()
    assert len(rows) == 1
    assert rows[0]["match_id"] == match_id
    table = client.get(f"/v1/tournaments/{tid}/points-table").json()["table"]
    by_team = {row["team_id"]: row for row in table}
    assert by_team[team_a]["won"] == 1
    assert by_team[team_a]["points"] == 2
