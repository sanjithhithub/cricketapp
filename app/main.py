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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_V1_PREFIX = "/v1"

app.include_router(auth_router, prefix=API_V1_PREFIX)
app.include_router(players_router, prefix=API_V1_PREFIX, dependencies=[Depends(get_current_user)])
app.include_router(teams_router, prefix=API_V1_PREFIX, dependencies=[Depends(get_current_user)])
app.include_router(levels_router, prefix=API_V1_PREFIX, dependencies=[Depends(get_current_user)])
app.include_router(matches_router, prefix=API_V1_PREFIX, dependencies=[Depends(get_current_user)])
app.include_router(scoring_router, prefix=API_V1_PREFIX, dependencies=[Depends(get_current_user)])
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


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
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_all_countries(db)


@app.get("/v1/country-codes", response_model=list[CountryCodeOut])
async def list_country_codes(
    _: User = Depends(get_current_user),
):
    return await get_country_codes()


@app.post("/v1/verify-otp", response_model=OTPVerifyResponse)
async def verify_otp(
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    valid = await verify_otp_code(db, data.country_code, data.mobile_number, data.otp_code)
    if not valid:
        raise HTTPException(400, "Invalid or expired OTP")
    await mark_player_phone_verified(db, data.country_code, data.mobile_number)
    return OTPVerifyResponse(message="Phone number verified successfully", verified=True)
