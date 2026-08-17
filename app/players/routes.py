from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.players.schemas import (
    PlayerCreate,
    PlayerUpdate,
    PlayerResponse,
    PlayerCreateResponse,
    ResendOTPResponse,
)
from app.players.crud import (
    get_players,
    get_player,
    create_player,
    update_player,
    replace_player,
    delete_player,
    resend_otp_for_player,
    search_players,
    get_unassigned_players,
)

router = APIRouter(tags=["players"])


@router.get("/players", response_model=list[PlayerResponse])
async def list_players(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return await get_players(db, skip=skip, limit=limit)


@router.get("/players/search", response_model=list[PlayerResponse])
async def search_players_endpoint(
    q: str = Query(..., min_length=1, description="Search by first or last name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return await search_players(db, q, skip=skip, limit=limit)


@router.get("/players/unassigned", response_model=list[PlayerResponse])
async def list_unassigned_players(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return await get_unassigned_players(db, skip=skip, limit=limit)


@router.post("/players", response_model=PlayerCreateResponse, status_code=201)
async def create_player_endpoint(
    data: PlayerCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        player, otp_sent = await create_player(db, data)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return PlayerCreateResponse(
        **player.__dict__,
        message=(
            "Player created. OTP sent to mobile number via SMS."
            if otp_sent
            else "Player created but OTP SMS could not be sent. Please check the SMS service configuration."
        ),
        otp_sent=otp_sent,
    )


@router.post("/players/{player_id}/resend-otp", response_model=ResendOTPResponse)
async def resend_otp_endpoint(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    player, otp_sent = await resend_otp_for_player(db, player_id)
    if not player:
        raise HTTPException(404, "Player not found")
    return ResendOTPResponse(
        message=(
            "New OTP sent to mobile number via SMS."
            if otp_sent
            else "New OTP could not be sent. Please check the SMS service configuration."
        ),
        otp_sent=otp_sent,
    )


@router.get("/players/{player_id}", response_model=PlayerResponse)
async def get_player_endpoint(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    player = await get_player(db, player_id)
    if not player:
        raise HTTPException(404, "Player not found")
    return player


@router.put("/players/{player_id}", response_model=PlayerResponse)
async def replace_player_endpoint(
    player_id: int,
    data: PlayerCreate,
    db: AsyncSession = Depends(get_db),
):
    player = await replace_player(db, player_id, data)
    if not player:
        raise HTTPException(404, "Player not found")
    return player


@router.patch("/players/{player_id}", response_model=PlayerResponse)
async def update_player_endpoint(
    player_id: int,
    data: PlayerUpdate,
    db: AsyncSession = Depends(get_db),
):
    player = await update_player(db, player_id, data)
    if not player:
        raise HTTPException(404, "Player not found")
    return player


@router.delete("/players/{player_id}", status_code=204)
async def delete_player_endpoint(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_player(db, player_id)
    if not deleted:
        raise HTTPException(404, "Player not found")
