"""Tests for the per-match customisable over count.

A match carries its own ``total_overs`` - an 8-over junior game, a 15-over
rain-shortened one - instead of being locked to the format default (T20 20, ODI
50, Test unlimited). The value is editable while the match is live, but it can
never be dropped below the overs already bowled.

The engine reads the limit through one helper, so the scorecard's ``max_overs``,
the per-bowler quota and the innings-end rule all follow it. These tests pin that
agreement, and the create/update API that sets the value.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

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
from app.scoring.crud import ScoringRepository
from app.scoring.engine import (
    DeliveryInput,
    InvalidDeliveryError,
    ScoreEngine,
    max_overs_for,
)
from app.scoring.enums import ExtraType
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.teams.models import PlayerTeamAssignment, Team

EMAIL = "custom_overs@test.com"


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


def _over_deliveries(innings_id, over, striker, non_striker, bowler):
    """Six dot balls - one legal over - so replay does not move the crease."""
    return [
        Delivery(
            innings_id=innings_id,
            over_number=over,
            ball_number=ball + 1,
            striker_id=striker,
            non_striker_id=non_striker,
            bowler_id=bowler,
            runs_batsman=0,
            runs_extras=0,
            extra_type=ExtraType.NONE.value,
            wicket_type=None,
            dismissed_player_id=None,
        )
        for ball in range(6)
    ]


async def _seed_innings(db, match, batting_order, bowlers, overs):
    innings = Innings(
        match_id=match.id,
        batting_team_id=match.team_a_id,
        bowling_team_id=match.team_b_id,
        innings_number=1,
    )
    db.add(innings)
    await db.flush()
    for position, player_id in enumerate(batting_order, start=1):
        db.add(InningsBatsman(innings_id=innings.id, player_id=player_id, position=position))
    striker, non_striker = batting_order[0], batting_order[1]
    for index in range(overs):
        for delivery in _over_deliveries(
            innings.id, index + 1, striker, non_striker, bowlers[index % len(bowlers)]
        ):
            db.add(delivery)
        striker, non_striker = non_striker, striker
    return innings


def _new_match(db, admin, team_a, team_b, total_overs):
    match = Match(
        match_date=date(2026, 5, 1),
        match_time="18:00",
        venue="Overs Ground",
        match_type="T20",
        result=None,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status="live",
        current_innings_number=1,
        total_overs=total_overs,
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
                    full_name="Custom Overs Admin",
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
                    homeground="Overs Ground",
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

            team_a = new_team("Overs A", "OVA")
            team_b = new_team("Overs B", "OVB")
            await db.flush()

            def make_squad(prefix):
                players = []
                for index in range(6):
                    player = Player(
                        first_name=f"{prefix}Bat",
                        last_name=f"Player{index}",
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
                        mobile_number=9720000000 + int(f"{prefix}{index}", 36),
                        email=f"custom_overs_{prefix}{index}_{admin.id}@test.com",
                    )
                    db.add(player)
                    players.append(player)
                return players

            squad_a = make_squad("OA")
            squad_b = make_squad("OB")
            await db.flush()
            team_a_ids = [p.id for p in squad_a]
            team_b_ids = [p.id for p in squad_b]
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

            # A three-over innings under an eight-over limit, for the "cannot
            # lower below what was bowled" rule.
            m_bowled = _new_match(db, admin, team_a, team_b, total_overs=8)
            await db.flush()
            await _seed_innings(db, m_bowled, team_a_ids[:2], team_b_ids[:2], overs=3)

            # A one-over innings so a one-over match reads as complete...
            m_tiny = _new_match(db, admin, team_a, team_b, total_overs=1)
            await db.flush()
            tiny = await _seed_innings(db, m_tiny, team_a_ids[:2], team_b_ids[:2], overs=1)

            # ...and a five-over limit (quota: one over per bowler) with two
            # overs already bowled, for the quota check.
            m_quota = _new_match(db, admin, team_a, team_b, total_overs=5)
            await db.flush()
            quota = await _seed_innings(db, m_quota, team_a_ids[:2], team_b_ids[:2], overs=2)

            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "bat_a": team_a_ids[:2],
                "bowl_b": team_b_ids[:2],
                "bowled_match": m_bowled.id,
                "tiny_match": m_tiny.id,
                "tiny_innings": tiny.id,
                "quota_match": m_quota.id,
                "quota_innings": quota.id,
            }

    return asyncio.run(setup())


# --- the engine helper ------------------------------------------------------


def test_custom_over_count_overrides_the_format_default():
    assert max_overs_for(Match(match_type="T20", total_overs=8)) == 8
    assert max_overs_for(Match(match_type="ODI", total_overs=15)) == 15


def test_no_custom_count_falls_back_to_the_format_default():
    assert max_overs_for(Match(match_type="T20", total_overs=None)) == 20
    assert max_overs_for(Match(match_type="ODI", total_overs=None)) == 50
    assert max_overs_for(Match(match_type="Test", total_overs=None)) is None


def test_an_unknown_format_without_a_custom_count_is_unlimited():
    assert max_overs_for(Match(match_type="Hundred", total_overs=None)) is None


def test_a_super_over_is_always_one_over_even_with_a_custom_count():
    innings = SimpleNamespace(is_super_over=True)
    assert max_overs_for(Match(match_type="T20", total_overs=8), innings) == 1


def test_bowler_quota_scales_with_the_custom_over_count():
    normal = SimpleNamespace(is_super_over=False)
    assert ScoreEngine._max_over_per_player(Match(match_type="T20", total_overs=8), normal) == 2
    assert ScoreEngine._max_over_per_player(Match(match_type="T20", total_overs=10), normal) == 2
    assert ScoreEngine._max_over_per_player(Match(match_type="T20", total_overs=4), normal) == 1
    assert ScoreEngine._max_over_per_player(Match(match_type="T20", total_overs=None), normal) == 4
    assert (
        ScoreEngine._max_over_per_player(Match(match_type="Test", total_overs=None), normal) is None
    )


def test_a_super_over_has_no_bowler_quota():
    innings = SimpleNamespace(is_super_over=True)
    assert ScoreEngine._max_over_per_player(Match(match_type="T20", total_overs=8), innings) is None


# --- the scorecard ----------------------------------------------------------


def _scorecard(innings_id):
    async def run():
        async with async_session() as db:
            engine = ScoreEngine(ScoringRepository(db))
            return await engine.get_scorecard(innings_id)

    return asyncio.run(run())


def test_a_one_over_match_ends_when_its_over_is_bowled(world):
    card = _scorecard(world["tiny_innings"])
    assert card.max_overs == 1
    assert card.completed is True
    assert card.end_reason == "overs_complete"


def test_raising_the_over_count_reopens_the_innings(world):
    async def raise_limit():
        async with async_session() as db:
            match = await db.get(Match, world["tiny_match"])
            match.total_overs = 2
            await db.commit()

    asyncio.run(raise_limit())
    card = _scorecard(world["tiny_innings"])
    assert card.max_overs == 2
    assert card.completed is False
    assert card.end_reason is None


def test_the_custom_quota_stops_a_bowler_bowling_another_over(world):
    async def attempt():
        async with async_session() as db:
            engine = ScoreEngine(ScoringRepository(db))
            striker, non_striker = world["bat_a"]
            return await engine.apply_delivery(
                world["quota_innings"],
                DeliveryInput(
                    striker_id=striker,
                    non_striker_id=non_striker,
                    bowler_id=world["bowl_b"][0],
                ),
            )

    with pytest.raises(InvalidDeliveryError, match="maximum 1 overs"):
        asyncio.run(attempt())


# --- the create / update API ------------------------------------------------


def _create(client, as_admin, world, total_overs="unset"):
    as_admin(world["admin"])
    payload = {
        "match_date": "2026-05-02",
        "match_time": "18:00",
        "venue": "Overs Ground",
        "match_type": "T20",
        "team_a_id": world["team_a"],
        "team_b_id": world["team_b"],
        "toss_winner_id": world["team_a"],
        "toss_decision": "bat",
    }
    if total_overs != "unset":
        payload["total_overs"] = total_overs
    resp = client.post("/v1/matches", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_match_accepts_a_custom_over_count(client, as_admin, world):
    assert _create(client, as_admin, world, total_overs=8)["total_overs"] == 8


def test_create_match_without_one_has_no_custom_count(client, as_admin, world):
    assert _create(client, as_admin, world)["total_overs"] is None


def test_patch_raises_lowers_and_clears_the_over_count(client, as_admin, world):
    match_id = _create(client, as_admin, world, total_overs=8)["id"]

    raised = client.patch(f"/v1/matches/{match_id}", json={"total_overs": 12})
    assert raised.status_code == 200, raised.text
    assert raised.json()["total_overs"] == 12

    lowered = client.patch(f"/v1/matches/{match_id}", json={"total_overs": 5})
    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["total_overs"] == 5

    cleared = client.patch(f"/v1/matches/{match_id}", json={"total_overs": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["total_overs"] is None


def test_an_over_count_below_the_balls_bowled_is_rejected(client, as_admin, world):
    as_admin(world["admin"])
    match_id = world["bowled_match"]
    # Three complete overs are already in the log: 3.0 overs.
    allowed = client.patch(f"/v1/matches/{match_id}", json={"total_overs": 3})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["total_overs"] == 3

    refused = client.patch(f"/v1/matches/{match_id}", json={"total_overs": 2})
    assert refused.status_code == 400, refused.text
    assert "already bowled" in refused.json()["detail"]
    assert "3.0" in refused.json()["detail"]


def test_an_over_count_in_range_is_enforced_by_the_schema(client, as_admin, world):
    as_admin(world["admin"])
    assert (
        client.post(
            "/v1/matches",
            json={
                "match_date": "2026-05-03",
                "match_time": "18:00",
                "venue": "Overs Ground",
                "match_type": "T20",
                "team_a_id": world["team_a"],
                "team_b_id": world["team_b"],
                "toss_winner_id": world["team_a"],
                "toss_decision": "bat",
                "total_overs": 0,
            },
        ).status_code
        == 422
    )
