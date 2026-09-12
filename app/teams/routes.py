import os
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.players.crud import assign_player_to_team_by_phone, get_available_players_for_team
from app.players.schemas import PlayerDropdownItem, TeamPlayerByPhone
from app.teams.crud import (
    create_team,
    delete_team,
    get_team_detail,
    get_team_options,
    get_team_squad,
    get_teams,
    get_teams_with_player_counts,
    replace_team,
    update_team,
    update_team_logo,
)
from app.teams.schemas import (
    TeamCreate,
    TeamDetailResponse,
    TeamOption,
    TeamPlayerCount,
    TeamResponse,
    TeamUpdate,
)

router = APIRouter(tags=["teams"])
UPLOAD_DIR = "uploads/teams"


@router.get("/teams", response_model=list[TeamResponse])
async def list_teams(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    db: AsyncSession = Depends(get_db),
):
    return await get_teams(db, skip=skip, limit=limit, level_id=level_id)


@router.get("/teams/options", response_model=list[TeamOption])
async def list_team_options(
    level_id: int | None = Query(None, description="Filter by team level"),
    db: AsyncSession = Depends(get_db),
):
    return await get_team_options(db, level_id=level_id)


@router.get("/teams/with-counts", response_model=list[TeamPlayerCount])
async def list_teams_with_player_counts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    db: AsyncSession = Depends(get_db),
):
    return await get_teams_with_player_counts(db, skip=skip, limit=limit, level_id=level_id)


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team_endpoint(
    data: TeamCreate,
    db: AsyncSession = Depends(get_db),
):
    return await create_team(db, data)


@router.get("/teams/{team_id}", response_model=TeamDetailResponse)
async def get_team_endpoint(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    team = await get_team_detail(db, team_id)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.get("/teams/{team_id}/squad")
async def get_team_squad_endpoint(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    squad = await get_team_squad(db, team_id)
    if not squad:
        raise HTTPException(404, "Team not found")
    return squad


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


@router.get("/teams/{team_id}/available-players", response_model=list[PlayerDropdownItem])
async def get_available_players_endpoint(
    team_id: int,
    q: str | None = Query(None, description="Search by first or last name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    players, error = await get_available_players_for_team(db, team_id, q=q, skip=skip, limit=limit)
    if error:
        raise HTTPException(404, error)
    return players


@router.post("/teams/{team_id}/players", status_code=201)
async def add_player_to_team_by_phone_endpoint(
    team_id: int,
    data: TeamPlayerByPhone,
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await assign_player_to_team_by_phone(
        db, team_id, data.country_code, data.mobile_number, data.role
    )
    if error:
        raise HTTPException(400, error)
    return {"message": "Player added to team successfully", "assignment": assignment}
