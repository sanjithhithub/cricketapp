"""Captain management: reading it, filling it, and keeping it consistent.

Captaincy already existed before this file: `is_captain` / `is_vice_captain`
flags on `player_team_assignments` (migration ``c4d5e6f7a8b9``), written by
``PUT /teams/{team_id}/captains``. Those flags were kept rather than adding a
``teams.captain_id`` column, because the assignment row *is* the proof that the
captain plays for that team - a column pointing at ``players`` could only
enforce "this player exists", never "this player belongs to us", and would have
been a second copy of a fact the squad already holds.

What was missing was the read side and the guarantees around it:

1. ``GET /teams/{team_id}/captain`` - who leads the side, with ``null`` (not an
   error) for a team that has named nobody.
2. ``GET /teams/{team_id}/captain/eligible-players`` - this team's own XI, so a
   picker physically cannot offer a player from another club.
3. Re-saving an unchanged captaincy must persist; the write clears every flag
   before setting, so a player already flagged is the one case where an ORM
   write can be silently skipped.
4. Lifecycle: leaving the squad or being deleted drops the title, and a
   captaincy change never touches a match.
"""

import asyncio
import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

ADMIN_EMAIL = "captain_mgmt_admin@test.com"
XI_SIZE = 11

# One place that mints mobile numbers, because the API de-duplicates players by
# phone and normalises the number first - two prefixes that differ only by a
# letter ("961A0000" and "961H0000") normalise to the same digits and the second
# player is refused as a duplicate of the first. Numbers are handed out in
# sequence so no two tests in this file can ever collide, whatever order they
# run in.
_MOBILE_SEQ = itertools.count(1)


def _next_mobile() -> str:
    return f"98{next(_MOBILE_SEQ):08d}"


# --- fixtures, following the pattern the rest of the suite uses --------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def refs(client):
    async def fetch():
        async with async_session() as db:
            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()
            return level.id, country.id, state.id, city.id

    return asyncio.run(fetch())


@pytest.fixture(scope="module")
def admin(client):
    async def setup():
        async with async_session() as db:
            user = User(
                email=ADMIN_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Captain Admin",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return asyncio.run(setup())


@pytest.fixture
def as_user():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


# --- builders ----------------------------------------------------------------


def _team(client, as_user, refs, name):
    level_id, country_id, state_id, city_id = refs
    r = client.post(
        "/v1/teams",
        json={
            "name": name,
            "short_name": name[:10],
            "level_id": level_id,
            "homeground": "Home",
            "founder": "Founder",
            "founded_year": 2001,
            "owner": "Owner",
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _player(client, as_user, refs, first_name):
    _, country_id, state_id, city_id = refs
    r = client.post(
        "/v1/players",
        json={
            "first_name": first_name,
            "last_name": "Player",
            "country_code": "+91",
            "mobile_number": _next_mobile(),
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
            "email": f"{first_name.lower()}@capmgmt.test",
            "date_of_birth": "1995-05-20",
            "gender": "male",
            "batting_hand": "Right",
            "batting_position": "Opening",
            "bowling_hand": "Right",
            "bowling_type": "Medium Fast",
            "height": 178.0,
            "weight": 72.0,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _xi(client, as_user, refs, team_name, extras=0):
    """A team with a full playing XI, plus `extras` substitutes.

    Returns ``(team, player_ids_in_xi_order, substitute_ids)``. The ids are
    what every test below keys on: two players can share a name, so a name is
    never the handle.
    """
    team = _team(client, as_user, refs, team_name)
    xi = []
    for i in range(XI_SIZE):
        p = _player(client, as_user, refs, f"{team_name}XI{i}")
        xi.append(p["id"])
    bulk = client.post(
        f"/v1/teams/{team['id']}/players/bulk",
        json={"player_ids": xi, "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text

    subs = []
    for i in range(extras):
        p = _player(client, as_user, refs, f"{team_name}Sub{i}")
        subs.append(p["id"])
    if subs:
        bulk = client.post(
            f"/v1/teams/{team['id']}/players/bulk",
            json={"player_ids": subs, "role": "substitute"},
        )
        assert bulk.status_code == 200, bulk.text

    return team, xi, subs


def _name_captains(client, team_id, captain_id, vice_id=None):
    return client.put(
        f"/v1/teams/{team_id}/captains",
        json={
            "captain_player_id": captain_id,
            "vice_captain_player_id": vice_id,
        },
    )


# --- 1. reading the current captain -----------------------------------------


def test_read_captain_returns_squad_rows(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapReadA")

    unset = client.get(f"/v1/teams/{team['id']}/captain")
    assert unset.status_code == 200, unset.text
    assert unset.json() == {"team_id": team["id"], "captain": None, "vice_captain": None}

    named = _name_captains(client, team["id"], xi[0], xi[1])
    assert named.status_code == 200, named.text

    r = client.get(f"/v1/teams/{team['id']}/captain")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team_id"] == team["id"]
    # The row, not just the id: a badge needs the name and the flag in one read.
    assert body["captain"]["id"] == xi[0]
    assert body["captain"]["is_captain"] is True
    assert body["captain"]["first_name"] == "CapReadAXI0"
    assert body["vice_captain"]["id"] == xi[1]
    assert body["vice_captain"]["is_vice_captain"] is True


def test_read_captain_is_200_with_nulls_when_unset(client, refs, admin, as_user):
    """A team with no captain is a valid team, not a failure to look up."""
    as_user(admin)
    team, _, _ = _xi(client, as_user, refs, "CapReadB")

    r = client.get(f"/v1/teams/{team['id']}/captain")
    assert r.status_code == 200, r.text
    assert r.json()["captain"] is None
    assert r.json()["vice_captain"] is None

    # The team detail itself still reads normally.
    detail = client.get(f"/v1/teams/{team['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["total"] == XI_SIZE


def test_read_captain_unknown_team_is_404(client, refs, admin, as_user):
    as_user(admin)
    assert client.get("/v1/teams/999999/captain").status_code == 404
    assert client.get("/v1/teams/999999/captain/eligible-players").status_code == 404


def test_read_captain_is_scoped_to_the_owning_account(client, refs, admin, as_user):
    """Another account's team is a 404, never a leaked squad."""
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapReadC")
    assert _name_captains(client, team["id"], xi[0]).status_code == 200

    async def stranger():
        async with async_session() as db:
            user = User(
                email="captain_stranger@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Captain Stranger",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    other = asyncio.run(stranger())
    as_user(other)
    assert client.get(f"/v1/teams/{team['id']}/captain").status_code == 404
    assert client.get(f"/v1/teams/{team['id']}/captain/eligible-players").status_code == 404


def test_captain_reads_require_authentication(client, refs, admin, as_user):
    as_user(admin)
    team, _, _ = _xi(client, as_user, refs, "CapReadD")
    # Drop the override so the real auth dependency runs.
    app.dependency_overrides.pop(get_current_user, None)

    assert client.get(f"/v1/teams/{team['id']}/captain").status_code == 401
    assert client.get(f"/v1/teams/{team['id']}/captain/eligible-players").status_code == 401


# --- 2. eligible players -----------------------------------------------------


def test_eligible_players_are_only_this_teams_own_xi(client, refs, admin, as_user):
    """The dropdown must never be able to show another club's players."""
    as_user(admin)
    team_a, xi_a, _ = _xi(client, as_user, refs, "CapEligA")
    team_b, xi_b, _ = _xi(client, as_user, refs, "CapEligB")

    r = client.get(f"/v1/teams/{team_a['id']}/captain/eligible-players")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team_id"] == team_a["id"]

    returned = [p["id"] for p in body["players"]]
    assert sorted(returned) == sorted(xi_a)
    assert not set(returned) & set(xi_b), "a player from the other team leaked in"
    for p in body["players"]:
        assert p["role"] == "playing_11"


def test_eligible_players_exclude_substitutes_and_bench(client, refs, admin, as_user):
    """Only the XI can be named, so offering a substitute is a guaranteed 400."""
    as_user(admin)
    team, _, subs = _xi(client, as_user, refs, "CapEligC", extras=2)
    assert subs

    r = client.get(f"/v1/teams/{team['id']}/captain/eligible-players")
    assert r.status_code == 200, r.text
    returned = [p["id"] for p in r.json()["players"]]
    assert not set(returned) & set(subs)
    assert len(returned) == XI_SIZE


def test_eligible_players_empty_is_200_not_an_error(client, refs, admin, as_user):
    """A squad with no XI yet has nothing eligible - that is a real answer."""
    as_user(admin)
    team = _team(client, as_user, refs, "CapEligEmpty")

    r = client.get(f"/v1/teams/{team['id']}/captain/eligible-players")
    assert r.status_code == 200, r.text
    assert r.json()["players"] == []


# --- 3. assigning, changing and removing ------------------------------------


def test_assign_change_and_remove_captain(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapWriteA")

    assigned = _name_captains(client, team["id"], xi[0], xi[1])
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["captain_player_id"] == xi[0]
    assert assigned.json()["vice_captain_player_id"] == xi[1]

    # Change: exactly one captain at a time, and the previous one is cleared.
    changed = _name_captains(client, team["id"], xi[2], xi[3])
    assert changed.status_code == 200, changed.text
    captain = client.get(f"/v1/teams/{team['id']}/captain").json()
    assert captain["captain"]["id"] == xi[2]
    assert captain["vice_captain"]["id"] == xi[3]

    squad = client.get(f"/v1/teams/{team['id']}").json()
    flagged = [p["id"] for p in squad["playing_11"] if p["is_captain"]]
    assert flagged == [xi[2]], "the old captain kept the flag"

    # Remove: both titles clear, and clearing is a 200.
    cleared = _name_captains(client, team["id"], None, None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["captain_player_id"] is None
    after = client.get(f"/v1/teams/{team['id']}/captain").json()
    assert after["captain"] is None and after["vice_captain"] is None


def test_resaving_the_same_captain_persists(client, refs, admin, as_user):
    """The regression the clear-then-set write is most likely to produce.

    The helper resets every flag on the team and then re-raises the two it was
    given. For a player who was *already* captain the in-memory value never
    changes, so an ORM that relies on dirty-tracking alone would skip the write
    and the bulk reset would win - the team would answer with no captain after a
    save that reported success.
    """
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapResave")

    first = _name_captains(client, team["id"], xi[0], xi[1])
    assert first.status_code == 200, first.text
    assert client.get(f"/v1/teams/{team['id']}/captain").json()["captain"]["id"] == xi[0]

    again = _name_captains(client, team["id"], xi[0], xi[1])
    assert again.status_code == 200, again.text

    read = client.get(f"/v1/teams/{team['id']}/captain").json()
    assert read["captain"]["id"] == xi[0], "re-saving the same captain lost the title"
    assert read["vice_captain"]["id"] == xi[1]

    # Same thing with only one title unchanged.
    keep_captain = _name_captains(client, team["id"], xi[0], None)
    assert keep_captain.status_code == 200, keep_captain.text
    read = client.get(f"/v1/teams/{team['id']}/captain").json()
    assert read["captain"]["id"] == xi[0]
    assert read["vice_captain"] is None


def test_assigning_a_player_from_another_team_is_400(client, refs, admin, as_user):
    as_user(admin)
    team_a, _, _ = _xi(client, as_user, refs, "CapForeignA")
    team_b, xi_b, _ = _xi(client, as_user, refs, "CapForeignB")

    r = _name_captains(client, team_a["id"], xi_b[0])
    assert r.status_code == 400, r.text
    assert "not on this team's squad" in r.json()["detail"]

    # Nothing was written by the rejected request.
    assert client.get(f"/v1/teams/{team_a['id']}/captain").json()["captain"] is None


def test_assigning_a_nonexistent_player_is_400(client, refs, admin, as_user):
    as_user(admin)
    team, _, _ = _xi(client, as_user, refs, "CapMissing")

    r = _name_captains(client, team["id"], 999999)
    assert r.status_code == 400, r.text
    assert "not on this team's squad" in r.json()["detail"]
    assert client.get(f"/v1/teams/{team['id']}/captain").json()["captain"] is None


def test_the_same_player_cannot_hold_both_titles(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapBoth")

    r = _name_captains(client, team["id"], xi[0], xi[0])
    assert r.status_code == 400, r.text
    assert "two different players" in r.json()["detail"]
    assert client.get(f"/v1/teams/{team['id']}/captain").json()["captain"] is None


def test_a_substitute_cannot_be_named_captain(client, refs, admin, as_user):
    as_user(admin)
    team, xi, subs = _xi(client, as_user, refs, "CapSub", extras=1)

    r = _name_captains(client, team["id"], subs[0])
    assert r.status_code == 400, r.text
    assert "must both be in the XI" in r.json()["detail"]
    assert client.get(f"/v1/teams/{team['id']}/captain").json()["captain"] is None


def test_naming_a_captain_requires_admin(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapPerm")

    async def member():
        async with async_session() as db:
            user = User(
                email="captain_member@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Captain Member",
                role="user",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    as_user(asyncio.run(member()))
    r = _name_captains(client, team["id"], xi[0])
    assert r.status_code == 403, r.text
    # Reads stay open to any signed-in account, exactly like the squad read.
    assert client.get(f"/v1/teams/{team['id']}/captain").status_code in (200, 404)


# --- 4. player and team lifecycle -------------------------------------------


def test_removing_the_captain_from_the_squad_clears_the_title(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapLifecycleA")
    assert _name_captains(client, team["id"], xi[0], xi[1]).status_code == 200

    removed = client.delete(f"/v1/players/{xi[0]}/teams/{team['id']}")
    assert removed.status_code in (200, 204), removed.text

    read = client.get(f"/v1/teams/{team['id']}/captain").json()
    # The assignment row carried the flag, so removing the row removes the title.
    assert read["captain"] is None
    assert read["vice_captain"]["id"] == xi[1]


def test_deleting_the_captain_player_clears_the_title(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapLifecycleB")
    assert _name_captains(client, team["id"], xi[0]).status_code == 200

    deleted = client.delete(f"/v1/players/{xi[0]}")
    assert deleted.status_code == 204, deleted.text

    read = client.get(f"/v1/teams/{team['id']}/captain").json()
    assert read["captain"] is None

    # The team itself survives, and the surviving player's profile is untouched.
    detail = client.get(f"/v1/teams/{team['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["total"] == XI_SIZE - 1
    survivor = client.get(f"/v1/players/{xi[1]}")
    assert survivor.status_code == 200
    assert survivor.json()["first_name"] == "CapLifecycleBXI1"


def test_moving_the_captain_off_the_team_leaves_no_dangling_title(client, refs, admin, as_user):
    """The closest thing this app has to a transfer: off one squad, onto another.

    There is no endpoint that repoints an assignment at a different team, so a
    move is a removal followed by an add. The removal is what must drop the
    title - the old side is never allowed to keep naming a player who no longer
    plays for it, and the new side does not inherit the title either.
    """
    as_user(admin)
    team_a, xi_a, _ = _xi(client, as_user, refs, "CapMoveA")
    team_b, _, _ = _xi(client, as_user, refs, "CapMoveB")
    assert _name_captains(client, team_a["id"], xi_a[0], xi_a[1]).status_code == 200

    off = client.delete(f"/v1/players/{xi_a[0]}/teams/{team_a['id']}")
    assert off.status_code in (200, 204), off.text
    assert client.get(f"/v1/teams/{team_a['id']}/captain").json()["captain"] is None

    moved = client.post(
        f"/v1/players/{xi_a[0]}/teams",
        json={
            "team_id": team_b["id"],
            "level_id": team_b["level_id"],
            "role": "substitute",
        },
    )
    assert moved.status_code == 201, moved.text

    assert client.get(f"/v1/teams/{team_a['id']}/captain").json()["captain"] is None
    assert client.get(f"/v1/teams/{team_b['id']}/captain").json()["captain"] is None
    # The old side is still a whole, valid team with the rest of its XI.
    assert client.get(f"/v1/teams/{team_a['id']}").json()["total"] == XI_SIZE - 1


def test_a_team_with_no_captain_is_still_valid_everywhere(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapNoCaptain")

    for path in (
        f"/v1/teams/{team['id']}",
        f"/v1/teams/{team['id']}/squad",
        f"/v1/teams/{team['id']}/captain",
        f"/v1/teams/{team['id']}/captain/eligible-players",
        f"/v1/teams/{team['id']}/analytics",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"

    # And naming one after the fact still works on a team that had none.
    assert _name_captains(client, team["id"], xi[4]).status_code == 200
    assert client.get(f"/v1/teams/{team['id']}/captain").json()["captain"]["id"] == xi[4]


def test_changing_the_captain_never_touches_the_players(client, refs, admin, as_user):
    """The captaincy is a flag on a squad row, not a field on the player."""
    as_user(admin)
    team, xi, _ = _xi(client, as_user, refs, "CapProfile")

    before = [client.get(f"/v1/players/{pid}").json() for pid in xi]
    assert _name_captains(client, team["id"], xi[0], xi[1]).status_code == 200
    assert _name_captains(client, team["id"], xi[5]).status_code == 200
    assert _name_captains(client, team["id"], None, None).status_code == 200

    for pid, snapshot in zip(xi, before, strict=True):
        after = client.get(f"/v1/players/{pid}").json()
        assert after == snapshot, f"captaincy changed the profile of player {pid}"


# --- 5. match integration ----------------------------------------------------


def _ball(client, match_id, card, runs, bowler):
    r = client.post(
        f"/v1/matches/{match_id}/deliveries",
        json={
            "striker_id": card["striker_id"],
            "non_striker_id": card["non_striker_id"],
            "bowler_id": bowler,
            "runs_batsman": runs,
            "runs_extras": 0,
            "extra_type": "none",
            "extra_runs": 0,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_captaincy_changes_leave_scores_results_and_fixtures_untouched(
    client, refs, admin, as_user
):
    """Naming a captain is a squad edit; a match is not a squad.

    The whole match record and the whole scorecard are compared before and
    after a captaincy change, so any field - runs, wickets, result, status,
    fixture link - that moved would fail here rather than in production.
    """
    as_user(admin)
    team_a, xi_a, _ = _xi(client, as_user, refs, "CapMatchA")
    team_b, xi_b, _ = _xi(client, as_user, refs, "CapMatchB")
    assert _name_captains(client, team_a["id"], xi_a[0], xi_a[1]).status_code == 200
    assert _name_captains(client, team_b["id"], xi_b[0]).status_code == 200

    created = client.post(
        "/v1/matches",
        json={
            "match_date": "2026-08-03",
            "match_time": "19:00",
            "venue": "Captaincy Oval",
            "match_type": "T20",
            "team_a_id": team_a["id"],
            "team_b_id": team_b["id"],
            "toss_winner_id": team_a["id"],
            "toss_decision": "bat",
            "total_overs": 20,
        },
    )
    assert created.status_code == 201, created.text
    match_id = created.json()["id"]

    start = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": xi_a},
    )
    assert start.status_code == 200, start.text
    card = start.json()
    card = _ball(client, match_id, card, 4, bowler=xi_b[0])
    card = _ball(client, match_id, card, 2, bowler=xi_b[0])

    def snapshot():
        return {
            "match": client.get(f"/v1/matches/{match_id}").json(),
            "scorecard": client.get(f"/v1/matches/{match_id}/scorecard").json(),
            "summary": client.get(f"/v1/matches/{match_id}/summary").json(),
            "analytics": client.get(f"/v1/matches/{match_id}/analytics").json(),
        }

    before = snapshot()

    # Change the captain on one side, clear it on the other, then read back.
    assert _name_captains(client, team_a["id"], xi_a[7], xi_a[8]).status_code == 200
    assert _name_captains(client, team_b["id"], None, None).status_code == 200

    after = snapshot()
    assert after["match"] == before["match"], "the match record moved"
    assert after["scorecard"] == before["scorecard"], "the scorecard moved"
    assert after["summary"] == before["summary"], "the result summary moved"
    assert after["analytics"] == before["analytics"], "the innings analytics moved"

    # And the new captaincy is visible on the team the match is about, so the
    # change did apply - it just applied somewhere the score does not live.
    captain = client.get(f"/v1/teams/{team_a['id']}/captain").json()
    assert captain["captain"]["id"] == xi_a[7]


def test_captain_is_not_auto_selected_into_the_opening_order(client, refs, admin, as_user):
    """Starting an innings takes exactly the order it is handed.

    The client starts from an empty selection and the server records what it
    receives, so captaincy can never leak into the opening batters by itself.
    """
    as_user(admin)
    team_a, xi_a, _ = _xi(client, as_user, refs, "CapOrderA")
    team_b, xi_b, _ = _xi(client, as_user, refs, "CapOrderB")
    assert _name_captains(client, team_a["id"], xi_a[9]).status_code == 200

    created = client.post(
        "/v1/matches",
        json={
            "match_date": "2026-08-04",
            "match_time": "10:00",
            "venue": "Order Ground",
            "match_type": "T20",
            "team_a_id": team_a["id"],
            "team_b_id": team_b["id"],
            "toss_winner_id": team_a["id"],
            "toss_decision": "bat",
            "total_overs": 20,
        },
    )
    assert created.status_code == 201, created.text
    match_id = created.json()["id"]

    openers = [xi_a[4], xi_a[5]]
    start = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": openers + [xi_a[6]]},
    )
    assert start.status_code == 200, start.text
    card = start.json()

    # The captain (player 9) is in the XI but was never named, so they are not
    # in the order and not at the crease.
    assert card["striker_id"] == openers[0]
    assert card["non_striker_id"] == openers[1]
    assert xi_a[9] not in (card["striker_id"], card["non_striker_id"])
