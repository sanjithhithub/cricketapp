from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.players.schemas import (
    PlayerCreate,
    PlayerUpdate,
    PlayerResponse,
    PlayerCreateResponse,
    ResendOTPResponse,
    TeamAssignment,
    TeamAssignmentUpdate,
    PlayerTeamInfo,
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
    assign_player_to_team,
    get_player_teams,
    update_player_team_role,
    remove_player_from_team,
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


@router.post("/players/{player_id}/teams", status_code=201)
async def assign_player_to_team_endpoint(
    player_id: int,
    data: TeamAssignment,
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await assign_player_to_team(db, player_id, data)
    if error:
        raise HTTPException(400, error)
    return {"message": "Player assigned to team successfully", "assignment": assignment}


@router.get("/players/{player_id}/teams")
async def get_player_teams_endpoint(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    player = await get_player(db, player_id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await get_player_teams(db, player_id)


@router.patch("/players/{player_id}/teams/{team_id}")
async def update_player_team_role_endpoint(
    player_id: int,
    team_id: int,
    data: TeamAssignmentUpdate,
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await update_player_team_role(db, player_id, team_id, data.role)
    if error:
        raise HTTPException(404, error)
    return {"message": f"Role updated to {data.role}"}


@router.delete("/players/{player_id}/teams/{team_id}", status_code=204)
async def remove_player_from_team_endpoint(
    player_id: int,
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    removed = await remove_player_from_team(db, player_id, team_id)
    if not removed:
        raise HTTPException(404, "Player not found in this team")
