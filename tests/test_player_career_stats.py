"""End-to-end tests for ``GET /players/{player_id}/career-stats``.

Seeds one batter (P0) and one bowler (P2) across two completed matches in two
different seasons, one of them played under a tournament, then drives the real
route. The expected numbers are worked out by hand from the seeded delivery log.
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
from app.tournaments.models import Tournament, TournamentFixture

EMAIL = "career@test.com"


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


async def _seed_innings(db, match, order, bowler, deliveries):
    innings = Innings(
        match_id=match.id,
        batting_team_id=match.team_a_id,
        bowling_team_id=match.team_b_id,
        innings_number=1,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(order, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    for ball, d in enumerate(deliveries, start=1):
        db.add(
            Delivery(
                innings_id=innings.id,
                over_number=1,
                ball_number=ball,
                striker_id=d["striker"],
                non_striker_id=d["non_striker"],
                bowler_id=bowler,
                runs_batsman=d.get("runs", 0),
                runs_extras=0,
                extra_type=ExtraType.NONE.value,
                wicket_type=d.get("wicket"),
                dismissed_player_id=d.get("dismissed"),
                fielder_id=d.get("fielder"),
            )
        )
    return innings


def _new_match(db, admin, team_a, team_b, match_date, venue="Career Ground"):
    match = Match(
        match_date=match_date,
        match_time="10:00",
        venue=venue,
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status="live",
        current_innings_number=1,
        user_id=admin.id,
    )
    db.add(match)
    return match


@pytest.fixture(scope="module")
def world(client):
    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Career Admin",
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
                    homeground="Career Ground",
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

            team_a = new_team("Career A", "CRA")
            team_b = new_team("Career B", "CRB")
            await db.flush()

            def new_player(prefix, i):
                player = Player(
                    first_name=f"{prefix}Career",
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
                    mobile_number=9400000000 + int(f"{prefix}{i}", 36),
                    email=f"career_{prefix}{i}_{admin.id}@test.com",
                    user_id=admin.id,
                )
                db.add(player)
                return player

            p0 = new_player("A", 0)
            p1 = new_player("A", 1)
            p2 = new_player("B", 0)
            await db.flush()
            for player in (p0, p1):
                db.add(
                    PlayerTeamAssignment(
                        player_id=player.id, team_id=team_a.id, level_id=level.id, role="playing_11"
                    )
                )
            db.add(
                PlayerTeamAssignment(
                    player_id=p2.id, team_id=team_b.id, level_id=level.id, role="playing_11"
                )
            )
            await db.flush()

            # Match A (2025): P0 5 off 2 not out; P1 out caught by P2; P2 bowls.
            match_a = _new_match(db, admin, team_a, team_b, date(2025, 6, 1))
            await db.flush()
            await _seed_innings(
                db,
                match_a,
                [p0.id, p1.id],
                p2.id,
                [
                    {"striker": p0.id, "non_striker": p1.id, "runs": 4},
                    {"striker": p0.id, "non_striker": p1.id, "runs": 1},
                    {"striker": p1.id, "non_striker": p0.id, "runs": 2},
                    {
                        "striker": p1.id,
                        "non_striker": p0.id,
                        "wicket": WicketType.CAUGHT.value,
                        "dismissed": p1.id,
                        "fielder": p2.id,
                    },
                ],
            )
            await set_match_completed(db, match_a)

            # Match B (2026, Spring Cup): P0 12 off 2 not out; P1 1.
            match_b = _new_match(db, admin, team_a, team_b, date(2026, 3, 1))
            await db.flush()
            await _seed_innings(
                db,
                match_b,
                [p0.id, p1.id],
                p2.id,
                [
                    {"striker": p0.id, "non_striker": p1.id, "runs": 6},
                    {"striker": p0.id, "non_striker": p1.id, "runs": 6},
                    {"striker": p0.id, "non_striker": p1.id, "runs": 2},
                ],
            )
            await set_match_completed(db, match_b)

            tournament = Tournament(name="Spring Cup", level_id=level.id, user_id=admin.id)
            db.add(tournament)
            await db.flush()
            db.add(
                TournamentFixture(
                    tournament_id=tournament.id,
                    team_a_id=team_a.id,
                    team_b_id=team_b.id,
                    match_id=match_b.id,
                )
            )

            await db.commit()
            return {
                "admin": admin,
                "p0": p0.id,
                "p2": p2.id,
                "team_b": team_b,
                "match_a": match_a.id,
                "match_b": match_b.id,
                "level_id": level.id,
                "level_name": level.name,
            }

    return asyncio.run(setup())


def _career(client, player_id, **params):
    resp = client.get(f"/v1/players/{player_id}/career-stats", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_career_batting_totals(client, as_admin, world):
    as_admin(world["admin"])
    body = _career(client, world["p0"])

    assert body["player_id"] == world["p0"]
    assert body["player_code"]
    assert body["full_name"]
    assert body["scope"] == "completed"

    career = body["career"]
    assert career["matches_played"] == 2
    assert career["innings_played"] == 2

    bat = career["batting"]
    assert bat["innings"] == 2
    assert bat["not_outs"] == 2
    assert bat["dismissals"] == 0
    assert bat["runs"] == 19
    assert bat["balls_faced"] == 5
    assert bat["fours"] == 1
    assert bat["sixes"] == 2
    assert bat["highest_score"] == 14
    assert bat["highest_score_not_out"] is True
    assert bat["highest_score_display"] == "14*"
    assert bat["strike_rate"] == 380.0
    assert bat["average"] is None

    # P0 never bowled.
    assert career["bowling"]["innings"] == 0
    # P0 was never credited with a fielding dismissal.
    assert career["fielding"]["available"] is True
    assert career["fielding"]["total_dismissals"] == 0


def test_career_fielding_credited_to_fielder(client, as_admin, world):
    as_admin(world["admin"])
    career = _career(client, world["p2"])["career"]

    # P2 only ever bowled and fielded, never batted.
    assert career["innings_played"] == 0
    assert career["bowling"]["innings"] == 2
    assert career["bowling"]["balls_bowled"] == 7
    assert career["bowling"]["wickets"] == 1
    assert career["fielding"]["catches"] == 1
    assert career["fielding"]["total_dismissals"] == 1


def test_season_splits_newest_first(client, as_admin, world):
    as_admin(world["admin"])
    seasons = _career(client, world["p0"])["by_season"]

    assert [s["season"] for s in seasons] == [2026, 2025]
    assert seasons[0]["batting"]["runs"] == 14
    assert seasons[1]["batting"]["runs"] == 5
    assert sum(s["batting"]["runs"] for s in seasons) == 19


def test_competition_and_team_splits(client, as_admin, world):
    as_admin(world["admin"])
    body = _career(client, world["p0"])

    competitions = body["by_competition"]
    assert len(competitions) == 1
    assert competitions[0]["level_id"] == world["level_id"]
    assert competitions[0]["level_name"] == world["level_name"]
    assert competitions[0]["batting"]["runs"] == 19

    assert len(body["by_team"]) == 1
    team = body["by_team"][0]
    assert team["team_name"] == "Career A"
    assert team["batting"]["runs"] == 19


def test_bowler_only_player_attributes_to_bowling_team(client, as_admin, world):
    as_admin(world["admin"])
    body = _career(client, world["p2"])
    assert len(body["by_team"]) == 1
    assert body["by_team"][0]["team_name"] == "Career B"


def test_tournament_splits_include_unassigned(client, as_admin, world):
    as_admin(world["admin"])
    tournaments = _career(client, world["p0"])["by_tournament"]
    assert [t["tournament_name"] for t in tournaments] == ["No tournament", "Spring Cup"]
    spring = next(t for t in tournaments if t["tournament_name"] == "Spring Cup")
    assert spring["batting"]["runs"] == 14
    assert spring["tournament_id"] is not None


def test_recent_form_newest_first_and_limited(client, as_admin, world):
    as_admin(world["admin"])
    body = _career(client, world["p0"])
    form = body["recent_form"]
    assert len(form) == 2
    assert form[0]["match_id"] == world["match_b"]
    assert form[1]["match_id"] == world["match_a"]
    assert form[0]["batting"]["runs"] == 14
    assert form[0]["batting"]["did_bat"] is True
    assert form[0]["fielding"]["available"] is True

    limited = _career(client, world["p0"], recent=1)
    assert len(limited["recent_form"]) == 1
    assert limited["recent_form"][0]["match_id"] == world["match_b"]


def test_career_stats_404_for_other_accounts(client, as_admin, world):
    as_admin(world["admin"])
    assert client.get("/v1/players/99999999/career-stats").status_code == 404
