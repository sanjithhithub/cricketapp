"""What the specification says about failure, pagination and the document itself.

This API failed in one specific way for a long time: every declared status code
was a success. There was not one 401, 403, 404 or 409 anywhere in the OpenAPI
document, although the server returned all of them on essentially every
protected route. A client generated from the spec therefore handled exactly zero
failures, and the only way to discover the contract was to read the route code.

Three kinds of guarantee are declared here:

* the *implied* ones - 401 on a secured operation, 403 on an admin-only one, 404
  on one addressing a single record. These follow from the dependency graph and
  the paths, so they are derived in :func:`install_openapi` rather than repeated
  on 66 route decorators where one omission would silently repeat the original
  bug;
* the *specific* ones - a 409 that carries a duplicate-player payload, a 400
  that means "the squads rules were broken". Those are per-route and are written
  on the route, where they can be read next to the code that raises them;
* the *cross-cutting* ones - the pagination headers every list endpoint returns,
  and the top-level document blocks (servers, tags) that make the spec
  self-describing.
"""

import os
from typing import Any

from fastapi import FastAPI, Response
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

from app.auth.security import get_current_user, require_admin
from app.players.schemas import PlayerDuplicateDetail


class ErrorResponse(BaseModel):
    """The body of every failure except a validation error.

    ``detail`` is a sentence, never a stack trace, and never a bare status code.
    Two clients can branch on it; nothing can branch on a traceback.
    """

    detail: str = Field(
        ...,
        examples=["Player not found"],
        description="What went wrong, in one sentence.",
    )


class DuplicatePlayerConflict(BaseModel):
    """409 from ``POST /players`` when the registration matches someone on file.

    ``detail`` is a :class:`~app.players.schemas.PlayerDuplicateDetail`: the
    duplicate flow is a conversation, not a rejection. The server reports who it
    matched, says what the client should do next, and refuses to guess whether two
    records are the same person. The client resolves it by resubmitting with
    ``duplicate_confirmed: true`` (a different person who shares a name) or
    ``existing_player_id`` (the same person, whose record is reused).
    """

    detail: PlayerDuplicateDetail


def _error_response(description: str, schema_ref: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": schema_ref}}},
    }


_ERROR = "#/components/schemas/ErrorResponse"

# The failures that every secured operation can produce. Kept as ready-made
# dictionaries so a route merges the ones it needs instead of rewriting prose.
BAD_REQUEST = _error_response(
    "The request was well formed but cannot be carried out - a squad is full, an "
    "OTP is wrong, or a captain is not in the playing XI.",
    _ERROR,
)
UNAUTHORIZED = _error_response(
    "No bearer token, or a token that is expired, malformed, or belongs to a user "
    "who is missing or deactivated. Send `Authorization: Bearer <access_token>`, "
    "and exchange an expired one at `POST /v1/auth/refresh`.",
    _ERROR,
)
FORBIDDEN = _error_response(
    "Authenticated, but the account is not an admin. Admin-only operations are the "
    "ones that create or change records.",
    _ERROR,
)
NOT_FOUND = _error_response(
    "No such record for this account. A record owned by another account is also "
    "reported this way, rather than as 403, so the response cannot be used to "
    "probe which ids exist.",
    _ERROR,
)
CONFLICT = _error_response(
    "The request conflicts with what is already on file - a duplicate name, or a "
    "value another row already holds.",
    _ERROR,
)
DUPLICATE_PLAYER_CONFLICT = {
    "description": (
        "The player matches someone already registered, so nothing was created. "
        "Read `detail.next_action`, show `detail.phone_matches` / "
        "`detail.name_matches` to a human, then resubmit the same body with either "
        "`duplicate_confirmed: true` (a different person who shares a name) or "
        "`existing_player_id` (the same person, whose record is reused)."
    ),
    "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/DuplicatePlayerConflict"}}
    },
}

# Pagination, as response headers rather than an envelope. The body stays the bare
# array every existing client already reads; what is new is that the caller can
# find out whether the page they got is the whole collection, which is the part
# that silently truncated at 500 rows.
PAGINATION_HEADERS: dict[str, Any] = {
    "X-Total-Count": {
        "description": "Rows matching this query across all pages, ignoring skip/limit.",
        "schema": {"type": "integer"},
    },
    "X-Has-More": {
        "description": "True when `skip + limit` is still below `X-Total-Count`.",
        "schema": {"type": "boolean"},
    },
    "X-Skip": {"description": "The offset this page starts at.", "schema": {"type": "integer"}},
    "X-Limit": {
        "description": "The page size this response was limited to.",
        "schema": {"type": "integer"},
    },
}

# Browsers only expose a header to JavaScript if the server opts it in, so these
# four names appear both in the CORS middleware configuration and in the spec.
PAGINATION_HEADER_NAMES = list(PAGINATION_HEADERS)


def paginated_list_response(
    description: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``responses=`` entry for a list endpoint.

    Merges the pagination headers into whatever else the route already declares,
    so a list endpoint that can also fail gets both in one place.
    """
    response = dict(extra or {})
    ok = dict(response.get("200") or {})
    ok["description"] = description
    ok["headers"] = {**PAGINATION_HEADERS, **(ok.get("headers") or {})}
    response["200"] = ok
    return response


def set_pagination_headers(response: Response, *, total: int, skip: int, limit: int) -> None:
    """Publish the page metadata on the response.

    Injected through the ``response: Response`` parameter rather than by building
    a JSONResponse here, so the declared ``response_model`` still does the
    serialising and the body cannot drift from the schema that describes it.
    """
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Has-More"] = "true" if skip + limit < total else "false"
    response.headers["X-Skip"] = str(skip)
    response.headers["X-Limit"] = str(limit)


# Grouped in Swagger by first use. A declared block also means an operation
# cannot silently end up untagged: an operation with no tags renders with no
# heading at all, which is how /health and the reference-data endpoints ended up
# invisible in the docs.
OPENAPI_TAGS: list[dict[str, Any]] = [
    {"name": "auth", "description": "Registration, sign-in, token refresh and sign-out."},
    {"name": "players", "description": "Player records, aliases, duplicates and squads."},
    {"name": "teams", "description": "Teams, squads, captaincy and squad membership."},
    {"name": "matches", "description": "Match fixtures and their result."},
    {"name": "scoring", "description": "Live scoring: innings, deliveries and scorecards."},
    {
        "name": "tournaments",
        "description": "Competitions: teams, fixtures, points table and leaderboards.",
    },
    {"name": "levels", "description": "Competition levels a team can be registered at."},
    {"name": "reference", "description": "Countries, states, cities and dial codes."},
    {"name": "health", "description": "Liveness probe."},
]


def openapi_servers() -> list[dict[str, Any]]:
    """The base URL clients should call, for the document's ``servers`` block.

    Relative ("/") by default, which resolves against whatever host serves the
    docs, so the block is never wrong. Set ``PUBLIC_BASE_URL`` to publish an
    absolute base instead - useful when the spec is read by a tool that has no
    page to resolve against.
    """
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return [{"url": configured, "description": "Configured public base URL"}]
    return [{"url": "/", "description": "This server, relative to the docs host"}]


def _dependency_calls(route: Any) -> list[Any]:
    """Every dependency callable on a route, including nested ones."""
    calls: list[Any] = []

    def walk(dependant: Any) -> None:
        if dependant.call is not None:
            calls.append(dependant.call)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    return calls


def _add_implied_errors(app: FastAPI, schema: dict[str, Any]) -> None:
    """Document the failures that follow from the dependency graph and the paths.

    Only adds a status code that is not already declared, so a route that is
    precise about its own failures keeps its own description.
    """
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(("/v1", "/api")) or not hasattr(route, "methods"):
            continue

        calls = _dependency_calls(route)
        secured = bool({get_current_user, require_admin} & set(calls))
        admin_only = require_admin in calls
        # Every id in this API is a path parameter of an operation that 404s when
        # the row is missing, including the by-code lookup.
        addresses_a_record = "{" in path

        # OpenAPI keys operations by lower-case method; route.methods is upper-case.
        # Without this the lookup silently misses every operation, which is how a
        # spec can contain none of the failures the server actually returns.
        method = next(iter(sorted(route.methods - {"HEAD"} - {"OPTIONS"})), "").lower()
        operation = schema.get("paths", {}).get(path, {}).get(method, {})
        if not operation:
            continue

        responses = operation.setdefault("responses", {})
        if secured and "401" not in responses:
            responses["401"] = UNAUTHORIZED
        if secured and admin_only and "403" not in responses:
            responses["403"] = FORBIDDEN
        if addresses_a_record and "404" not in responses:
            responses["404"] = NOT_FOUND


def install_openapi(app: FastAPI) -> None:
    """Teach the app to build a spec that documents its own failures."""
    app.openapi_schema = None

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=OPENAPI_TAGS,
            servers=openapi_servers(),
        )
        _add_implied_errors(app, schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
