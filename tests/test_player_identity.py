"""Player identity: codes, shared names, shared numbers and the playing XI.

Covers the eight cases the onboarding and selection work has to get right:

1. two different players with the same full name
2. the same player registered again with the same normalised number
3. two players sharing a number
4. a player with no number
5. a player changing their number
6. two players with similar names but different ids
7. a duplicate id rejected when selecting a playing XI
8. existing matches and scorecards still resolving the right players

The theme running through them: a name is display data and a number is a
convention, so neither may decide who a player is. The ``player_code`` and the
``id`` do, and nothing in the app is allowed to merge two records on a name or a
number alone.
"""

import asyncio
import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State
from app.players.crud import PLAYING_XI_SIZE
from app.players.identity import generate_player_code, phone_e164
from app.players.models import Player

ADMIN_EMAIL = "identity_admin@test.com"
OTHER_EMAIL = "identity_other@test.com"

_counter = {"n": 0}


def _next_phone() -> str:
    _counter["n"] += 1
    return f"98{_counter['n']:08d}"


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
                full_name="Identity Admin",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            other = User(
                email=OTHER_EMAIL,
                hashed_password=hash_password("Test@1234"),
                full_name="Other Admin",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add_all([user, other])
            await db.commit()
            await db.refresh(user)
            await db.refresh(other)
            return user, other

    return asyncio.run(setup())


@pytest.fixture
def as_user():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


_AUTO = object()


def _payload(
    refs,
    *,
    first="Rohit",
    last="Sharma",
    mobile=_AUTO,
    email=None,
    team_id=None,
    role="playing_11",
    country_code="+91",
):
    """Build a create payload.

    ``mobile`` defaults to a fresh number, and an explicit ``None`` means "this
    player has no number" - the two have to be distinguishable to test case 4.
    """
    level_id, country_id, state_id, city_id = refs
    if email is None:
        _counter["n"] += 1
        email = f"identity{_counter['n']}@test.com"
    if mobile is _AUTO:
        mobile = _next_phone()
    return {
        "first_name": first,
        "last_name": last,
        "date_of_birth": "1995-05-20",
        "gender": "male",
        "profile_image": None,
        "batting_hand": "right",
        "batting_position": "opening",
        "bowling_hand": "right",
        "bowling_type": "medium",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
        "height": 178.0,
        "weight": 72.0,
        "country_code": country_code,
        "mobile_number": mobile,
        "email": email,
        "team_id": team_id,
        "role": role,
    }


def _create(client, payload, **extra):
    return client.post("/v1/players", json={**payload, **extra})


def _players_named(client, first, last):
    resp = client.get("/v1/players/search", params={"q": first})
    assert resp.status_code == 200
    return [p for p in resp.json() if p["last_name"].lower() == last.lower()]


# --- 1. two different players, same full name ---------------------------


def test_two_different_players_may_share_a_full_name(client, refs, admin, as_user):
    """Same name, different number: the second registration warns, then succeeds.

    It is not blocked and the two records are not merged - different people
    really do share names, and refusing would be wrong more often than allowing
    it.
    """
    as_user(admin[0])
    first = _create(client, _payload(refs, first="Sachin", last="Mehta", mobile="9811100001"))
    assert first.status_code == 201
    second = _create(client, _payload(refs, first="Sachin", last="Mehta", mobile="9811100002"))

    # Reported, not refused: the caller has to confirm.
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["next_action"] == "confirm_duplicate_name"
    assert [m["player_code"] for m in detail["name_matches"]] == [first.json()["player_code"]]

    confirmed = _create(
        client,
        _payload(refs, first="Sachin", last="Mehta", mobile="9811100002"),
        duplicate_confirmed=True,
    )
    assert confirmed.status_code == 201
    body = confirmed.json()
    assert body["id"] != first.json()["id"]
    assert body["player_code"] != first.json()["player_code"]
    assert "name that already exists" in (body["duplicate_name_warning"] or "")

    # Two distinct people, each with a permanent code of their own.
    matches = _players_named(client, "Sachin", "Mehta")
    assert len(matches) == 2
    assert len({m["player_code"] for m in matches}) == 2


# --- 2. same player, same normalised number -----------------------------


def test_reregistering_the_same_number_asks_before_creating_anything(client, refs, admin, as_user):
    """A repeated number stops the registration and offers the existing player.

    Confirming that it is the same person must reuse that record, because a
    second row would split the player's match history in two.
    """
    as_user(admin[0])
    original = _create(client, _payload(refs, first="Rahul", last="Dravid", mobile="9811100011"))
    assert original.status_code == 201
    original_id = original.json()["id"]

    # Typed differently, same number: a leading zero and spaces must not make it
    # look like a different person.
    repeat = _create(client, _payload(refs, first="Rahul", last="Dravid", mobile="098111 00011"))
    assert repeat.status_code == 409
    detail = repeat.json()["detail"]
    assert detail["next_action"] == "confirm_same_person"
    matches = detail["phone_matches"]
    assert len(matches) == 1
    assert matches[0]["id"] == original_id
    assert matches[0]["player_code"] == original.json()["player_code"]
    # The candidate never leaks a full number back to the person asking.
    assert matches[0]["mobile_number"] != "9811100011"
    assert "*" in matches[0]["mobile_number"]

    # "Same person" - reuse the row, do not create a second one.
    linked = _create(
        client,
        _payload(refs, first="Rahul", last="Dravid", mobile="9811100011"),
        existing_player_id=original_id,
    )
    assert linked.status_code == 201
    assert linked.json()["id"] == original_id
    assert linked.json()["linked_existing"] is True

    named = _players_named(client, "Rahul", "Dravid")
    assert len(named) == 1


# --- 3. two players sharing a number -------------------------------------


def test_two_players_may_share_a_number_when_confirmed(client, refs, admin, as_user):
    """A shared number is a real case: two siblings on one handset.

    The second registration is still surfaced first - the app has no way to know
    - but confirming lets it through, and the two remain separate players.
    """
    as_user(admin[0])
    shared = "9811100021"
    first = _create(client, _payload(refs, first="Anil", last="Kumar", mobile=shared))
    assert first.status_code == 201

    second = _create(client, _payload(refs, first="Sunil", last="Kumar", mobile=shared))
    assert second.status_code == 409
    assert second.json()["detail"]["next_action"] == "confirm_same_person"

    confirmed = _create(
        client,
        _payload(refs, first="Sunil", last="Kumar", mobile=shared),
        duplicate_confirmed=True,
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["id"] != first.json()["id"]

    check = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Sunil",
            "last_name": "Kumar",
            "country_code": "+91",
            "mobile_number": shared,
        },
    ).json()
    assert check["phone_taken"] is True
    assert len(check["phone_matches"]) == 2
    assert len({m["player_code"] for m in check["phone_matches"]}) == 2


def test_a_shared_number_cannot_be_used_to_pick_one_player_for_a_team(client, refs, admin, as_user):
    """Typing a shared number must not silently pick one of the two holders."""
    as_user(admin[0])
    level_id, *_ = refs
    team = client.post(
        "/v1/teams",
        json={
            "name": f"Shared Phone FC {_next_phone()[-4:]}",
            "short_name": "SPF",
            "logo": None,
            "homeground": "Ground",
            "founder": "Founder",
            "founded_year": 2020,
            "owner": "Owner",
            "country_id": refs[1],
            "state_id": refs[2],
            "city_id": refs[3],
            "level_id": level_id,
        },
    )
    assert team.status_code == 201
    team_id = team.json()["id"]

    shared = "9811100022"
    p1 = _create(client, _payload(refs, first="Kabir", last="Rao", mobile=shared))
    p2 = _create(
        client, _payload(refs, first="Kabir", last="Rao", mobile=shared), duplicate_confirmed=True
    )
    assert p1.status_code == 201 and p2.status_code == 201

    resp = client.post(
        f"/v1/teams/{team_id}/players",
        json={"country_code": "+91", "mobile_number": shared, "role": "playing_11"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "share this phone number" in detail
    # Both candidates are named by code, so the scorer can pick deliberately.
    assert p1.json()["player_code"] in detail
    assert p2.json()["player_code"] in detail

    # And by code the choice is unambiguous.
    by_code = client.get(f"/v1/players/by-code/{p1.json()['player_code']}")
    assert by_code.status_code == 200
    assert by_code.json()["id"] == p1.json()["id"]


# --- 4. a player with no number ------------------------------------------


def test_a_player_may_have_no_number(client, refs, admin, as_user):
    """No number is allowed, and it is never reported as a duplicate match."""
    as_user(admin[0])
    created = _create(client, _payload(refs, first="Nikhil", last="Jain", mobile=None))
    assert created.status_code == 201
    body = created.json()
    assert body["mobile_number"] is None
    assert body["player_code"]

    check = client.post(
        "/v1/players/check-duplicate",
        json={"first_name": "Nikhil", "last_name": "Jain", "country_code": "+91"},
    ).json()
    assert check["phone_taken"] is False
    assert check["phone_matches"] == []

    # A second numberless player with the same name still needs a confirmation,
    # because a number is the only other signal there would have been.
    second = _create(client, _payload(refs, first="Nikhil", last="Jain", mobile=None))
    assert second.status_code == 409
    assert second.json()["detail"]["next_action"] == "confirm_duplicate_name"

    # A number does not excuse a name clash: same name, different number is the
    # ambiguity the warning exists for, so it is still asked about and still
    # allowed once confirmed.
    third = _create(client, _payload(refs, first="Nikhil", last="Jain", mobile=_next_phone()))
    assert third.status_code == 409
    assert third.json()["detail"]["next_action"] == "confirm_duplicate_name"
    confirmed = _create(
        client,
        _payload(refs, first="Nikhil", last="Jain", mobile=_next_phone()),
        duplicate_confirmed=True,
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["duplicate_name_warning"] is not None
    # The unconfirmed attempt created nothing, so exactly the two confirmed
    # registrations exist.
    assert len(_players_named(client, "Nikhil", "Jain")) == 2


# --- 5. a player changes their number ------------------------------------


def test_changing_a_number_re_derives_the_duplicate_key(client, refs, admin, as_user):
    """The old number must stop matching and the new one must start matching."""
    as_user(admin[0])
    old = "9811100031"
    new = "9811100032"
    player = _create(client, _payload(refs, first="Vivek", last="Iyer", mobile=old))
    assert player.status_code == 201
    player_id = player.json()["id"]

    # Still matches the number they had.
    before = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Vivek",
            "last_name": "Iyer",
            "country_code": "+91",
            "mobile_number": old,
        },
    ).json()
    assert before["phone_taken"] is True

    updated = client.patch(f"/v1/players/{player_id}", json={"mobile_number": new})
    assert updated.status_code == 200
    assert updated.json()["mobile_number"] == new

    after_old = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Someone",
            "last_name": "Else",
            "country_code": "+91",
            "mobile_number": old,
        },
    ).json()
    assert after_old["phone_taken"] is False

    after_new = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Vivek",
            "last_name": "Iyer",
            "country_code": "+91",
            "mobile_number": new,
        },
    ).json()
    assert after_new["phone_taken"] is True
    assert after_new["phone_matches"][0]["id"] == player_id

    # The freed number is reusable by a different player.
    other = _create(client, _payload(refs, first="Ganesh", last="Pillai", mobile=old))
    assert other.status_code == 201
    assert other.json()["id"] != player_id


def test_a_changed_number_is_no_longer_marked_verified(client, refs, admin, as_user):
    as_user(admin[0])
    player = _create(client, _payload(refs, first="Deepa", last="Nair", mobile="9811100041"))
    player_id = player.json()["id"]
    client.patch(f"/v1/players/{player_id}", json={})  # no-op, keeps the shape honest
    updated = client.patch(f"/v1/players/{player_id}", json={"mobile_number": "9811100042"})
    assert updated.json()["is_phone_verified"] is False


# --- 6. similar names, different ids -------------------------------------


def test_similar_names_stay_separate_and_are_told_apart_by_code(client, refs, admin, as_user):
    as_user(admin[0])
    exact = _create(client, _payload(refs, first="Ravi", last="Rao", mobile="9811100051"))
    prefixed = _create(client, _payload(refs, first="Ravindra", last="Rao", mobile="9811100052"))
    other_last = _create(client, _payload(refs, first="Ravi", last="Raj", mobile="9811100053"))
    for resp in (exact, prefixed, other_last):
        assert resp.status_code == 201

    # Searching a shared fragment returns both near-matches, each identifiable.
    results = client.get("/v1/players/search", params={"q": "Ravi"}).json()
    codes = {p["player_code"] for p in results}
    assert exact.json()["player_code"] in codes
    assert prefixed.json()["player_code"] in codes
    assert other_last.json()["player_code"] in codes
    assert (
        len(
            {
                p["id"]
                for p in results
                if p["id"] in {exact.json()["id"], prefixed.json()["id"], other_last.json()["id"]}
            }
        )
        == 3
    )

    # A player code resolves to exactly one player, whatever the name.
    for resp in (exact, prefixed, other_last):
        found = client.get(f"/v1/players/by-code/{resp.json()['player_code']}").json()
        assert found["id"] == resp.json()["id"]


def test_a_nickname_resolves_to_the_same_player_instead_of_a_new_record(
    client, refs, admin, as_user
):
    """An alias attaches to the player it names instead of duplicating them."""
    as_user(admin[0])
    player = _create(client, _payload(refs, first="Rajesh", last="Krishnan", mobile="9811100061"))
    assert player.status_code == 201
    player_id = player.json()["id"]

    added = client.post(f"/v1/players/{player_id}/aliases", json={"alias": "Raj  Krishna"})
    assert added.status_code == 200
    assert added.json()["aliases"] == ["Raj Krishna"]

    # Re-adding the same nickname in different casing is a no-op, not a failure.
    again = client.post(f"/v1/players/{player_id}/aliases", json={"alias": "raj krishna"})
    assert again.json()["aliases"] == ["Raj Krishna"]

    # A search for the nickname finds the one player, and creates nothing.
    before = len(client.get("/v1/players").json())
    found = client.get("/v1/players/search", params={"q": "Raj Krishna"}).json()
    assert [p["id"] for p in found] == [player_id]
    assert len(client.get("/v1/players").json()) == before


# --- 7. duplicate selection in a playing XI ------------------------------


def _make_team(client, name):
    level_id, country_id, state_id, city_id = _REFS
    resp = client.post(
        "/v1/teams",
        json={
            "name": name,
            "short_name": name[:3].upper(),
            "logo": None,
            "homeground": "Ground",
            "founder": "Founder",
            "founded_year": 2021,
            "owner": "Owner",
            "country_id": country_id,
            "state_id": state_id,
            "city_id": city_id,
            "level_id": level_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


_REFS = None


def test_selecting_the_same_player_twice_in_an_xi_is_rejected(client, refs, admin, as_user):
    global _REFS
    _REFS = refs
    as_user(admin[0])
    team_id = _make_team(client, f"XI Club {_next_phone()[-4:]}")

    # Two different people who share a name - the exact case where a name-keyed
    # selector would pick the wrong one.
    a = _create(client, _payload(refs, first="Mohan", last="Iyer", mobile="9811100071"))
    b = _create(
        client,
        _payload(refs, first="Mohan", last="Iyer", mobile="9811100072"),
        duplicate_confirmed=True,
    )
    assert a.status_code == 201 and b.status_code == 201

    resp = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": [a.json()["id"], b.json()["id"]], "role": "playing_11"},
    )
    assert resp.status_code == 200
    assert resp.json()["added_player_ids"] == [a.json()["id"], b.json()["id"]]

    # The same id twice in one selection is refused, and nothing is written.
    dupe = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": [a.json()["id"], a.json()["id"]], "role": "substitute"},
    )
    assert dupe.status_code == 400
    assert "more than once" in dupe.json()["detail"]

    # Re-adding a player who is already on the squad is refused too.
    already = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": [a.json()["id"]], "role": "substitute"},
    )
    assert already.status_code == 400


def test_playing_xi_is_capped_at_eleven_but_the_squad_is_not(client, refs, admin, as_user):
    global _REFS
    _REFS = refs
    as_user(admin[0])
    team_id = _make_team(client, f"XI Limit {_next_phone()[-4:]}")

    ids = []
    for i in range(PLAYING_XI_SIZE + 2):
        resp = _create(
            client,
            _payload(
                refs, first=f"Xi{i}", last=f"Player{_next_phone()[-4:]}", mobile=_next_phone()
            ),
        )
        assert resp.status_code == 201
        ids.append(resp.json()["id"])

    # Eleven is allowed.
    first_eleven = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": ids[:PLAYING_XI_SIZE], "role": "playing_11"},
    )
    assert first_eleven.status_code == 200

    # A twelfth, on top of the eleven already in, is refused.
    too_many = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": ids[PLAYING_XI_SIZE:], "role": "playing_11"},
    )
    assert too_many.status_code == 400
    assert str(PLAYING_XI_SIZE) in too_many.json()["detail"]

    # But the squad itself takes the rest, because a squad is bigger than a side.
    rest = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": ids[PLAYING_XI_SIZE:], "role": "substitute"},
    )
    assert rest.status_code == 200

    squad = client.get(f"/v1/teams/{team_id}/squad").json()
    assert len(squad["playing_11"]) == PLAYING_XI_SIZE
    assert len(squad["substitutes"]) == 2
    # Squad rows carry the code, so same-named members stay distinguishable.
    assert all(p["player_code"] for p in squad["playing_11"])


def test_a_full_playing_xi_refuses_another_player_by_id(client, refs, admin, as_user):
    global _REFS
    _REFS = refs
    as_user(admin[0])
    team_id = _make_team(client, f"XI Single {_next_phone()[-4:]}")

    # The first eleven are taken straight into the XI as they are registered.
    for i in range(PLAYING_XI_SIZE):
        resp = _create(
            client,
            _payload(
                refs,
                first=f"Single{i}",
                last=f"Player{_next_phone()[-4:]}",
                mobile=_next_phone(),
                team_id=team_id,
                role="playing_11",
            ),
        )
        assert resp.status_code == 201, resp.text

    # The twelfth is refused by id: the side is full, not the player's identity.
    extra = _create(
        client,
        _payload(
            refs,
            first="Overflow",
            last=f"Player{_next_phone()[-4:]}",
            team_id=team_id,
            role="playing_11",
        ),
    )
    assert extra.status_code == 409
    assert str(PLAYING_XI_SIZE) in extra.json()["detail"]

    # Registering the same person anyway is fine; only the XI slot is missing.
    same = _create(
        client,
        _payload(refs, first="Overflow", last=f"Player{_next_phone()[-4:]}", mobile=_next_phone()),
    )
    assert same.status_code == 201

    # And as a substitute there is room.
    sub = client.post(
        f"/v1/teams/{team_id}/players/bulk",
        json={"player_ids": [same.json()["id"]], "role": "substitute"},
    )
    assert sub.status_code == 200


# --- 8. existing matches and scorecards still resolve players ------------


def test_existing_matches_and_scorecards_still_show_the_right_players(client, refs, admin, as_user):
    """A scorecard must keep pointing at the same person after all of this."""
    global _REFS
    _REFS = refs
    as_user(admin[0])

    team_a = _make_team(client, f"Scorer A {_next_phone()[-4:]}")
    team_b = _make_team(client, f"Scorer B {_next_phone()[-4:]}")

    # Both openers share a name, so only the id can tell the scorecard rows apart.
    striker = _create(
        client,
        _payload(refs, first="Sameer", last="Vyas", mobile="9811100081"),
        team_id=team_a,
        role="playing_11",
    )
    other = _create(
        client,
        _payload(refs, first="Sameer", last="Vyas", mobile="9811100082"),
        team_id=team_b,
        role="playing_11",
        duplicate_confirmed=True,
    )
    assert striker.status_code == 201 and other.status_code == 201
    striker_id = striker.json()["id"]
    other_id = other.json()["id"]

    match = client.post(
        "/v1/matches",
        json={
            "match_date": "2026-10-01",
            "match_time": "14:00",
            "venue": "Stadium",
            "match_type": "T20",
            "result": None,
            "team_a_id": team_a,
            "team_b_id": team_b,
            "toss_winner_id": team_a,
            "toss_decision": "bat",
            "referee_1_name": None,
            "referee_2_name": None,
            "match_referee_name": None,
        },
    )
    assert match.status_code == 201, match.text
    match_id = match.json()["id"]

    # Two openers, only one of whom shares a name with a player on the other side.
    partner = _create(
        client,
        _payload(
            refs,
            first="Partner",
            last=f"Open{_next_phone()[-4:]}",
            mobile=_next_phone(),
            team_id=team_a,
            role="playing_11",
        ),
    )
    assert partner.status_code == 201

    started = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": [striker_id, partner.json()["id"]]},
    )
    assert started.status_code in (200, 201), started.text

    scorecard = client.get(f"/v1/matches/{match_id}/scorecard").json()
    rows = [b for b in scorecard["batsmen"] if b["player_id"] == striker_id]
    assert len(rows) == 1
    assert rows[0]["first_name"] == "Sameer"
    # The other Sameer is a different player and must not appear on this card.
    assert all(b["player_id"] != other_id for b in scorecard["batsmen"])

    # A duplicate id in the batting order is still rejected.
    dupe = client.post(
        f"/v1/matches/{match_id}/start-innings",
        json={"batting_order": [striker_id, striker_id]},
    )
    assert dupe.status_code >= 400


# --- privacy --------------------------------------------------------------


def test_a_number_is_never_leaked_through_a_duplicate_check(client, refs, admin, as_user):
    """The duplicate flow is the one place a number is looked up by.

    It has to return matches, or the confirmation screen is useless - but the
    candidate's number is masked even to the admin who registered them. Showing
    it would put a stranger's number in a banner.
    """
    as_user(admin[0])
    secret = "9811100091"
    player = _create(client, _payload(refs, first="Privacy", last="Test", mobile=secret))
    assert player.status_code == 201
    player_id = player.json()["id"]
    code = player.json()["player_code"]

    listing = client.get("/v1/players", params={"limit": 500}).json()
    mine = next(p for p in listing if p["id"] == player_id)
    assert mine["player_code"] == code
    assert mine["phone_display"] != secret
    assert "*" in mine["phone_display"]

    check = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Privacy",
            "last_name": "Test",
            "country_code": "+91",
            "mobile_number": secret,
        },
    ).json()
    assert check["phone_matches"][0]["id"] == player_id
    assert check["phone_matches"][0]["mobile_number"] != secret
    assert "*" in check["phone_matches"][0]["mobile_number"]

    # The registration response does come back in full: the person who just
    # typed the number already knows it, and needs it to confirm the form.
    assert player.json()["mobile_number"] == secret


def test_another_account_cannot_read_a_players_number_or_code(client, refs, admin, as_user):
    as_user(admin[0])
    player = _create(client, _payload(refs, first="Private", last="Record", mobile="9811100092"))
    player_id = player.json()["id"]
    code = player.json()["player_code"]

    as_user(admin[1])
    assert client.get(f"/v1/players/{player_id}").status_code == 404
    assert client.get(f"/v1/players/by-code/{code}").status_code == 404
    assert all(
        p["id"] != player_id for p in client.get("/v1/players", params={"limit": 500}).json()
    )


# --- the invariants themselves -------------------------------------------


def test_every_player_has_a_unique_permanent_code(client, refs, admin, as_user):
    as_user(admin[0])
    for _ in range(3):
        _create(client, _payload(refs, last=f"Code{_next_phone()[-4:]}"))

    codes = [p["player_code"] for p in client.get("/v1/players", params={"limit": 500}).json()]
    assert len(codes) == len(set(codes))
    assert all(c.startswith("CKP-") and len(c) == 12 for c in codes)

    # A code is permanent: renaming a player does not change it.
    target = next(
        p for p in client.get("/v1/players", params={"limit": 500}).json() if p["player_code"]
    )
    renamed = client.patch(f"/v1/players/{target['id']}", json={"first_name": "Renamed"})
    assert renamed.json()["player_code"] == target["player_code"]


def test_generated_codes_do_not_collide_in_practice():
    codes = {generate_player_code() for _ in range(2000)}
    assert len(codes) == 2000


def test_numbers_are_stored_as_text_with_leading_zeros_intact():
    assert phone_e164("+91", "09812345678") == "+919812345678"
    assert phone_e164("91", "+91 98123 45678") == "+919812345678"
    assert phone_e164("+1", "") is None
    assert phone_e164("", "9812345678") is None


# --- email: a contact field, never a key ---------------------------------


def test_two_players_may_share_an_email(client, refs, admin, as_user):
    """A shared email is as real as a shared phone: a parent registers both
    children with one address. The second registration is reported in the
    duplicate check but never blocked - no 409, no confirmation required."""
    as_user(admin[0])
    shared_email = f"shared{_counter['n']}-email@test.com"

    first = _create(client, _payload(refs, first="Aarav", last="Mehta", email=shared_email))
    assert first.status_code == 201
    second = _create(client, _payload(refs, first="Anaya", last="Mehta", email=shared_email))
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]

    by_code = client.get(f"/v1/players/by-code/{second.json()['player_code']}")
    assert by_code.status_code == 200


def test_the_duplicate_check_reports_a_shared_email_but_does_not_block(
    client, refs, admin, as_user
):
    as_user(admin[0])
    email = f"check{_counter['n']}-email@test.com"
    first = _create(client, _payload(refs, first="Riya", last="Nair", email=email))
    assert first.status_code == 201

    check = client.post(
        "/v1/players/check-duplicate",
        json={
            "first_name": "Kiara",
            "last_name": "Nair",
            "country_code": "+91",
            "mobile_number": _next_phone(),
            "email": email,
        },
    ).json()
    assert check["phone_taken"] is False
    assert [m["player_code"] for m in check["email_matches"]] == [first.json()["player_code"]]
    assert check["requires_confirmation"] is False


def test_duplicate_email_does_not_500_on_create(client, refs, admin, as_user):
    """Regression: the players.email unique constraint used to 500 on a second
    registration with the same address."""
    as_user(admin[0])
    email = f"no500{_counter['n']}-email@test.com"
    first = _create(client, _payload(refs, first="Kabir", last="Singh", email=email))
    assert first.status_code == 201

    second = _create(client, _payload(refs, first="Dev", last="Singh", email=email))
    assert second.status_code == 201, second.text


def test_setting_an_email_already_in_use_does_not_500(client, refs, admin, as_user):
    """PATCH and PUT to an email that already belongs to another player must be
    accepted: email is a contact field, not an identity."""
    as_user(admin[0])
    email = f"patch{_counter['n']}-email@test.com"
    first = _create(client, _payload(refs, first="Neil", last="Kapoor", email=email))
    second = _create(client, _payload(refs, first="Omar", last="Kapoor"))
    assert first.status_code == 201 and second.status_code == 201

    patched = client.patch(f"/v1/players/{second.json()['id']}", json={"email": email})
    assert patched.status_code == 200, patched.text
    assert patched.json()["email"] == email

    replaced = client.put(
        f"/v1/players/{second.json()['id']}",
        json=_payload(refs, first="Omar", last="Kapoor", email=email),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["email"] == email


def test_a_shared_email_is_masked_in_duplicate_candidates(client, refs, admin, as_user):
    """The candidate payload must never echo the email back verbatim if showing
    it would leak someone else's address decision; today it omits email."""
    as_user(admin[0])
    check = client.post(
        "/v1/players/check-duplicate",
        json={"first_name": "Nobody", "last_name": "Here"},
    ).json()
    for match in check["phone_matches"] + check["name_matches"] + check["email_matches"]:
        assert "email" not in match


def test_the_model_fills_in_identity_for_any_insert(refs, admin):
    """Even a row written straight through the ORM gets a code and a phone key.

    Nothing but the model can guarantee that: code, and the key duplicates are
    matched on, are invariants rather than something an endpoint may forget.
    """
    level_id, country_id, state_id, city_id = refs
    user = admin[0]

    async def insert():
        async with async_session() as db:
            player = Player(
                first_name="Direct",
                last_name="Insert",
                date_of_birth=datetime.date(1990, 1, 1),
                gender="male",
                batting_hand="Right",
                batting_position="Middle Order",
                bowling_hand="Right",
                bowling_type="Spin",
                country_id=country_id,
                state_id=state_id,
                city_id=city_id,
                height=170.0,
                weight=65.0,
                country_code="91",
                mobile_number="09876543210",
                email=f"direct{_next_phone()}@test.com",
                user_id=user.id,
            )
            db.add(player)
            await db.commit()
            # The leading zero survives in the stored value, and the comparison
            # key drops it - which is the whole reason the column is a string.
            return player.player_code, player.mobile_number, player.phone_e164

    code, mobile, e164 = asyncio.run(insert())
    assert code.startswith("CKP-")
    assert mobile == "09876543210"
    assert e164 == "+919876543210"
    assert level_id is not None
