from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db, engine, Base
from app.models import Country, State, City
from app.schemas import (
    CountryOut,
    StateOut,
    LocationOut,
    CountryCodeOut,
    OTPVerifyRequest,
    OTPVerifyResponse,
)
from app.crud import (
    get_all_countries,
    get_country_codes,
    verify_otp_code,
)
from app.players.routes import router as players_router
from app.teams.routes import router as teams_router
from app.levels.routes import router as levels_router
from app.players.crud import mark_player_phone_verified
from app.seed import seed_locations

app = FastAPI(title="CricketApp", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_V1_PREFIX = "/v1"

app.include_router(players_router, prefix=API_V1_PREFIX)
app.include_router(teams_router, prefix=API_V1_PREFIX)
app.include_router(levels_router, prefix=API_V1_PREFIX)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async for db in get_db():
        await seed_locations(db)
        break


@app.on_event("shutdown")
async def shutdown():
    await engine.dispose()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/locations", response_model=list[CountryOut])
async def list_locations(db: AsyncSession = Depends(get_db)):
    return await get_all_countries(db)


@app.get("/v1/country-codes", response_model=list[CountryCodeOut])
async def list_country_codes():
    return await get_country_codes()


@app.post("/v1/verify-otp", response_model=OTPVerifyResponse)
async def verify_otp(
    data: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    valid = await verify_otp_code(db, data.country_code, data.mobile_number, data.otp_code)
    if not valid:
        raise HTTPException(400, "Invalid or expired OTP")
    await mark_player_phone_verified(db, data.country_code, data.mobile_number)
    return OTPVerifyResponse(message="Phone number verified successfully", verified=True)



