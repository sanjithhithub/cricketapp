import os
import shutil
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.teams.schemas import TeamCreate, TeamUpdate, TeamResponse, TeamPlayerAdd, PlayerOnTeam
from app.teams.crud import (
    get_teams,
    get_team,
    create_team,
    update_team,
    replace_team,
    delete_team,
    add_player_to_team,
    remove_player_from_team,
    update_team_logo,
)

router = APIRouter(tags=["teams"])
UPLOAD_DIR = "uploads/teams"


@router.get("/teams", response_model=list[TeamResponse])
async def list_teams(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return await get_teams(db, skip=skip, limit=limit)


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team_endpoint(
    data: TeamCreate,
    db: AsyncSession = Depends(get_db),
):
    return await create_team(db, data)


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team_endpoint(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    team = await get_team(db, team_id)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.put("/teams/{team_id}", response_model=TeamResponse)
async def replace_team_endpoint(
    team_id: int,
    data: TeamCreate,
    db: AsyncSession = Depends(get_db),
):
    team = await replace_team(db, team_id, data)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team_endpoint(
    team_id: int,
    data: TeamUpdate,
    db: AsyncSession = Depends(get_db),
):
    team = await update_team(db, team_id, data)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team_endpoint(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_team(db, team_id)
    if not deleted:
        raise HTTPException(404, "Team not found")


@router.post("/teams/{team_id}/players", response_model=PlayerOnTeam, status_code=201)
async def add_player_to_team_endpoint(
    team_id: int,
    data: TeamPlayerAdd,
    db: AsyncSession = Depends(get_db),
):
    player = await add_player_to_team(
        db, team_id, data.country_code, data.mobile_number
    )
    if player is None:
        raise HTTPException(404, "Team or Player not found")
    return player


@router.get("/teams/{team_id}/players", response_model=list[PlayerOnTeam])
async def list_team_players(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    team = await get_team(db, team_id)
    if not team:
        raise HTTPException(404, "Team not found")
    return team.players


@router.delete("/teams/{team_id}/players/{player_id}", status_code=204)
async def remove_player_from_team_endpoint(
    team_id: int,
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    removed = await remove_player_from_team(db, team_id, player_id)
    if not removed:
        raise HTTPException(404, "Player not found in team")


@router.post("/teams/{team_id}/upload-logo")
async def upload_team_logo(
    team_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    ext = os.path.splitext(file.filename)[1] if file.filename else ".png"
    filename = f"team_{team_id}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    team = await update_team_logo(db, team_id, file_path)
    if not team:
        raise HTTPException(404, "Team not found")
    return {"logo": file_path}
