"""Playing XI selection: the picker decides who takes the field, and how.

The squad buckets (``playing_11`` / ``substitute`` / ``bench``) already said
*whether* a player plays, and were capped at eleven. What they never said is
*what a picked player does*, which is the other half of a cricket side: one
wicketkeeper, one bowler and nine other fielders. That decision is recorded
on ``player_team_assignments.xi_role`` (migration ``f7a1c3e5b9d2``) and
written only through ``PUT /teams/{team_id}/playing-xi``.

Why a single PUT rather than eleven single-player edits: composition is a
property of all eleven together. No ordering of individual moves can tell
you the side has two keepers until the second one lands, by which point
half a XI has already been rewritten. So the whole XI arrives as one
request, is validated as one unit, and is written in one transaction - a
rejected pick leaves the stored XI exactly as it was.

What is enforced here, and why:

* exactly 11 players - a side fields eleven, and a request that claims to
  be the XI must actually contain one;
* exactly 1 wicketkeeper, exactly 1 bowler, the remaining 9 fielders - the
  composition the field panel and the scoring screen assume;
* every pick already on this team's squad - being picked into an XI is not
  how a player joins a club;
* no player twice - a duplicate would silently shrink the XI to ten while
  claiming eleven;
* admin only, because naming the side is a match-official decision, the
  same as every other scoring write.

Players left out of a re-pick drop to ``substitute`` with their field job
cleared, so the stored XI is always exactly what was last sent - there is
no state where the XI holds twelve, or holds a keeper who was not picked.
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

ADMIN_EMAIL = "xi_select_admin@test.com"
XI_SIZE = 11

# One sequence per file, so two players can never share a number: the API
# de-duplicates players by phone and normalises it first, and two numbers
# differing only in a letter would collide after normalisation.
_MOBILE_SEQ = itertools.count(1)


def _next_mobile() -> str:
    return f"99{next(_MOBILE_SEQ):08d}"


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
                full_name="XI Admin",
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


def _team(client, refs, name):
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


def _player(client, refs, first_name):
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
            "email": f"{first_name.lower()}@xiselect.test",
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


def _squad(client, refs, name, extras=4):
    """A team whose squad holds ``XI_SIZE`` players plus `extras` substitutes.

    Returns ``(team, xi_ids, substitute_ids)``. The XI is assembled through
    the bulk endpoint the way squads are built today, so it carries no field
    jobs yet - exactly the state the picker exists to fix.
    """
    team = _team(client, refs, name)
    xi = []
    for i in range(XI_SIZE):
        p = _player(client, refs, f"{name}XI{i}")
        xi.append(p["id"])
    bulk = client.post(
        f"/v1/teams/{team['id']}/players/bulk",
        json={"player_ids": xi, "role": "playing_11"},
    )
    assert bulk.status_code == 200, bulk.text

    subs = []
    for i in range(extras):
        p = _player(client, refs, f"{name}Sub{i}")
        subs.append(p["id"])
    if subs:
        bulk = client.post(
            f"/v1/teams/{team['id']}/players/bulk",
            json={"player_ids": subs, "role": "substitute"},
        )
        assert bulk.status_code == 200, bulk.text

    return team, xi, subs


def _pick(client, team_id, entries):
    """PUT `entries` - ``(player_id, xi_role)`` pairs - as the team's XI."""
    return client.put(
        f"/v1/teams/{team_id}/playing-xi",
        json={"players": [{"player_id": pid, "xi_role": role} for pid, role in entries]},
    )


def _standard_xi(ids):
    """`ids` as a legal XI: the first keeps wicket, the second bowls, rest field."""
    entries = [(ids[0], "wicketkeeper"), (ids[1], "bowler")]
    entries += [(pid, "fielder") for pid in ids[2:]]
    return entries


def _mk_user(email, name, role):
    async def create():
        async with async_session() as db:
            user = User(
                email=email,
                hashed_password=hash_password("Test@1234"),
                full_name=name,
                role=role,
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return asyncio.run(create())


# --- 1. the pick itself ------------------------------------------------------


def test_the_picked_eleven_replace_the_old_xi_and_record_each_job(client, refs, admin, as_user):
    as_user(admin)
    team, xi, subs = _squad(client, refs, "XIPick")

    # Swap the two lowest XI members for two substitutes: nine of the old XI
    # plus two promoted, so the response must show both directions at once.
    picked = xi[:9] + subs[:2]
    r = _pick(client, team["id"], _standard_xi(picked))
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["team_id"] == team["id"]
    assert len(body["playing_11"]) == XI_SIZE
    assert {p["id"] for p in body["playing_11"]} == set(picked)

    # Composition is exactly what was enforced: one keeper, one bowler, nine.
    roles = [p["xi_role"] for p in body["playing_11"]]
    assert roles.count("wicketkeeper") == 1
    assert roles.count("bowler") == 1
    assert roles.count("fielder") == 9
    keeper = next(p for p in body["playing_11"] if p["id"] == picked[0])
    bowler = next(p for p in body["playing_11"] if p["id"] == picked[1])
    assert keeper["xi_role"] == "wicketkeeper"
    assert bowler["xi_role"] == "bowler"

    # The two dropped members became substitutes, and a substitute carries
    # no field job - the job belongs to the XI alone.
    demoted = [p for p in body["substitutes"] if p["id"] in xi[9:]]
    assert len(demoted) == 2
    assert all(p["role"] == "substitute" for p in demoted)
    assert all(p["xi_role"] is None for p in demoted)

    # The promoted substitutes are no longer substitutes.
    promoted = {p["id"] for p in body["playing_11"]} & set(subs)
    assert promoted == set(subs[:2])
    assert not any(p["id"] in promoted for p in body["substitutes"])


def test_the_squad_and_team_reads_carry_the_jobs(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XIRead")
    assert _pick(client, team["id"], _standard_xi(xi)).status_code == 200

    squad = client.get(f"/v1/teams/{team['id']}/squad").json()
    assert len(squad["playing_11"]) == XI_SIZE
    assert [p["xi_role"] for p in squad["playing_11"]].count("wicketkeeper") == 1
    assert [p["xi_role"] for p in squad["playing_11"]].count("bowler") == 1

    detail = client.get(f"/v1/teams/{team['id']}").json()
    assert len(detail["playing_11"]) == XI_SIZE
    jobs = {p["xi_role"] for p in detail["playing_11"]}
    assert jobs == {"wicketkeeper", "bowler", "fielder"}


# --- 2. composition is refused, not repaired --------------------------------


def test_a_pick_that_is_not_eleven_players_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team, xi, subs = _squad(client, refs, "XICount")
    before = client.get(f"/v1/teams/{team['id']}/squad").json()

    r = _pick(client, team["id"], _standard_xi(xi[:9] + subs[:1]))
    assert r.status_code == 400, r.text
    assert "exactly 11" in r.json()["detail"]

    r = _pick(client, team["id"], _standard_xi(xi + subs[:1]))
    assert r.status_code == 400, r.text
    assert "exactly 11" in r.json()["detail"]

    # Nothing was written: a rejected pick leaves the XI exactly as it was.
    after = client.get(f"/v1/teams/{team['id']}/squad").json()
    assert after == before


def test_a_second_wicketkeeper_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XITwoKeepers")

    entries = [(xi[0], "wicketkeeper"), (xi[1], "wicketkeeper")]
    entries += [(pid, "fielder") for pid in xi[2:]]
    r = _pick(client, team["id"], entries)
    assert r.status_code == 400, r.text
    assert "wicketkeeper" in r.json()["detail"]


def test_a_missing_bowler_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XINoBowler")

    # Eleven players, one keeper, nobody bowling: looks fine, is not.
    entries = [(xi[0], "wicketkeeper")]
    entries += [(pid, "fielder") for pid in xi[1:]]
    r = _pick(client, team["id"], entries)
    assert r.status_code == 400, r.text
    assert "bowler" in r.json()["detail"]


def test_two_bowlers_are_refused_as_well(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XITwoBowlers")

    entries = [(xi[0], "wicketkeeper"), (xi[1], "bowler"), (xi[2], "bowler")]
    entries += [(pid, "fielder") for pid in xi[3:]]
    r = _pick(client, team["id"], entries)
    assert r.status_code == 400, r.text
    assert "bowler" in r.json()["detail"]


def test_the_same_player_cannot_fill_two_slots(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XIDup")

    # Eleven entries with a legal role split, but only ten distinct players:
    # the XI would silently be one short if this were allowed.
    entries = _standard_xi(xi[:10])
    entries = entries + [(xi[0], "fielder")]
    r = _pick(client, team["id"], entries)
    assert r.status_code == 400, r.text
    assert "twice" in r.json()["detail"]


def test_a_player_outside_the_squad_is_refused(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XIStranger")
    stranger = _player(client, refs, "XIStrangerNobody")

    entries = _standard_xi(xi)[:-1] + [(stranger["id"], "fielder")]
    r = _pick(client, team["id"], entries)
    assert r.status_code == 400, r.text
    assert "not on this team's squad" in r.json()["detail"]

    squad = client.get(f"/v1/teams/{team['id']}/squad").json()
    assert len(squad["playing_11"]) == XI_SIZE
    assert stranger["id"] not in {p["id"] for p in squad["playing_11"]}


# --- 3. who may pick ---------------------------------------------------------


def test_picking_the_xi_requires_admin(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XIPerm")

    as_user(_mk_user("xi_member@test.com", "XI Member", "user"))
    r = _pick(client, team["id"], _standard_xi(xi))
    assert r.status_code == 403, r.text


def test_picking_another_accounts_team_is_404(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XIForeign")

    as_user(_mk_user("xi_stranger@test.com", "XI Stranger", "admin"))
    r = _pick(client, team["id"], _standard_xi(xi))
    assert r.status_code == 404, r.text


# --- 4. the XI and the rest of the squad ------------------------------------


def test_leaving_the_xi_clears_the_field_job(client, refs, admin, as_user):
    as_user(admin)
    team, xi, _ = _squad(client, refs, "XILifecycle")
    assert _pick(client, team["id"], _standard_xi(xi)).status_code == 200

    # An ordinary role change out of the XI drops the job with it: a
    # substitute has no field job.
    moved = client.patch(
        f"/v1/players/{xi[1]}/teams/{team['id']}",
        json={"role": "substitute"},
    )
    assert moved.status_code == 200, moved.text

    squad = client.get(f"/v1/teams/{team['id']}/squad").json()
    bowler = next(p for p in squad["substitutes"] if p["id"] == xi[1])
    assert bowler["xi_role"] is None

    # Back into the XI the ordinary way: plays, but with no recorded job -
    # only the picker knows whether someone was picked to keep, bowl or field.
    moved = client.patch(
        f"/v1/players/{xi[1]}/teams/{team['id']}",
        json={"role": "playing_11"},
    )
    assert moved.status_code == 200, moved.text
    squad = client.get(f"/v1/teams/{team['id']}/squad").json()
    restored = next(p for p in squad["playing_11"] if p["id"] == xi[1])
    assert restored["xi_role"] is None


def test_repicking_is_a_full_replace_not_an_append(client, refs, admin, as_user):
    as_user(admin)
    team, xi, subs = _squad(client, refs, "XIReplace")

    first = xi[:9] + subs[:2]
    assert _pick(client, team["id"], _standard_xi(first)).status_code == 200

    # A completely different eleven: whoever was in the first pick and is not
    # in this one is back among the substitutes, with no job.
    second = xi[3:9] + subs[1:3] + [xi[9], xi[10], subs[3]]
    assert len(set(second)) == XI_SIZE
    assert _pick(client, team["id"], _standard_xi(second)).status_code == 200

    squad = client.get(f"/v1/teams/{team['id']}/squad").json()
    assert {p["id"] for p in squad["playing_11"]} == set(second)
    leftover = [p for p in squad["substitutes"] if p["id"] == first[1]]
    assert len(leftover) == 1
    assert leftover[0]["xi_role"] is None
