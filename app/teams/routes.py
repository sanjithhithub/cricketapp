from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.players.crud import (
    assign_player_to_team_by_phone,
    assign_players_to_team_bulk,
    get_available_players_for_team,
)
from app.players.schemas import PlayerDropdownItem, TeamPlayerByPhone
from app.storage import InvalidImage, image_key, put_image
from app.teams.crud import (
    create_team,
    delete_team,
    get_team,
    get_team_detail,
    get_team_options,
    get_team_squad,
    get_teams,
    replace_team,
    set_team_captains,
    update_team,
)
from app.teams.schemas import (
    PlayerBulkAssignResponse,
    TeamBulkPlayerAdd,
    TeamCaptainsResponse,
    TeamCaptainsSet,
    TeamCreate,
    TeamDetailResponse,
    TeamListItem,
    TeamOption,
    TeamResponse,
    TeamUpdate,
)

router = APIRouter(tags=["teams"])


@router.get("/teams", response_model=list[TeamListItem])
async def list_teams(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_teams(db, user_id=current_user.id, skip=skip, limit=limit, level_id=level_id)


@router.get("/teams/options", response_model=list[TeamOption])
async def list_team_options(
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_team_options(db, user_id=current_user.id, level_id=level_id)


@router.get("/teams/with-counts", response_model=list[TeamListItem])
async def list_teams_with_player_counts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_teams(db, user_id=current_user.id, skip=skip, limit=limit, level_id=level_id)


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team_endpoint(
    data: TeamCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await create_team(db, data, user_id=current_user.id)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A team with this name already exists in your account.")


@router.get("/teams/{team_id}", response_model=TeamDetailResponse)
async def get_team_endpoint(
    team_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    team = await get_team_detail(db, team_id, current_user.id)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.get("/teams/{team_id}/squad")
@router.get("/teams/{team_id}/players", include_in_schema=False)
async def get_team_squad_endpoint(
    team_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    squad = await get_team_squad(db, team_id, current_user.id)
    if not squad:
        raise HTTPException(404, "Team not found")
    return squad


@router.put("/teams/{team_id}/captains", response_model=TeamCaptainsResponse)
async def set_team_captains_endpoint(
    team_id: int,
    data: TeamCaptainsSet,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result, error = await set_team_captains(
        db,
        team_id,
        data.captain_player_id,
        data.vice_captain_player_id,
        current_user.id,
    )
    if error:
        # 400 not 404: the squad rules were violated (same player twice, not in
        # the XI, not on this squad). The client fixes it by picking differently.
        raise HTTPException(400, error)
    captain_id, vice_id = result
    return TeamCaptainsResponse(
        message=error,
        team_id=team_id,
        captain_player_id=captain_id,
        vice_captain_player_id=vice_id,
    )


@router.put("/teams/{team_id}", response_model=TeamResponse)
async def replace_team_endpoint(
    team_id: int,
    data: TeamCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        team = await replace_team(db, team_id, data, current_user.id)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A team with this name already exists in your account.")
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team_endpoint(
    team_id: int,
    data: TeamUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        team = await update_team(db, team_id, data, current_user.id)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A team with this name already exists in your account.")
    if not team:
        raise HTTPException(404, "Team not found")
    return team


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team_endpoint(
    team_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_team(db, team_id, current_user.id)
    if not deleted:
        raise HTTPException(404, "Team not found")


@router.post("/teams/{team_id}/upload-logo")
async def upload_team_logo(
    team_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Ownership is resolved before the upload so a 404 does not leave an orphan
    # object in the bucket for every attempt against a missing team.
    team = await get_team(db, team_id, current_user.id)
    if not team:
        raise HTTPException(404, "Team not found")

    # Key built from the team id and the sniffed content type only. The previous
    # os.path.join(UPLOAD_DIR, filename) let a client-supplied filename carrying
    # "../" segments write outside the uploads directory.
    try:
        key = image_key("teams", team_id, file.content_type or "")
        await put_image(key, await file.read(), file.content_type or "")
    except InvalidImage as exc:
        raise HTTPException(400, str(exc)) from exc

    await db.commit()
    await db.refresh(team)
    return {"logo": key}


@router.get("/teams/{team_id}/available-players", response_model=list[PlayerDropdownItem])
async def get_available_players_endpoint(
    team_id: int,
    q: str | None = Query(None, description="Search by first or last name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    players, error = await get_available_players_for_team(
        db, team_id, user_id=current_user.id, q=q, skip=skip, limit=limit
    )
    if error:
        raise HTTPException(404, error)
    return players


@router.post("/teams/{team_id}/players", status_code=201)
async def add_player_to_team_by_phone_endpoint(
    team_id: int,
    data: TeamPlayerByPhone,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await assign_player_to_team_by_phone(
        db, team_id, data.country_code, data.mobile_number, data.role, user_id=current_user.id
    )
    if error:
        raise HTTPException(400, error)
    return {"message": "Player added to team successfully", "assignment": assignment}


@router.post("/teams/{team_id}/players/bulk", response_model=PlayerBulkAssignResponse)
async def add_players_to_team_bulk_endpoint(
    team_id: int,
    data: TeamBulkPlayerAdd,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a selected group of players to a squad in one call.

    The selection is a list of ``player_id``s, never names: two players can share
    a name, and only the id says which one is which. A repeated id in the same
    request is rejected, as is a selection that would take the playing XI past
    eleven.
    """
    assignments, added_ids, error = await assign_players_to_team_bulk(
        db, team_id, data.player_ids, data.role, user_id=current_user.id
    )
    if error:
        raise HTTPException(400, error)
    return PlayerBulkAssignResponse(
        message=f"Added {len(added_ids)} player(s) to the squad",
        team_id=team_id,
        role=data.role,
        added_player_ids=added_ids,
    )
