"""Who the "Add Player to Squad" picker may offer, and who it may not.

``GET /teams/{team_id}/available-players`` is the list the add forms on the
team page are built from, so it has to agree with the squad the page is
showing. Two rules, and the first one is the one that broke:

1. a player this team has already added is never offered back to it - not on
   the plain list, and not through a search for their name;
2. a player held by another team at the same level is not offered either,
   because a player does not turn out for two teams in one competition.

Rule 1 used to be implied by rule 2 alone: the query excluded anyone whose
assignment carried this team's ``level_id``. When the assignment's level
disagrees with its team's level - an older write, or a client that sent its
own level id in the assign body - the squad member slipped straight past that
test and reappeared in the picker, offering the user a player the squad
already contains. The exclusion now names this ``team_id`` outright as well as
the level, so membership hides the player whatever level the row claims.
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
from app.teams.models import PlayerTeamAssignment

ADMIN_EMAIL = "available_players_admin@test.com"

# Mobile numbers are de-duplicated after their non-digits are stripped, so two
# prefixes differing only by a letter collide. One counter for the module
# hands out numbers in sequence and keeps every test apart whatever order they
# run in.
_MOBILE_SEQ = itertools.count(1)


def _next_mobile() -> str:
    return f"97{next(_MOBILE_SEQ):08d}"


# --- fixtures, following the pattern the rest of the suite uses -------------


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
                full_name="Available Players Admin",
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


def _team(client, refs, name, level_id=None):
    _, country_id, state_id, city_id = refs
    r = client.post(
        "/v1/teams",
        json={
            "name": name,
            "short_name": name[:10],
            "level_id": level_id if level_id is not None else refs[0],
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
            "email": f"{first_name.lower()}@available.test",
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


def _add(client, team_id, player_id, role="playing_11", level_id=None):
    payload = {"team_id": team_id, "role": role}
    if level_id is not None:
        payload["level_id"] = level_id
    else:
        # Read the team's own level the way the team page does, so the row is
        # written exactly as the UI would write it.
        payload["level_id"] = client.get(f"/v1/teams/{team_id}").json()["level_id"]
    return client.post(f"/v1/players/{player_id}/teams", json=payload)


def _available(client, team_id, q=None):
    path = f"/v1/teams/{team_id}/available-players?skip=0&limit=500"
    if q:
        path += f"&q={q}"
    r = client.get(path)
    assert r.status_code == 200, r.text
    return [p["id"] for p in r.json()]


# --- 1. a player the squad already has --------------------------------------


def test_a_player_already_added_is_never_offered_back(client, refs, admin, as_user):
    """The reported bug: add a player, and they stay in the picker."""
    as_user(admin)
    team = _team(client, refs, "AvailOwn")
    player = _player(client, refs, "AvailOwnPlayer")

    assert player["id"] in _available(client, team["id"])

    added = _add(client, team["id"], player["id"])
    assert added.status_code == 201, added.text

    # Read again straight away - no reload, no cache to expire - and the
    # player the squad now holds must not be in the list.
    assert player["id"] not in _available(client, team["id"])

    # And it must not come back through a search for their name either, since
    # the search box is a second way into the same picker.
    assert player["id"] not in _available(client, team["id"], q="AvailOwnPlayer")

    # The squad itself, of course, still has them.
    detail = client.get(f"/v1/teams/{team['id']}").json()
    squad = [p["id"] for p in detail["playing_11"] + detail["substitutes"] + detail["bench"]]
    assert player["id"] in squad


def test_removing_from_the_squad_offers_them_again(client, refs, admin, as_user):
    """The list has to move in both directions, or a player can never return."""
    as_user(admin)
    team = _team(client, refs, "AvailReturn")
    player = _player(client, refs, "AvailReturnPlayer")

    assert _add(client, team["id"], player["id"]).status_code == 201
    assert player["id"] not in _available(client, team["id"])

    removed = client.delete(f"/v1/players/{player['id']}/teams/{team['id']}")
    assert removed.status_code in (200, 204), removed.text
    assert player["id"] in _available(client, team["id"])


def test_an_assignment_whose_level_disagrees_still_hides_the_player(client, refs, admin, as_user):
    """The regression behind the bug: ``level_id`` alone was doing the hiding.

    The row is written directly with a level that is not its team's, which is
    what a client sending its own level id used to produce. Membership is
    membership, so the player must still be absent from the picker.
    """
    as_user(admin)
    team = _team(client, refs, "AvailOddLevel")
    player = _player(client, refs, "AvailOddLevelPlayer")

    async def odd_level_id():
        async with async_session() as db:
            level = TeamLevel(name=f"Odd Level {next(_MOBILE_SEQ)}")
            db.add(level)
            await db.commit()
            await db.refresh(level)
            return level.id

    wrong_level = asyncio.run(odd_level_id())
    assert wrong_level != refs[0]

    async def write_assignment():
        async with async_session() as db:
            db.add(
                PlayerTeamAssignment(
                    player_id=player["id"],
                    team_id=team["id"],
                    level_id=wrong_level,
                    role="playing_11",
                )
            )
            await db.commit()

    asyncio.run(write_assignment())

    assert player["id"] not in _available(client, team["id"])
    assert player["id"] not in _available(client, team["id"], q="AvailOddLevelPlayer")


def test_a_player_held_by_another_team_at_this_level_is_not_offered(client, refs, admin, as_user):
    """The level rule still stands: one competition, one team."""
    as_user(admin)
    team_a = _team(client, refs, "AvailLevelA")
    team_b = _team(client, refs, "AvailLevelB")
    player = _player(client, refs, "AvailLevelPlayer")

    assert _add(client, team_a["id"], player["id"]).status_code == 201

    assert player["id"] not in _available(client, team_a["id"])
    assert player["id"] not in _available(client, team_b["id"])


def test_an_unadded_player_is_still_offered(client, refs, admin, as_user):
    """Guard against the fix simply emptying the picker."""
    as_user(admin)
    team = _team(client, refs, "AvailStill")
    player = _player(client, refs, "AvailStillPlayer")

    assert player["id"] in _available(client, team["id"])
    assert player["id"] in _available(client, team["id"], q="AvailStillPlayer")
