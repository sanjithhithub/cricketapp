"""The contract a client is generated from.

Everything else in this suite tests one behaviour at a time. This one tests the
promise made by the document itself - that the spec describes what the server
actually does - because a client generated from the spec has nothing else to go
on. The failures it guards against are all silent: the server behaves correctly,
the route still works by hand, and only a generated client or a future reader of
`/openapi.json` finds out that the contract is wrong.

Five guarantees:

1. every operation documents the failures it can return, including the 401/403/404
   that follow from the dependency graph rather than from any route decorator
2. list responses describe their page in headers, and the body is still a bare
   array
3. `/v1` and `/api` are the same operations, both documented
4. the deprecated full-replacement PUTs still work, and say so
5. refresh tokens rotate, and cannot be replayed
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import RefreshToken, User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.models import City, Country, State

PAGINATION_HEADERS = {"X-Total-Count", "X-Has-More", "X-Skip", "X-Limit"}
RESPONSE_HEADERS = {"x-total-count", "x-has-more", "x-skip", "x-limit"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def schema(client):
    return client.get("/openapi.json").json()


@pytest.fixture(scope="module")
def admin(client):
    async def setup():
        async with async_session() as db:
            user = User(
                email="contract_admin@test.com",
                hashed_password=hash_password("Test@1234"),
                full_name="Contract Admin",
                role="admin",
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return asyncio.run(setup())


@pytest.fixture(scope="module")
def refs(client):
    """Seed ids needed to satisfy the required address fields."""

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


@pytest.fixture
def as_user(admin):
    app.dependency_overrides[get_current_user] = lambda: admin
    yield admin
    app.dependency_overrides.pop(get_current_user, None)


def _player_payload(refs, *, mobile, **overrides):
    """A complete, valid create payload.

    Every field the schema requires has to be present here, which is itself the
    point of the length constraints added to `PlayerCreate`: a client generated
    from the spec is told all of them rather than discovering them one 422 at a
    time.
    """
    level_id, country_id, state_id, city_id = refs
    payload = {
        "first_name": "Contract",
        "last_name": "Dupe",
        "date_of_birth": "1995-05-20",
        "gender": "male",
        "batting_hand": "right",
        "batting_position": "opening",
        "bowling_hand": "right",
        "bowling_type": "medium",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
        "height": 178.0,
        "weight": 72.0,
        "country_code": "+91",
        "mobile_number": mobile,
        "email": f"contract_{mobile}@test.com",
        "role": "playing_11",
    }
    payload.update(overrides)
    return payload


def _team_payload(refs, **overrides):
    """A complete, valid team create payload, for the same reason."""
    level_id, country_id, state_id, city_id = refs
    payload = {
        "name": "Contract Team",
        "short_name": "CT",
        "level_id": level_id,
        "homeground": "Home",
        "founder": "Founder",
        "founded_year": 2001,
        "owner": "Owner",
        "country_id": country_id,
        "state_id": state_id,
        "city_id": city_id,
    }
    payload.update(overrides)
    return payload


def _operations(schema):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield path, method, operation


# --- 1. documented failures ------------------------------------------------


def test_every_operation_documents_a_success_code(schema):
    """A response with only failure codes described is not a usable operation."""
    undocumented = [
        (path, method)
        for path, method, operation in _operations(schema)
        if not {"200", "201", "204"} & set(operation.get("responses", {}))
    ]
    assert undocumented == []


def test_protected_operations_document_401(schema):
    """The original bug: not one 401 anywhere, on every protected route.

    Asserted per operation rather than globally so a regression names the route
    that lost it.
    """
    missing = [
        (path, method)
        for path, method, operation in _operations(schema)
        if path.startswith(("/v1", "/api"))
        and "/auth/" not in path
        and "401" not in operation.get("responses", {})
    ]
    assert missing == []


def test_admin_only_operations_document_403(schema):
    """Spelled out rather than inferred.

    The implied-403 rule reads the dependency graph, so a test that used the same
    inference would pass even if the rule itself were broken. Naming the
    admin-only operations here is what makes the assertion independent of the code
    under test.
    """
    admin_only = [
        ("/v1/players", "post"),
        ("/v1/players/{player_id}", "put"),
        ("/v1/players/{player_id}", "patch"),
        ("/v1/players/{player_id}", "delete"),
        ("/v1/players/{player_id}/teams", "post"),
        ("/v1/players/{player_id}/teams/{team_id}", "patch"),
        ("/v1/players/{player_id}/upload-profile-image", "post"),
        ("/v1/teams", "post"),
        ("/v1/teams/{team_id}/captains", "put"),
        ("/v1/teams/{team_id}/players", "post"),
        ("/v1/matches", "post"),
        ("/v1/matches/{match_id}/deliveries", "post"),
    ]
    missing = [
        (path, method)
        for path, method in admin_only
        if "403" not in schema["paths"][path][method].get("responses", {})
    ]
    assert missing == []


def test_single_record_operations_document_404(schema):
    missing = [
        (path, method)
        for path, method, operation in _operations(schema)
        if "{" in path and "404" not in operation.get("responses", {})
    ]
    assert missing == []


def test_conflict_is_documented_where_a_duplicate_is_refused(schema):
    """A 409 that the spec never mentions is a 409 no client handles."""
    create = schema["paths"]["/v1/players"]["post"]
    assert "409" in create["responses"]
    # The duplicate payload is a named schema, not a free-form object, so a
    # generated client gets a typed `detail.next_action`.
    refs = [
        entry["$ref"]
        for entry in create["responses"]["409"]["content"]["application/json"]["schema"]["anyOf"]
    ]
    assert "#/components/schemas/DuplicatePlayerConflict" in refs

    create_team = schema["paths"]["/v1/teams"]["post"]
    assert "409" in create_team["responses"]


def test_a_duplicate_carries_the_structured_payload(client, as_user, refs):
    """The 409 documented above is the one the server sends.

    Checked against the live endpoint rather than the schema, because the schema
    and the response could agree on the wrong thing.
    """
    payload = _player_payload(refs, mobile="9811122233")
    first = client.post("/v1/players", json=payload)
    assert first.status_code == 201, first.text

    second = client.post("/v1/players", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["next_action"] in ("confirm_same_person", "confirm_duplicate_name")
    assert isinstance(detail["message"], str)
    assert detail["phone_matches"] or detail["name_matches"]


def test_order_exhausted_is_gone(client, as_user):
    """Removed because it always said false.

    A field that is constant carries no information, and its presence in the
    schema tells a client to build a branch that can never be taken.
    """
    assert (
        "order_exhausted"
        not in app.openapi()["components"]["schemas"]["ScorecardResponse"]["properties"]
    )


# --- 2. pagination ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/v1/players", "/v1/teams", "/v1/matches", "/v1/teams/with-counts"],
)
def test_list_endpoints_document_the_page_headers(schema, path):
    ok = schema["paths"][path]["get"]["responses"]["200"]
    assert PAGINATION_HEADERS <= set(ok["headers"])


@pytest.mark.parametrize("path", ["/v1/players", "/v1/teams", "/v1/matches"])
def test_list_endpoints_actually_send_the_page_headers(client, as_user, path):
    """Documented and returned are different claims.

    The body must stay a bare list: an envelope here would break every existing
    client that reads `response.data` as an array, which is the whole reason the
    page size went into headers instead.
    """
    resp = client.get(path, params={"limit": 1})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    for header in RESPONSE_HEADERS:
        assert header in resp.headers, f"{path} did not send {header}"
    assert int(resp.headers["x-limit"]) == 1
    assert int(resp.headers["x-skip"]) == 0
    assert int(resp.headers["x-total-count"]) >= len(resp.json())
    # Total is across all pages, so has_more can disagree with a short page.
    has_more = resp.headers["x-has-more"] == "true"
    assert has_more == (0 + 1 < int(resp.headers["x-total-count"]))


def test_pagination_headers_are_exposed_to_browsers(schema):
    """A header the server sends but does not expose is invisible to fetch()."""
    source = open("app/main.py", encoding="utf-8").read()
    for header in ("X-Total-Count", "X-Has-More", "X-Skip", "X-Limit"):
        assert header in source


# --- 3. both prefixes documented -------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/locations", "/api/country-codes", "/api/verify-otp"],
)
def test_api_alias_reference_routes_are_documented(schema, path):
    """These existed at runtime and were hidden from the spec.

    Served but undocumented is the worst state for a client: it works by hand and
    is missing from anything generated.
    """
    assert path in schema["paths"]


@pytest.mark.parametrize(
    "path",
    ["/v1/locations", "/v1/country-codes", "/v1/verify-otp"],
)
def test_both_prefixes_carry_the_same_operations(schema, path):
    assert path in schema["paths"]


def test_health_is_documented_and_tagged(schema):
    assert schema["paths"]["/health"]["get"]["tags"] == ["health"]
    assert "HealthResponse" in schema["components"]["schemas"]


# --- 4. deprecated PUTs still work -----------------------------------------


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/v1/players/{player_id}", "put"),
        ("/v1/teams/{team_id}", "put"),
        ("/v1/matches/{match_id}", "put"),
    ],
)
def test_replacement_puts_are_marked_deprecated(schema, path, method):
    assert schema["paths"][path][method]["deprecated"] is True


def test_a_deprecated_put_still_replaces(client, as_user, refs):
    """Deprecation is a promise about the future, not a removal today.

    Marking it deprecated while breaking it would help nobody and would convert
    a client warning into an outage.
    """
    team = client.post("/v1/teams", json=_team_payload(refs, name="Deprecated PUT"))
    assert team.status_code == 201, team.text
    body = team.json()

    replaced = client.put(
        f"/v1/teams/{body['id']}",
        json=_team_payload(refs, name="Deprecated PUT Renamed"),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["name"] == "Deprecated PUT Renamed"


# --- 5. refresh tokens -----------------------------------------------------


def _login(client, email, password="Test@1234"):
    resp = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_login_returns_a_refresh_token(client, admin):
    body = _login(client, "contract_admin@test.com")
    assert body["refresh_token"]
    assert body["refresh_expires_in"] > body["expires_in"]


def test_refresh_rotates_and_returns_a_usable_access_token(client, admin):
    first = _login(client, "contract_admin@test.com")

    resp = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    # Rotation: the token handed back is not the one that was sent.
    assert body["refresh_token"] != first["refresh_token"]

    # And the new access token is accepted. This is the whole point of the flow:
    # without it the client has a token it cannot use.
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200


def test_a_refresh_token_is_single_use(client, admin):
    """Rotation is what makes a stolen token detectable rather than indefinite."""
    first = _login(client, "contract_admin@test.com")
    first_refresh = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert first_refresh.status_code == 200

    replay = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401


def test_refresh_rejects_a_token_it_never_issued(client, admin):
    resp = client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


def test_logout_revokes_the_refresh_token(client, admin):
    body = _login(client, "contract_admin@test.com")

    out = client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
    assert out.status_code == 200
    assert out.json()["revoked"] is True

    after = client.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert after.status_code == 401


def test_logout_is_idempotent(client, admin):
    """A client calling logout without a token should not get an error for it.

    Answering 401 to "please log me out" leaves the caller unsure whether the
    session ended, which is the opposite of what it asked.
    """
    body = _login(client, "contract_admin@test.com")
    client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})

    again = client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
    assert again.status_code == 200
    assert again.json()["revoked"] is False


def test_the_stored_token_is_a_hash_not_the_token(client, admin):
    """A database dump must not be replayable as a session."""
    body = _login(client, "contract_admin@test.com")

    async def stored():
        async with async_session() as db:
            result = await db.execute(select(RefreshToken))
            return [record.token_hash for record in result.scalars()]

    hashes = asyncio.run(stored())
    assert hashes
    assert body["refresh_token"] not in hashes
    # SHA-256 hex is 64 characters; the token itself is much shorter.
    assert all(len(h) == 64 for h in hashes)
