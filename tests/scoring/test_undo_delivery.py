"""End-to-end tests for the real backend undo (POST .../deliveries/undo).

Undo is a SERVER operation here: the delivery row is deleted from the
append-only log and everything else - team total, batter figures, bowler
figures, balls and overs, wickets, the crease, the current over and the ball
history - is replayed from what remains. Each test therefore asserts the full
`ScorecardResponse` after the undo, not just a total, because a UI that shows
"42/1 (3.2 ov)" while the log says otherwise is a scoring bug.

Also covered:

- a pending "who comes in next?" pick for a wicket the undo erased is cleared,
  so it cannot silently fill the crease slot of a LATER wicket
- a pick the undone ball had consumed is restored, so the reopened vacancy does
  not ask the scorer to choose the same batter twice
- the innings the undone ball completed is reopened (the successor row it
  created is dropped and `current_innings_number` moves back)
- nothing to undo / completed matches are refused, never partially applied
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
from app.scoring.models import Innings
from app.teams.models import PlayerTeamAssignment, Team

EMAIL = "undo_api@test.com"


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


@pytest.fixture(scope="module")
def world():
    """An admin, two teams with an 11-player squad each, and one match."""

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Undo Admin",
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

            def new_team(team, short):
                team.name = f"{short} Team"
                team.short_name = short
                team.homeground = "Test Ground"
                team.founder = "Founder"
                team.founded_year = 2020
                team.owner = "Owner"
                team.country_id = country.id
                team.state_id = state.id
                team.city_id = city.id
                team.level_id = level.id
                team.user_id = admin.id
                db.add(team)
                return team

            team_a = new_team(Team(), f"UDA {admin.id}")
            team_b = new_team(Team(), f"UDB {admin.id}")
            await db.flush()

            squads = {}
            for team, prefix in ((team_a, "A"), (team_b, "B")):
                ids = []
                for i in range(11):
                    email = f"undo_{prefix}{i}_{admin.id}@test.com"
                    player = (
                        await db.execute(select(Player).where(Player.email == email))
                    ).scalar_one_or_none()
                    if player is None:
                        player = Player(
                            first_name=f"Bat{prefix}",
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
                            mobile_number=9800000000 + int(f"{prefix}{i}", 36),
                            email=email,
                        )
                        db.add(player)
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

            match = Match(
                match_date=date(2026, 1, 1),
                match_time="18:00",
                venue="Test Ground",
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
            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "batting": squads[team_a.id],
                "bowling": squads[team_b.id],
                "match_id": match.id,
            }

    return asyncio.run(setup())


@pytest.fixture
def new_match(client, as_admin, world):
    """Factory: a fresh match with its first innings already started."""

    async def setup(order_size, total_overs):
        async with async_session() as db:
            match = Match(
                match_date=date(2026, 1, 3),
                match_time="18:00",
                venue="Test Ground",
                match_type="T20",
                result=None,
                team_a_id=world["team_a"],
                team_b_id=world["team_b"],
                toss_winner_id=world["team_a"],
                toss_decision="bat",
                status="live",
                current_innings_number=0,
                total_overs=total_overs,
                user_id=world["admin"].id,
            )
            db.add(match)
            await db.commit()
            return match.id

    def _start(order_size=11, total_overs=None):
        as_admin(world["admin"])
        match_id = asyncio.run(setup(order_size, total_overs))
        resp = client.post(
            f"/v1/matches/{match_id}/start-innings",
            json={"batting_order": world["batting"][:order_size]},
        )
        assert resp.status_code == 200, resp.text
        return {**world, "match_id": match_id}

    return _start


def _ball(client, match_id, striker, non_striker, bowler, **over):
    payload = {
        "striker_id": striker,
        "non_striker_id": non_striker,
        "bowler_id": bowler,
        "runs_batsman": 0,
        "runs_extras": 0,
        "extra_type": "none",
        "wicket_type": None,
        "dismissed_player_id": None,
    }
    payload.update(over)
    return client.post(f"/v1/matches/{match_id}/deliveries", json=payload)


def _wicket(client, match_id, striker, non_striker, bowler, dismissed):
    return _ball(
        client,
        match_id,
        striker,
        non_striker,
        bowler,
        wicket_type="bowled",
        dismissed_player_id=dismissed,
    )


def _undo(client, match_id):
    return client.post(f"/v1/matches/{match_id}/deliveries/undo")


def _batsman(card, player_id):
    return next(b for b in card["batsmen"] if b["player_id"] == player_id)


def test_undo_a_run_restores_score_batter_bowler_and_ball_log(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    resp = _ball(client, m["match_id"], a[0], a[1], b[0], runs_batsman=4)
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 4
    assert resp.json()["legal_balls"] == 1

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["total"] == 0
    assert card["legal_balls"] == 0
    assert card["overs_bowled_str"] == "0.0"
    assert card["extras"] == 0
    assert card["striker_id"] == a[0]  # the crease is back to the openers
    assert card["non_striker_id"] == a[1]
    striker = _batsman(card, a[0])
    assert striker["runs"] == 0 and striker["balls_faced"] == 0
    assert striker["fours"] == 0 and not striker["out"]
    bowlers = {x["player_id"]: x for x in card["bowlers"]}
    if b[0] in bowlers:
        assert bowlers[b[0]]["runs_conceded"] == 0
        assert bowlers[b[0]]["balls_bowled"] == 0

    # The deletion is persisted, not just recomputed for this response.
    fresh = client.get(f"/v1/matches/{m['match_id']}/scorecard").json()
    assert fresh["total"] == 0 and fresh["legal_balls"] == 0
    assert fresh["striker_id"] == a[0]

    # The log is empty, so there is nothing left to take back.
    resp = _undo(client, m["match_id"])
    assert resp.status_code == 409, resp.text


def test_undo_a_wide_removes_the_extra_and_keeps_the_ball_illegal(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    resp = _ball(client, m["match_id"], a[0], a[1], b[0], extra_type="wide", runs_extras=1)
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert card["total"] == 1 and card["extras"] == 1
    assert card["legal_balls"] == 0  # a wide is not a ball of the over

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["total"] == 0 and card["extras"] == 0
    assert card["legal_balls"] == 0
    bowlers = {x["player_id"]: x for x in card["bowlers"]}
    if b[0] in bowlers:
        assert bowlers[b[0]]["wides"] == 0


def test_undo_a_wicket_restores_wickets_fow_and_the_dismissed_batter(client, new_match):
    m = new_match(order_size=3)
    a, b = m["batting"], m["bowling"]

    resp = _wicket(client, m["match_id"], a[0], a[1], b[0], a[0])
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert card["wickets"] == 1
    assert card["awaiting_batsman"] is True
    assert card["fall_of_wickets"]

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["wickets"] == 0
    assert card["awaiting_batsman"] is False
    assert card["fall_of_wickets"] == []
    assert card["striker_id"] == a[0]  # the out opener is back at the crease
    assert card["non_striker_id"] == a[1]
    assert _batsman(card, a[0])["out"] is False
    assert _batsman(card, a[0])["dismissal"] is None
    bowlers = {x["player_id"]: x for x in card["bowlers"]}
    if b[0] in bowlers:
        assert bowlers[b[0]]["wickets"] == 0


def test_undo_clears_a_pending_incoming_pick_for_the_erased_wicket(client, new_match):
    m = new_match(order_size=3)
    a, b = m["batting"], m["bowling"]

    assert _wicket(client, m["match_id"], a[0], a[1], b[0], a[0]).status_code == 201
    pick = client.post(f"/v1/matches/{m['match_id']}/batting-order", json={"player_id": a[2]})
    assert pick.status_code in (200, 201), pick.text

    # Undo the wicket the pick was made for.
    assert _undo(client, m["match_id"]).status_code == 200

    # A later wicket must ASK for a batter again. If the stale pick survived it
    # would silently fill this vacancy with a[2] on replay.
    assert _wicket(client, m["match_id"], a[0], a[1], b[0], a[0]).status_code == 201
    card = client.get(f"/v1/matches/{m['match_id']}/scorecard").json()
    assert card["awaiting_batsman"] is True
    assert card["striker_id"] is None
    assert card["non_striker_id"] == a[1]


def test_undo_restores_the_pick_a_delivery_had_consumed(client, new_match):
    m = new_match(order_size=3)
    a, b = m["batting"], m["bowling"]

    assert _wicket(client, m["match_id"], a[0], a[1], b[0], a[0]).status_code == 201
    # The next delivery is what fills the vacancy, and it runs one (crosses).
    resp = _ball(client, m["match_id"], a[2], a[1], b[0], runs_batsman=1)
    assert resp.status_code == 201, resp.text
    assert resp.json()["striker_id"] == a[1]

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 200, resp.text
    card = resp.json()
    # The vacancy is open again and the batter the scorer already chose for it
    # is re-picked, so the crease does not ask him twice.
    assert card["awaiting_batsman"] is False
    assert card["striker_id"] == a[2]
    assert card["non_striker_id"] == a[1]
    assert card["wickets"] == 1  # the earlier wicket still stands

    # Scoring continues without a second pick.
    resp = _ball(client, m["match_id"], a[2], a[1], b[0], runs_batsman=2)
    assert resp.status_code == 201, resp.text


def test_undo_reopens_an_innings_the_ball_had_completed(client, new_match):
    m = new_match(total_overs=1)  # a 6-ball innings
    a, b = m["batting"], m["bowling"]
    striker, non_striker = a[0], a[1]

    for _ in range(5):
        resp = _ball(client, m["match_id"], striker, non_striker, b[0], runs_batsman=1)
        assert resp.status_code == 201, resp.text
        # one run crosses the batters every ball
        striker, non_striker = non_striker, striker

    resp = _ball(client, m["match_id"], striker, non_striker, b[0], runs_batsman=2)
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert card["completed"] is True
    assert card["total"] == 7

    # The completed innings opened the chase, so the match now reads innings 2.
    card = client.get(f"/v1/matches/{m['match_id']}/scorecard").json()
    assert card["innings_number"] == 2
    assert card["target"] == 8

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["innings_number"] == 1
    assert card["completed"] is False
    assert card["legal_balls"] == 5
    assert card["total"] == 5

    # The successor innings the ball created is gone and the match scores in
    # innings 1 again.
    async def state():
        async with async_session() as db:
            rows = (
                (await db.execute(select(Innings).where(Innings.match_id == m["match_id"])))
                .scalars()
                .all()
            )
            match = (await db.execute(select(Match).where(Match.id == m["match_id"]))).scalar_one()
            return len(rows), match.current_innings_number

    innings_count, current = asyncio.run(state())
    assert innings_count == 1
    assert current == 1
    assert client.get(f"/v1/matches/{m['match_id']}/scorecard").json()["innings_number"] == 1


def test_undo_is_refused_on_a_completed_match(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]
    assert _ball(client, m["match_id"], a[0], a[1], b[0], runs_batsman=1).status_code == 201

    async def complete():
        async with async_session() as db:
            match = (await db.execute(select(Match).where(Match.id == m["match_id"]))).scalar_one()
            match.status = "completed"
            await db.commit()

    asyncio.run(complete())

    resp = _undo(client, m["match_id"])
    assert resp.status_code == 409, resp.text

    # Nothing was removed: the score is still there.
    card = client.get(f"/v1/matches/{m['match_id']}/scorecard").json()
    assert card["total"] == 1
