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

    replaced = client.post(
        f"/v1/tournaments/{tid}/fixtures/generate", json={"legs": 2, "replace": True}
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
    resp = client.post(
        f"/v1/tournaments/{tid}/fixtures/{fixture['id']}/result",
        json={"match_id": ab["match_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["match_id"] == ab["match_id"]
    assert resp.json()["winner_team_id"] == team_a


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


# --- knockout ----------------------------------------------------------------


def test_knockout_draw_seeds_from_the_points_table(client, as_admin, world):
    _use_admin(as_admin, world)
    resp = client.post(
        f"/v1/tournaments/{world['tournament']}/fixtures/knockout", json={"teams": 4}
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 2
    assert {r["stage"] for r in rows} == {"semifinal"}

    # Seeds 1v4 and 2v3, from the table order A, C, B, D.
    seeds = [
        world["teams"]["Team A"],
        world["teams"]["Team C"],
        world["teams"]["Team B"],
        world["teams"]["Team D"],
    ]
    pairs = {frozenset((r["team_a_id"], r["team_b_id"])) for r in rows}
    assert pairs == {frozenset((seeds[0], seeds[3])), frozenset((seeds[1], seeds[2]))}

    # Regenerating the same round is refused while it has no results? It is not:
    # an unlinked round is replaced, and the replacement is the same size.
    again = client.post(
        f"/v1/tournaments/{world['tournament']}/fixtures/knockout", json={"teams": 4}
    )
    assert again.status_code == 200, again.text
    assert len(again.json()) == 2


def test_knockout_needs_enough_teams(client, as_admin, world):
    body = _new_tournament(client, as_admin, world, "Tiny Draw Cup")
    tid = body["id"]
    _add_team(client, tid, world["team_ids"][0])
    resp = client.post(f"/v1/tournaments/{tid}/fixtures/knockout", json={"teams": 4})
    assert resp.status_code == 400, resp.text
