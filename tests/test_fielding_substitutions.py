"""Substitute fielders, end to end.

Covers the substitution half of the Field Placement feature: the reserve list,
bringing a reserve on, the outgoing player leaving the field, the 11-player
cap, team and duplicate refusals, the ordinary-substitute restrictions, the
reason rules, and the history that is written.

The world seeds squads that are deliberately *longer* than 11 so there are
real reserves to substitute on, and it never assumes a squad of 11 or 15 -
the reserve list is read from the squad itself.

Like the rest of this suite it drives the real FastAPI routes against the
real database, because that is the wiring the frontend consumes. This repo
has no browser test runner, so the drag interactions themselves are asserted
against the same payloads they consume (coordinates, position keys, the
two-way sync) rather than against a DOM.
"""

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.fielding.models import FieldingAssignment, FieldingSetup, FieldSubstitution
from app.fielding.positions import DETECT_RADIUS, NON_SPOT_KEYS, nearest_anchor
from app.levels.models import TeamLevel
from app.main import app
from app.matches.models import Match
from app.models import City, Country, State
from app.players.models import Player
from app.scoring.models import Innings
from app.teams.models import PlayerTeamAssignment, Team

ADMIN_EMAIL = "substitution_admin@test.com"


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
def sides():
    """Two sides with 11 XI players plus 4 reserves each.

    Built once for the module and never mutated - a player's XI membership,
    squad role and profile are the same for every test. Only the *match* is
    per-test (see :func:`world`), because a substitution rewrites who is on
    the field and one test's field must not leak into the next.
    """

    async def setup():
        async with async_session() as db:
            admin = (
                await db.execute(select(User).where(User.email == ADMIN_EMAIL))
            ).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=ADMIN_EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="Sub Admin",
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
                    homeground="Sub Ground",
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

            team_a = new_team("Sub Alpha", "SUA")
            team_b = new_team("Sub Beta", "SUB")
            await db.flush()

            def make_squad(prefix, count):
                ids = []
                for i in range(count):
                    player = Player(
                        first_name=f"SU{prefix}",
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
                        email=f"sub_{prefix}{i}_{admin.id}@test.com",
                    )
                    db.add(player)
                    ids.append(player)
                return ids

            # 15 each: 11 in the XI, 4 sitting in the reserves.
            squad_a = make_squad("A", 15)
            squad_b = make_squad("B", 15)
            await db.flush()
            for squad, team in ((squad_a, team_a), (squad_b, team_b)):
                for i, player in enumerate(squad):
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11" if i < 11 else "substitute",
                            xi_role=(
                                "wicketkeeper" if i == 0 else ("bowler" if i == 1 else "fielder")
                            )
                            if i < 11
                            else None,
                        )
                    )
            await db.commit()

            return {
                "admin": admin,
                "team_a_id": team_a.id,
                "team_b_id": team_b.id,
                "fielding_team_id": team_b.id,
                "batting_team_id": team_a.id,
                "xi": [p.id for p in squad_b[:11]],
                "reserves": [p.id for p in squad_b[11:]],
                "batting_squad": [p.id for p in squad_a],
            }

    return asyncio.run(setup())


@pytest.fixture
def world(sides):
    """A fresh match on the shared sides, with innings 1 started.

    Every test gets its own match, so a field, a substitution or a wicket
    written by one test is invisible to the next - the XI and the reserves
    are shared, the *state* is not.
    """

    async def make():
        async with async_session() as db:
            match = Match(
                match_date=date(2026, 6, 1),
                match_time="19:00",
                venue="Sub Stadium",
                match_type="T20",
                result=None,
                team_a_id=sides["team_a_id"],
                team_b_id=sides["team_b_id"],
                toss_winner_id=sides["team_a_id"],
                toss_decision="bat",  # A bats first, so B fields in innings 1
                user_id=sides["admin"].id,
            )
            db.add(match)
            await db.flush()

            # Innings 1 started for real, so the field setup may be saved.
            from app.scoring.crud import start_scoring

            innings, error = await start_scoring(db, match, sides["batting_squad"][:2])
            assert error is None, error
            await db.commit()
            return {**sides, "match_id": match.id, "innings_id": innings.id}

    return asyncio.run(make())


def _use_admin(as_admin, world):
    as_admin(world["admin"])


def _url(world, suffix=""):
    return f"/v1/matches/{world['match_id']}/fielding-setup{suffix}"


def _put_full_field(client, w):
    """Put the keeper plus ten other XI players on the field (11 active)."""
    assignments = [{"player_id": w["xi"][0], "position_key": "wicket_keeper"}]
    keys = [
        "slip",
        "second_slip",
        "gully",
        "point",
        "cover",
        "mid_off",
        "mid_on",
        "midwicket",
        "square_leg",
        "fine_leg",
    ]
    for pid, key in zip(w["xi"][1:], keys, strict=False):
        assignments.append({"player_id": pid, "position_key": key})
    return client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": assignments,
        },
    )


# --- 1 & 2. the two lists are separate --------------------------------------


def test_playing_xi_and_reserves_are_listed_separately(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    body = client.get(_url(w)).json()

    xi_ids = [p["player_id"] for p in body["eligible_players"]]
    reserve_ids = [p["player_id"] for p in body["eligible_substitutes"]]

    # All 11 XI players, in the XI list only.
    assert len(xi_ids) == 11
    assert xi_ids == w["xi"]
    # The reserves are the *other* squad members - not assumed to be 11 or 15.
    assert reserve_ids == w["reserves"]
    assert len(reserve_ids) == 4
    assert not set(xi_ids) & set(reserve_ids)

    # Labels: XI carries its field job, reserves are marked (SUB).
    keeper = next(p for p in body["eligible_players"] if p["player_id"] == w["xi"][0])
    assert keeper["xi_role"] == "wicketkeeper"
    bowler = next(p for p in body["eligible_players"] if p["player_id"] == w["xi"][1])
    assert bowler["xi_role"] == "bowler"
    for r in body["eligible_substitutes"]:
        assert r["squad_role"] in ("substitute", "bench")
        assert r["xi_role"] is None
        assert r["is_substitute"] is False
        assert r["on_field"] is False

    # Nothing is placed yet, so both lists say so rather than guessing.
    assert all(p["on_field"] is False for p in body["eligible_players"])
    assert body["assignments"] == []
    assert body["max_active_fielders"] == 11


def test_reserve_list_is_empty_not_an_error_when_there_are_none(client, world, as_admin):
    """A squad with no reserves returns an empty list, not a failure."""
    w = world
    _use_admin(as_admin, w)

    async def strip_reserves():
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(PlayerTeamAssignment).where(
                            PlayerTeamAssignment.team_id == w["fielding_team_id"],
                            PlayerTeamAssignment.role != "playing_11",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                r.role = "playing_11"
            await db.commit()
            return [(r.player_id) for r in rows]

    moved = asyncio.run(strip_reserves())
    try:
        body = client.get(_url(w)).json()
        assert body["eligible_substitutes"] == []
        assert len(body["eligible_players"]) == 11 + len(moved)
    finally:

        async def restore():
            async with async_session() as db:
                rows = (
                    (
                        await db.execute(
                            select(PlayerTeamAssignment).where(
                                PlayerTeamAssignment.team_id == w["fielding_team_id"],
                                PlayerTeamAssignment.player_id.in_(moved),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for r in rows:
                    r.role = "substitute"
                await db.commit()

        asyncio.run(restore())


# --- 3. placing an XI player ------------------------------------------------


def test_place_xi_player_and_reload(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    resp = _put_full_field(client, w)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["assignments"]) == 11
    on_field = {p["player_id"]: p["position_key"] for p in body["eligible_players"]}
    assert on_field[w["xi"][0]] == "wicket_keeper"
    assert sum(1 for v in on_field.values() if v) == 11

    # A fresh read (as if the page were reloaded) returns the same field.
    fresh = client.get(_url(w)).json()
    assert len(fresh["assignments"]) == 11
    assert fresh["wicketkeeper_player_id"] == w["xi"][0]


# --- 4, 5. bringing a reserve on --------------------------------------------


def test_bring_substitute_onto_field(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    outgoing = w["xi"][5]  # currently at cover
    incoming = w["reserves"][0]

    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": outgoing,
            "incoming_player_id": incoming,
            "reason": "injury",
            "position_key": "point",
            "anchor_x": 76.0,
            "anchor_y": 68.0,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The outgoing player is off the field; the reserve is on it, in their place.
    active_ids = {a["player_id"] for a in body["assignments"]}
    assert outgoing not in active_ids
    assert incoming in active_ids
    assert len(active_ids) == 11  # still exactly 11

    sub_row = next(a for a in body["assignments"] if a["player_id"] == incoming)
    assert sub_row["position_key"] == "point"
    assert sub_row["is_substitute"] is True
    assert sub_row["anchor_x"] == 76.0 and sub_row["anchor_y"] == 68.0

    # The XI and reserve lists both reflect the change.
    outgoing_entry = next(p for p in body["eligible_players"] if p["player_id"] == outgoing)
    assert outgoing_entry["on_field"] is False
    assert outgoing_entry["position_key"] is None
    incoming_entry = next(p for p in body["eligible_substitutes"] if p["player_id"] == incoming)
    assert incoming_entry["on_field"] is True
    assert incoming_entry["is_substitute"] is True
    assert incoming_entry["position_key"] == "point"

    # The substitution is in the match history, with who authorised it.
    assert len(body["substitutions"]) == 1
    hist = body["substitutions"][0]
    assert hist["outgoing_player_id"] == outgoing
    assert hist["incoming_player_id"] == incoming
    assert hist["reason"] == "injury"
    assert hist["replacement_type"] == "normal"
    assert hist["authorized_by_user_id"] == w["admin"].id
    assert hist["outgoing_player_name"] and hist["incoming_player_name"]

    # And it survives a reload from the database.
    fresh = client.get(_url(w)).json()
    assert len(fresh["substitutions"]) == 1
    assert fresh["substitutions"][0]["incoming_player_id"] == incoming

    # The outgoing player keeps their profile and their squad membership.
    async def still_a_member():
        async with async_session() as db:
            row = (
                (
                    await db.execute(
                        select(PlayerTeamAssignment).where(
                            PlayerTeamAssignment.player_id == outgoing,
                            PlayerTeamAssignment.team_id == w["fielding_team_id"],
                        )
                    )
                )
                .scalars()
                .first()
            )
            player = (
                (await db.execute(select(Player).where(Player.id == outgoing))).scalars().first()
            )
            return row is not None and row.role == "playing_11", player is not None

    still_xi, has_profile = asyncio.run(still_a_member())
    assert has_profile
    assert still_xi  # the original XI is intact; only the field changed


def test_substitute_unplaced_then_placed_like_any_fielder(client, world, as_admin):
    """A reserve may come on with no position and be placed afterwards."""
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    outgoing = w["xi"][3]
    incoming = w["reserves"][1]

    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": outgoing,
            "incoming_player_id": incoming,
            "reason": "illness",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 10 placed + the new man not yet placed.
    assert len(body["assignments"]) == 10
    entry = next(p for p in body["eligible_substitutes"] if p["player_id"] == incoming)
    assert entry["on_field"] is False

    # Now place them, exactly like an XI player.
    assignments = [a for a in body["assignments"]] + [
        {"player_id": incoming, "position_key": "cover", "anchor_x": 72.0, "anchor_y": 56.0}
    ]
    save = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": assignments,
        },
    )
    assert save.status_code == 200, save.text
    placed = next(a for a in save.json()["assignments"] if a["player_id"] == incoming)
    assert placed["position_key"] == "cover"
    assert placed["is_substitute"] is True  # still flagged as a substitute


# --- 6. never more than 11 ---------------------------------------------------


def test_field_never_holds_more_than_eleven(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    # Twelve players, one of them from the opposition: refused outright.
    resp = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [{"player_id": pid, "position_key": "slip"} for pid in w["xi"]]
            + [{"player_id": w["reserves"][0], "position_key": "point"}],
        },
    )
    assert resp.status_code == 422
    assert "at most 11" in resp.text

    # And a real substitution never pushes the count past 11 either: the
    # outgoing player is removed in the same transaction that adds the
    # incoming one (who is given a position here, so eleven stay placed).
    before = client.get(_url(w)).json()
    assert len(before["assignments"]) == 11
    sub = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][2],
            "incoming_player_id": w["reserves"][0],
            "reason": "injury",
            "position_key": "second_slip",
        },
    )
    assert sub.status_code == 200
    active = {a["player_id"] for a in sub.json()["assignments"]}
    assert len(active) <= 11
    assert len(active) == 11
    assert w["reserves"][0] in active


# --- 7 & 8. wrong team and duplicates ---------------------------------------


def test_reject_player_from_another_team(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    # An XI player of the *batting* side cannot be brought on to field.
    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][4],
            "incoming_player_id": w["batting_squad"][3],
            "reason": "injury",
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "other side" in detail or "batting side" in detail

    # Nor can a player who belongs to no team at all.
    async def stranger():
        async with async_session() as db:
            in_xi = select(PlayerTeamAssignment.player_id)
            p = (
                (await db.execute(select(Player.id).where(Player.id.notin_(in_xi)).limit(1)))
                .scalars()
                .first()
            )
            if p is not None:
                return p
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()
            player = Player(
                first_name="No",
                last_name="Team",
                date_of_birth=date(1999, 1, 1),
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
                mobile_number=9500000002,
                email="sub_no_team@test.com",
            )
            db.add(player)
            await db.commit()
            return player.id

    pid = asyncio.run(stranger())
    resp2 = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][4],
            "incoming_player_id": pid,
            "reason": "injury",
        },
    )
    assert resp2.status_code == 400
    assert "not registered" in resp2.json()["detail"]


def test_reject_duplicate_active_and_already_active_substitute(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    # An XI player already on the field cannot be "substituted on".
    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][2],
            "incoming_player_id": w["xi"][7],
            "reason": "injury",
        },
    )
    assert resp.status_code == 400
    assert "in the playing XI" in resp.json()["detail"]

    # Bring a reserve on *with a place to stand*, then try to bring the same
    # man on again.
    first = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][2],
            "incoming_player_id": w["reserves"][0],
            "reason": "injury",
            "position_key": "gully",
        },
    )
    assert first.status_code == 200, first.text

    again = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][3],
            "incoming_player_id": w["reserves"][0],
            "reason": "injury",
        },
    )
    assert again.status_code == 400
    assert "already on the field" in again.json()["detail"]

    # Sending the same player off the field when they are not on it fails too.
    not_on = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["reserves"][2],
            "incoming_player_id": w["reserves"][1],
            "reason": "injury",
        },
    )
    assert not_on.status_code == 400
    assert "not on the field" in not_on.json()["detail"]

    # A player cannot replace themselves.
    self_sub = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][5],
            "incoming_player_id": w["xi"][5],
            "reason": "injury",
        },
    )
    assert self_sub.status_code == 422


# --- ordinary vs approved special replacements ------------------------------


def test_ordinary_substitute_may_not_bowl_or_keep(client, world, as_admin):
    """The Laws: an ordinary substitute cannot bowl or keep wicket."""
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    reserve = w["reserves"][0]
    sub = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][6],
            "incoming_player_id": reserve,
            "reason": "injury",
        },
    )
    assert sub.status_code == 200, sub.text

    # Trying to hand them the gloves is refused.
    assignments = [a for a in sub.json()["assignments"] if a["player_id"] != w["xi"][0]] + [
        {"player_id": reserve, "position_key": "wicket_keeper"}
    ]
    resp = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": reserve,
            "assignments": assignments,
        },
    )
    assert resp.status_code == 400
    assert "ordinary substitute" in resp.json()["detail"]
    assert "keep wicket" in resp.json()["detail"]

    # And the bowler's mark is refused too.
    resp2 = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [a for a in assignments if a["position_key"] != "wicket_keeper"]
            + [{"player_id": reserve, "position_key": "bowler"}],
        },
    )
    assert resp2.status_code == 400
    assert "may not bowl" in resp2.json()["detail"]

    # Naming them keeper without giving them the anchor is also refused.
    resp3 = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][7],
            "incoming_player_id": w["reserves"][1],
            "reason": "injury",
            "position_key": "wicket_keeper",
        },
    )
    assert resp3.status_code == 400
    assert "keep wicket" in resp3.json()["detail"]


def test_approved_special_replacement_keeps_privileges(client, world, as_admin):
    """A competition-approved concussion replacement is not an ordinary sub.

    The reason has to be on the competition's *special* list - which is
    configurable per deployment (see app.fielding.rules) - so a reserve is
    never silently treated as an officially approved replacement.
    """
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    reserve = w["reserves"][0]
    sub = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][6],
            "incoming_player_id": reserve,
            "reason": "concussion",
            "position_key": "cover",
        },
    )
    assert sub.status_code == 200, sub.text
    body = sub.json()
    # The history accumulates across this module's tests, so read the row
    # this substitution just wrote rather than the first one ever written.
    entry = [s for s in body["substitutions"] if s["incoming_player_id"] == reserve][-1]
    assert entry["reason"] == "concussion"
    assert entry["replacement_type"] == "special"

    # They may be named keeper as an approved special replacement. Their
    # existing spot is released first, so one player holds one position.
    assignments = [
        a for a in body["assignments"] if a["player_id"] not in (w["xi"][0], reserve)
    ] + [{"player_id": reserve, "position_key": "wicket_keeper"}]
    resp = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": reserve,
            "assignments": assignments,
        },
    )
    assert resp.status_code == 200, resp.text


def test_reason_must_be_permitted_by_the_competition(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    # The payload advertises the rules the UI offers.
    body = client.get(_url(w)).json()
    rules = body["substitution_rules"]
    assert rules["enabled"] is True
    values = {r["value"] for r in rules["reasons"]}
    assert {"injury", "illness", "concussion"} <= values
    special = {r["value"] for r in rules["reasons"] if r["type"] == "special"}
    assert special == {"concussion"}

    # A reason outside the allowed set is refused.
    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][4],
            "incoming_player_id": w["reserves"][0],
            "reason": "because_i_want_to",
        },
    )
    assert resp.status_code == 400
    assert "not permitted" in resp.json()["detail"]

    # And with substitutions switched off for the competition, nothing goes.
    import os

    os.environ["SUBSTITUTION_ENABLED"] = "false"
    try:
        off = client.post(
            _url(w, "/substitution"),
            json={
                "innings_number": 1,
                "outgoing_player_id": w["xi"][4],
                "incoming_player_id": w["reserves"][0],
                "reason": "injury",
            },
        )
        assert off.status_code == 400
        assert "not permitted" in off.json()["detail"]
        assert client.get(_url(w)).json()["substitution_rules"]["enabled"] is False
    finally:
        os.environ.pop("SUBSTITUTION_ENABLED", None)


# --- 9 to 13: what the browser drag consumes --------------------------------


def test_drop_detection_snapsl_to_the_nearest_anchor(client, world, as_admin):
    """A marker released in the Slip zone becomes Slip, and so on.

    This is the server half of the drag: the same nearest-anchor rule the
    panel runs on release, asserted here against the same catalog the panel
    is given, so the position the UI shows and the position the server
    stores cannot disagree. Nothing in it depends on screen pixels - the
    coordinates are normalized 0-100 on the diagram.
    """
    w = world
    _use_admin(as_admin, w)

    # Drag from slip to point: the position key follows the coordinates.
    assert nearest_anchor(60.0, 86.0) == "slip"
    assert nearest_anchor(76.0, 68.0) == "point"
    assert nearest_anchor(72.0, 56.0) == "cover"

    # A point dragged between two anchors resolves to the *nearer* one, and
    # that is stable: the same input always gives the same answer, whatever
    # the catalog's ordering.
    between = nearest_anchor(63.0, 77.0)
    assert between is not None
    # Whichever it picked, it must be the closest anchor to that point.
    from app.fielding.positions import ANCHORS as _ANCHORS

    d2s = {k: (63.0 - ax) ** 2 + (77.0 - ay) ** 2 for k, (ax, ay) in _ANCHORS.items()}
    assert d2s[between] == min(d2s.values())

    # Outside every zone -> no anchor, so the panel marks it Custom Position.
    assert nearest_anchor(50.0, 50.0, radius=1.0) is None

    # The keeper's anchor is only offered to the designated keeper: with the
    # keeper excluded, (50, 88) reaches no ordinary anchor at all.
    assert nearest_anchor(50.0, 88.0) is None
    assert nearest_anchor(50.0, 88.0, include_keeper=True) == "wicket_keeper"

    # Custom and Unassigned are concepts, never a detection target.
    assert NON_SPOT_KEYS == {"custom_position", "unassigned"}


def test_dropdown_change_moves_marker_and_drag_changes_dropdown(client, world, as_admin):
    """Both directions of the sync, in the payloads they travel in.

    Direction 1: pick "Cover" from the list -> the marker moves to Cover's
    anchor. Direction 2: drag the marker to Point -> the stored position
    becomes Point with the drop coordinates. Each is a save, and each is
    read back, so the assertion is about persisted state rather than about
    pixels.
    """
    w = world
    _use_admin(as_admin, w)

    p = w["xi"][2]
    put = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [
                {"player_id": w["xi"][0], "position_key": "wicket_keeper"},
                {"player_id": p, "position_key": "cover"},
            ],
        },
    )
    assert put.status_code == 200, put.text
    # Direction 1: the list choice moved the marker to Cover's anchor.
    marked = next(a for a in put.json()["assignments"] if a["player_id"] == p)
    assert marked["position_key"] == "cover"
    assert (marked["anchor_x"], marked["anchor_y"]) == (72.0, 56.0)

    # Direction 2: drag to Point with an explicit drop point.
    dragged = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [
                {"player_id": w["xi"][0], "position_key": "wicket_keeper"},
                {
                    "player_id": p,
                    "position_key": "point",
                    "anchor_x": 74.5,
                    "anchor_y": 69.5,
                },
            ],
        },
    )
    assert dragged.status_code == 200, dragged.text
    stored = next(a for a in dragged.json()["assignments"] if a["player_id"] == p)
    assert stored["position_key"] == "point"
    assert (stored["anchor_x"], stored["anchor_y"]) == (74.5, 69.5)

    # Reload: the coordinates are what the diagram redraws, so resizing the
    # screen cannot move the marker - they are stored normalized, not as
    # pixels.
    fresh = client.get(_url(w)).json()
    again = next(a for a in fresh["assignments"] if a["player_id"] == p)
    assert (again["anchor_x"], again["anchor_y"]) == (74.5, 69.5)
    assert 0 <= again["anchor_x"] <= 100 and 0 <= again["anchor_y"] <= 100


def test_drop_outside_every_zone_keeps_the_custom_position(client, world, as_admin):
    """A marker released where no anchor reaches is Custom Position."""
    w = world
    _use_admin(as_admin, w)

    p = w["xi"][4]
    resp = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [
                {"player_id": w["xi"][0], "position_key": "wicket_keeper"},
                {
                    "player_id": p,
                    "position_key": "custom_position",
                    "anchor_x": 88.0,
                    "anchor_y": 21.0,
                    "custom_label": "Deep mid-on, captain's call",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    stored = next(a for a in resp.json()["assignments"] if a["player_id"] == p)
    assert stored["position_key"] == "custom_position"
    assert stored["position_label"] == "Deep mid-on, captain's call"
    assert (stored["anchor_x"], stored["anchor_y"]) == (88.0, 21.0)

    # The custom label does not leak onto a catalog position.
    resp2 = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [
                {"player_id": w["xi"][0], "position_key": "wicket_keeper"},
                {"player_id": p, "position_key": "point", "custom_label": "ignored"},
            ],
        },
    )
    assert resp2.status_code == 200, resp2.text
    stored2 = next(a for a in resp2.json()["assignments"] if a["player_id"] == p)
    assert stored2["position_label"] == "Point"
    assert stored2["custom_label"] is None


def test_detection_radius_is_stable_across_scales(client, world, as_admin):
    """Detection uses diagram units, so resizing the screen changes nothing.

    The panel converts the pointer to the same 0-100 space before asking for
    the nearest anchor; the radius is in that space. Dragging to the same
    relative spot on a phone-sized or a desktop-sized diagram therefore
    resolves to the same position - which is what this asserts, by checking
    the catalog's anchors are inside the range and the radius is fixed.
    """
    w = world
    _use_admin(as_admin, w)
    body = client.get(_url(w)).json()

    anchors = [(p["anchor_x"], p["anchor_y"]) for p in body["position_catalog"]]
    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in anchors)
    assert 0 < DETECT_RADIUS <= 10

    # Every anchor detects as itself, from its own coordinates.
    for entry in body["position_catalog"]:
        if entry["key"] in NON_SPOT_KEYS:
            continue
        detected = nearest_anchor(entry["anchor_x"], entry["anchor_y"])
        if entry["key"] == "wicket_keeper":
            detected = nearest_anchor(entry["anchor_x"], entry["anchor_y"], include_keeper=True)
        assert detected == entry["key"], entry["key"]


# --- 14 & 15: innings scoping and persistence --------------------------------


def test_innings_2_gets_the_other_sides_field_and_reserves(client, world, as_admin):
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    async def open_two():
        async with async_session() as db:
            match = (
                (await db.execute(select(Match).where(Match.id == w["match_id"]))).scalars().one()
            )
            from app.scoring.crud import open_second_innings

            return await open_second_innings(db, match, None)

    innings2 = asyncio.run(open_two())
    assert innings2.bowling_team_id == w["batting_team_id"]

    try:
        body = client.get(_url(w, "?innings_number=2")).json()
        assert body["fielding_team_id"] == w["batting_team_id"]
        # The previous innings' fielders did not leak.
        assert body["assignments"] == []
        assert body["substitutions"] == []
        # ...and neither did its XI: this is the *other* side's XI.
        xi = {p["player_id"] for p in body["eligible_players"]}
        assert xi == set(w["batting_squad"][:11])
        assert not xi & set(w["xi"])
        # Its reserves are that side's reserves.
        reserves = {p["player_id"] for p in body["eligible_substitutes"]}
        assert reserves == set(w["batting_squad"][11:])
    finally:

        async def restore():
            async with async_session() as db:
                match = (
                    (await db.execute(select(Match).where(Match.id == w["match_id"])))
                    .scalars()
                    .one()
                )
                match.current_innings_number = 1
                await db.commit()

        asyncio.run(restore())

    # Innings 1's field is untouched by looking at innings 2.
    one = client.get(_url(w, "?innings_number=1")).json()
    assert len(one["assignments"]) == 11
    assert one["fielding_team_id"] == w["fielding_team_id"]


def test_substitution_is_stored_against_match_innings_and_team(client, world, as_admin):
    """The history row carries every id the requirement asks for."""
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)
    resp = client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][8],
            "incoming_player_id": w["reserves"][0],
            "reason": "injury",
        },
    )
    assert resp.status_code == 200, resp.text

    async def rows():
        async with async_session() as db:
            return (
                (
                    await db.execute(
                        select(FieldSubstitution)
                        .where(FieldSubstitution.match_id == w["match_id"])
                        .order_by(FieldSubstitution.id)
                    )
                )
                .scalars()
                .all()
            )

    stored = asyncio.run(rows())
    assert len(stored) == 1  # this match's own history, and nothing else
    s = stored[0]
    assert s.match_id == w["match_id"]
    assert s.innings_number == 1
    assert s.team_id == w["fielding_team_id"]
    assert s.outgoing_player_id == w["xi"][8]
    assert s.incoming_player_id == w["reserves"][0]
    assert s.reason == "injury"
    assert s.replacement_type == "normal"
    assert s.authorized_by_user_id == w["admin"].id
    assert s.created_at is not None

    # The assignment rows also record substitute status per player.
    async def assignments_rows():
        async with async_session() as db:
            setup = (
                (
                    await db.execute(
                        select(FieldingSetup).where(
                            FieldingSetup.match_id == w["match_id"],
                            FieldingSetup.innings_number == 1,
                        )
                    )
                )
                .scalars()
                .one()
            )
            return (
                (
                    await db.execute(
                        select(FieldingAssignment).where(FieldingAssignment.setup_id == setup.id)
                    )
                )
                .scalars()
                .all()
            )

    rows_a = asyncio.run(assignments_rows())
    # The substitution was made without a position, so the incoming player is
    # on the field but not yet placed - ten placed, one man still to position.
    assert len(rows_a) == 10
    assert [a.player_id for a in rows_a if a.is_substitute] == []
    assert w["reserves"][0] not in {a.player_id for a in rows_a}

    # Once he is placed, his row carries the substitute flag.
    placed = client.put(
        _url(w),
        json={
            "innings_number": 1,
            "wicketkeeper_player_id": w["xi"][0],
            "assignments": [
                {"player_id": a["player_id"], "position_key": a["position_key"]}
                for a in resp.json()["assignments"]
            ]
            + [{"player_id": w["reserves"][0], "position_key": "point"}],
        },
    )
    assert placed.status_code == 200, placed.text
    rows_b = asyncio.run(assignments_rows())
    sub_rows = [a for a in rows_b if a.is_substitute]
    assert [a.player_id for a in sub_rows] == [w["reserves"][0]]
    assert len(rows_b) == 11


# --- 16: nothing else broke --------------------------------------------------


def test_scoring_and_results_untouched_by_field_and_substitutions(client, world, as_admin):
    """Moving fielders and substituting never touches a ball or a result."""
    w = world
    _use_admin(as_admin, w)
    _put_full_field(client, w)

    async def match_row():
        async with async_session() as db:
            return (
                (await db.execute(select(Match).where(Match.id == w["match_id"]))).scalars().one()
            )

    before = asyncio.run(match_row())
    result_before, status_before = before.result, before.status

    client.post(
        _url(w, "/substitution"),
        json={
            "innings_number": 1,
            "outgoing_player_id": w["xi"][4],
            "incoming_player_id": w["reserves"][0],
            "reason": "injury",
            "position_key": "cover",
        },
    )

    # Score a ball: the existing scoring path still works.
    delivery = client.post(
        f"/v1/matches/{w['match_id']}/deliveries",
        json={
            "striker_id": w["batting_squad"][0],
            "non_striker_id": w["batting_squad"][1],
            "bowler_id": w["xi"][1],
            "runs_batsman": 1,
            "runs_extras": 0,
            "extra_type": "none",
        },
    )
    assert delivery.status_code == 201, delivery.text

    after = asyncio.run(match_row())
    # The field panel never writes to the match row.
    assert after.result == result_before
    assert after.status == status_before

    # The bowler of record is surfaced so the XI list can mark (Bowler).
    body = client.get(_url(w)).json()
    assert body["current_bowler_player_id"] == w["xi"][1]


def test_substitution_write_requires_admin(client, world):
    """Same permission model as every other scoring write."""
    w = world

    async def make_owner():
        async with async_session() as db:
            owner = User(
                email="sub_scorer@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Sub Scorer",
                role="user",
                is_active=True,
                is_verified=True,
            )
            db.add(owner)
            await db.flush()
            match = Match(
                match_date=date(2026, 6, 3),
                match_time="19:00",
                venue="Sub Ground",
                match_type="T20",
                result=None,
                team_a_id=w["team_a_id"],
                team_b_id=w["team_b_id"],
                toss_winner_id=w["team_a_id"],
                toss_decision="bat",
                user_id=owner.id,
            )
            db.add(match)
            await db.flush()
            db.add(
                Innings(
                    match_id=match.id,
                    batting_team_id=w["team_a_id"],
                    bowling_team_id=w["team_b_id"],
                    innings_number=1,
                )
            )
            await db.commit()
            return owner, match.id

    scorer, match_id = asyncio.run(make_owner())
    app.dependency_overrides[get_current_user] = lambda: scorer
    try:
        url = f"/v1/matches/{match_id}/fielding-setup/substitution"
        resp = client.post(
            url,
            json={
                "innings_number": 1,
                "outgoing_player_id": w["xi"][0],
                "incoming_player_id": w["reserves"][0],
                "reason": "injury",
            },
        )
        assert resp.status_code == 403
        # And the read still works for the owner.
        assert client.get(f"/v1/matches/{match_id}/fielding-setup").status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
