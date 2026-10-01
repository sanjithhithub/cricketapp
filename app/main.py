import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

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
    OTPVerifyRequest,
    OTPVerifyResponse,
)
from app.scoring.routes import router as scoring_router
from app.seed import seed_levels, seed_locations
from app.teams.routes import router as teams_router

app = FastAPI(title="CricketApp", version="1.0.0")

API_V1_PREFIX = "/v1"
# Alias prefix. The web client is built against "/api"; exposing both means its
# baseURL can point straight at this server with no path rewriting on either side.
API_ALIAS_PREFIX = "/api"


def _split_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


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
    """
    return os.getenv("CORS_ORIGINS_REGEX", "").strip() or None


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
    # Needed so the browser can read these from a cross-origin response.
    expose_headers=["Content-Length", "Content-Type"],
)

routers = [
    (auth_router, []),
    (players_router, [Depends(get_current_user)]),
    (teams_router, [Depends(get_current_user)]),
    (levels_router, [Depends(get_current_user)]),
    (matches_router, [Depends(get_current_user)]),
    (scoring_router, [Depends(get_current_user)]),
]
for router, deps in routers:
    app.include_router(router, prefix=API_V1_PREFIX, dependencies=deps)
    app.include_router(router, prefix=API_ALIAS_PREFIX, dependencies=deps)

UPLOADS_DIR = "uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount(
    "/uploads",
    StaticFiles(directory=UPLOADS_DIR, check_dir=False),
    name="uploads",
)


@app.on_event("startup")
async def startup():
    if DATABASE_URL.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async for db in get_db():
        await seed_locations(db)
        await seed_levels(db)
        break


@app.on_event("shutdown")
async def shutdown():
    await engine.dispose()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/locations", response_model=list[CountryOut])
@app.get("/api/locations", response_model=list[CountryOut], include_in_schema=False)
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_all_countries(db)


@app.get("/v1/country-codes", response_model=list[CountryCodeOut])
@app.get("/api/country-codes", response_model=list[CountryCodeOut], include_in_schema=False)
async def list_country_codes(
    _: User = Depends(get_current_user),
):
    return await get_country_codes()


@app.post("/v1/verify-otp", response_model=OTPVerifyResponse)
@app.post("/api/verify-otp", response_model=OTPVerifyResponse, include_in_schema=False)
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
