import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import (
    BAD_REQUEST,
    OPENAPI_TAGS,
    PAGINATION_HEADER_NAMES,
    install_openapi,
)
from app.auth.models import User
from app.auth.routes import router as auth_router
from app.auth.security import get_current_user
from app.crud import (
    get_all_countries,
    get_country_codes,
    verify_otp_code,
)
from app.database import DATABASE_URL, Base, engine, get_db
from app.levels.routes import router as levels_router
from app.matches.routes import router as matches_router
from app.players.crud import mark_player_phone_verified
from app.players.routes import router as players_router
from app.schemas import (
    CountryCodeOut,
    CountryOut,
    HealthResponse,
    OTPVerifyRequest,
    OTPVerifyResponse,
)
from app.scoring.routes import router as scoring_router
from app.seed import seed_levels, seed_locations
from app.storage import read_image
from app.teams.routes import router as teams_router
from app.tournaments.routes import router as tournaments_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create tables on SQLite, seed reference data, then run.

    Replaces the deprecated ``@app.on_event`` hooks: the context manager form
    is what FastAPI moved to, and it guarantees the shutdown body runs even when
    startup fails halfway - the engine is disposed either way.

    ``create_all`` runs only on SQLite. Postgres is managed by Alembic
    (``docker-entrypoint.sh`` runs ``alembic upgrade head`` before uvicorn), so
    creating tables here would paper over a missing migration instead of
    failing loudly on deploy.
    """
    if DATABASE_URL.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async for db in get_db():
        await seed_locations(db)
        await seed_levels(db)
        break
    yield
    await engine.dispose()


app = FastAPI(
    title="CricketApp",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Cricket scoring and team management.\n\n"
        "Every route is served under both `/v1` and `/api`; the two prefixes are "
        "aliases of the same operations.\n\n"
        "**Authentication.** Send `Authorization: Bearer <access_token>`. Access "
        "tokens are short lived. When one expires, `POST /v1/auth/refresh` with the "
        "refresh token returns a new pair without a password; the refresh token is "
        "single use and is replaced by each refresh.\n\n"
        '**Failures.** Every failure body is `{"detail": "..."}`, except '
        "`POST /v1/players`, whose 409 carries a structured duplicate-player payload "
        "(`detail.next_action` tells the client what to do next), and validation "
        "errors, which use FastAPI's `detail` array.\n\n"
        "**Pagination.** List endpoints return a bare array and describe the page "
        "in the `X-Total-Count`, `X-Has-More`, `X-Skip` and `X-Limit` response "
        "headers. A page is never silently truncated: check `X-Has-More`."
    ),
    openapi_tags=OPENAPI_TAGS,
)

API_V1_PREFIX = "/v1"
# Alias prefix. The web client is built against "/api"; exposing both means its
# baseURL can point straight at this server with no path rewriting on either side.
API_ALIAS_PREFIX = "/api"


DEV_ORIGIN_REGEX = (
    r"^http://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}):\d{2,5}$"
)


def _split_origins(raw: str) -> list[str]:
    """Split a comma-separated CORS_ORIGINS value into a clean origin list.

    Entries are normalised because browsers compare the Origin header byte for
    byte against Access-Control-Allow-Origin. A trailing slash, a missing
    scheme, or a stray space therefore silently fails to match and the request
    is rejected. Normalising here means one sloppy value in .env does not look
    like a mysterious CORS bug in the browser.
    """
    origins: list[str] = []
    for raw_origin in raw.split(","):
        origin = raw_origin.strip().strip('"').strip("'").strip()
        if not origin:
            continue
        if origin == "*":
            # Valid only without credentials; see the middleware comment below.
            origins.append(origin)
            continue
        if "://" not in origin:
            origin = f"https://{origin}"
        scheme, _, host = origin.partition("://")
        host = host.split("/")[0].rstrip("/")
        if not host:
            continue
        rebuilt = f"{scheme.lower()}://{host.lower()}"
        if rebuilt not in origins:
            origins.append(rebuilt)
    return origins


def _cors_origins() -> list[str]:
    configured = os.getenv("CORS_ORIGINS", "").strip()
    if configured:
        return _split_origins(configured)
    # Dev fallback only. In production CORS_ORIGINS is set in .env; leaving it
    # empty here means every browser request from the real site is blocked.
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://192.168.1.34:5173",
        "http://192.168.1.38:5173",
    ]


def _cors_origin_regex() -> str | None:
    """Pattern for extra origins, from CORS_ORIGINS_REGEX.

    Needed for cases a literal list cannot express: a large set of preview
    subdomains, or any host under a domain you control. Kept separate from
    CORS_ORIGINS so the common case stays a plain list a reviewer can read.

    When it is unset we fall back to DEV_ORIGIN_REGEX so that a Vite dev server
    which auto-increments its port (5173 busy -> 5174, 5175, ...) is not locked
    out. The port is matched, not fixed at 5173, which was the previous
    behaviour and the cause of intermittent CORS failures during development.
    Restricted to http on private/loopback hosts, so this cannot widen the
    allow-list to a public https origin.
    """
    return os.getenv("CORS_ORIGINS_REGEX", "").strip() or DEV_ORIGIN_REGEX


app.add_middleware(
    CORSMiddleware,
    # Credentials are enabled, so origins cannot be "*": the spec forbids pairing
    # a wildcard origin with credentials, and browsers reject such a response.
    # CORS_ORIGINS_REGEX is the escape hatch when you need many hosts - pair it
    # with allow_credentials and the server reflects the matching origin, which
    # is legal precisely because it is never the literal "*".
    allow_origins=_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Browsers cap a preflight cache at 600 seconds, so 10 minutes is the most
    # that actually takes effect. Without this every request pays a preflight.
    max_age=600,
    # Needed so the browser can read these from a cross-origin response. The
    # pagination names are here because a header the server sends but does not
    # list is invisible to fetch() and XHR, which would leave browser clients
    # unable to see whether a page was truncated - exactly the bug the headers
    # were added to solve.
    expose_headers=["Content-Length", "Content-Type", *PAGINATION_HEADER_NAMES],
)

routers = [
    (auth_router, []),
    (players_router, [Depends(get_current_user)]),
    (teams_router, [Depends(get_current_user)]),
    (levels_router, [Depends(get_current_user)]),
    (matches_router, [Depends(get_current_user)]),
    (scoring_router, [Depends(get_current_user)]),
    (tournaments_router, [Depends(get_current_user)]),
]
for router, deps in routers:
    app.include_router(router, prefix=API_V1_PREFIX, dependencies=deps)
    app.include_router(router, prefix=API_ALIAS_PREFIX, dependencies=deps)

UPLOADS_DIR = "uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Installed after the routers so the dependency graph it walks is complete. Until
# this runs, the document declares no failures at all - see app/api_docs.py.
install_openapi(app)


def _cdn_base_url() -> str:
    """CloudFront base URL for uploads, from UPLOADS_CDN_BASE_URL.

    Unset means CloudFront is not configured yet, and uploads are streamed from
    S3 by this app instead. Kept as a runtime lookup rather than a constant so
    flipping it on is a .env change and a container restart, with no code deploy
    and no window where the two halves disagree about the URL shape.
    """
    return os.getenv("UPLOADS_CDN_BASE_URL", "").strip().rstrip("/")


@app.get("/uploads/{key:path}", include_in_schema=False)
async def serve_upload(key: str):
    """Serve an upload, redirecting to CloudFront when it is configured.

    Replaces the StaticFiles mount, which served only the local uploads
    directory and therefore 404'd every image once uploads moved to the bucket.

    With UPLOADS_CDN_BASE_URL set, the response is a 307 to the CDN and the
    bytes never touch this container, so image views stop consuming API CPU on a
    small instance. Browsers follow a redirect inside <img src>, so the stored
    key and every existing <img src> URL stay exactly as they are and the
    frontend needs no change.

    A file found on disk is still served from disk even when the CDN is on. Those
    are the pre-migration leftovers: the object is not in the bucket, so a
    redirect would trade a 200 for a 404. Redirecting without checking would be
    worse, and an S3 HEAD to confirm the object exists would reintroduce the
    per-request API call this is meant to remove.

    The key is resolved and rejected before any S3 call: it must stay inside the
    bucket, which is checked with PurePosixPath rather than string matching so
    encoded traversal like "..%2f..%2fetc" cannot slip through. Note FastAPI has
    already URL-decoded the path segment by this point.
    """
    candidate = PurePosixPath(key)

    # Rejects absolute paths, "..", and anything with an empty or dot segment.
    # PurePosixPath("a/../../b").parts contains "..", so this is sufficient.
    if ".." in candidate.parts or candidate.is_absolute():
        raise HTTPException(400, "Invalid key")
    if not candidate.parts:
        raise HTTPException(400, "Invalid key")

    normalized = candidate.as_posix()

    # Legacy records still point at the old local layout, e.g.
    # "uploads/teams/team_16.png" written before uploads moved to S3.
    legacy = os.path.join(UPLOADS_DIR, normalized)
    if os.path.isfile(legacy) and os.path.abspath(legacy).startswith(
        os.path.abspath(UPLOADS_DIR) + os.sep
    ):
        with open(legacy, "rb") as handle:
            return Response(content=handle.read(), media_type="image/png")

    # New records store the bare S3 key, e.g. "teams/16.png".
    bucket_key = normalized
    if bucket_key.startswith("uploads/"):
        bucket_key = bucket_key[len("uploads/") :]

    cdn_base = _cdn_base_url()
    if cdn_base:
        # quote() keeps the key from contributing raw "?" or "#" to the URL.
        # "/" is left readable because it is the key's own path separator.
        return RedirectResponse(f"{cdn_base}/{quote(bucket_key, safe='/')}", status_code=307)

    data = await read_image(bucket_key)
    if data is None:
        raise HTTPException(404, "Not found")

    suffix = os.path.splitext(bucket_key)[1].lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix)

    if media_type is None:
        raise HTTPException(415, "Unsupported media type")

    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health():
    """Liveness probe.

    Deliberately unauthenticated and dependency-free: a probe that needs a token
    cannot answer the question "is this process up", only "is someone's token
    still valid".
    """
    return {"status": "ok"}


@app.get("/v1/locations", response_model=list[CountryOut], tags=["reference"])
@app.get("/api/locations", response_model=list[CountryOut], tags=["reference"])
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_all_countries(db)


@app.get("/v1/country-codes", response_model=list[CountryCodeOut], tags=["reference"])
@app.get("/api/country-codes", response_model=list[CountryCodeOut], tags=["reference"])
async def list_country_codes(
    _: User = Depends(get_current_user),
):
    return await get_country_codes()


@app.post(
    "/v1/verify-otp",
    response_model=OTPVerifyResponse,
    tags=["players"],
    responses={"400": BAD_REQUEST},
)
@app.post(
    "/api/verify-otp",
    response_model=OTPVerifyResponse,
    tags=["players"],
    responses={"400": BAD_REQUEST},
)
async def verify_otp(
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    valid = await verify_otp_code(db, data.country_code, data.mobile_number, data.otp_code)
    if not valid:
        raise HTTPException(400, "Invalid or expired OTP")
    await mark_player_phone_verified(db, data.country_code, data.mobile_number, current_user.id)
    return OTPVerifyResponse(message="Phone number verified successfully", verified=True)
