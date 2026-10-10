from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api_docs import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    paginated_list_response,
    set_pagination_headers,
)
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.players.crud import (
    assign_player_to_team_by_phone,
    assign_players_to_team_bulk,
    get_available_players_for_team,
)
from app.players.schemas import PlayerDropdownItem, TeamPlayerByPhone
from app.teams.analytics import get_head_to_head_analytics, get_team_analytics
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
    HeadToHeadAnalyticsResponse,
    PlayerBulkAssignResponse,
    TeamAnalyticsResponse,
    TeamBulkPlayerAdd,
    TeamCaptainsResponse,
    TeamCaptainsSet,
    TeamCreate,
    TeamDetailResponse,
    TeamListItem,
    TeamLogoResponse,
    TeamOption,
    TeamPlayerAddResponse,
    TeamResponse,
    TeamSquadResponse,
    TeamUpdate,
)

router = APIRouter(tags=["teams"])


@router.get(
    "/teams",
    response_model=list[TeamListItem],
    responses=paginated_list_response("One page of this account's teams."),
)
async def list_teams(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    teams, total = await get_teams(
        db, user_id=current_user.id, skip=skip, limit=limit, level_id=level_id
    )
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return teams


@router.get("/teams/options", response_model=list[TeamOption])
async def list_team_options(
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_team_options(db, user_id=current_user.id, level_id=level_id)


@router.get(
    "/teams/with-counts",
    response_model=list[TeamListItem],
    responses=paginated_list_response("One page of this account's teams, with squad counts."),
)
async def list_teams_with_player_counts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    level_id: int | None = Query(None, description="Filter by team level"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    teams, total = await get_teams(
        db, user_id=current_user.id, skip=skip, limit=limit, level_id=level_id
    )
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return teams


@router.post("/teams", response_model=TeamResponse, status_code=201, responses={"409": CONFLICT})
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


@router.get(
    "/teams/{team_id}/squad",
    response_model=TeamSquadResponse,
    responses={"404": NOT_FOUND},
)
@router.get("/teams/{team_id}/players", include_in_schema=False)
async def get_team_squad_endpoint(
    team_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The squad split into playing XI, substitutes and bench.

    An empty bucket is an empty list, never a missing key, so a client can render
    the three sections without checking each one.
    """
    squad = await get_team_squad(db, team_id, current_user.id)
    if not squad:
        raise HTTPException(404, "Team not found")
    return squad


@router.get(
    "/teams/{team_id}/analytics",
    response_model=TeamAnalyticsResponse,
    responses={"404": NOT_FOUND},
)
async def get_team_analytics_endpoint(
    team_id: int,
    recent: int = Query(5, ge=1, le=20, description="How many recent results to return"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A team's record across its completed matches.

    Covers matches played, wins, losses, draws, win percentage, runs scored and
    conceded, highest and lowest innings, average score and scoring rate, recent
    form (``recent`` results, most recent first), the leading run scorer and the
    leading wicket taker. Only completed matches this account owns are counted,
    and the winner of each is derived with the same rules as the match summary.
    """
    team = await get_team(db, team_id, current_user.id)
    if not team:
        raise HTTPException(404, "Team not found")
    payload = await get_team_analytics(db, team, current_user.id, recent=recent)
    return TeamAnalyticsResponse.model_validate(payload)


@router.get(
    "/teams/{team_id}/head-to-head/{opponent_id}",
    response_model=HeadToHeadAnalyticsResponse,
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def get_head_to_head_endpoint(
    team_id: int,
    opponent_id: int,
    recent: int = Query(5, ge=1, le=20, description="How many recent meetings to return"),
    top: int = Query(5, ge=1, le=25, description="How many top performances per discipline"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Two teams' record against each other across their completed matches.

    Reports the total meetings, wins for each side, the recent results (``recent``
    meetings, most recent first) as a form string, each side's highest and lowest
    innings, and the best individual batting and bowling performances from those
    matches (``top`` per discipline). Only completed matches this account owns are
    counted, and each winner is derived with the same rules as the match summary.
    """
    if team_id == opponent_id:
        raise HTTPException(400, "A team cannot play itself.")
    team = await get_team(db, team_id, current_user.id)
    if not team:
        raise HTTPException(404, "Team not found")
    opponent = await get_team(db, opponent_id, current_user.id)
    if not opponent:
        raise HTTPException(404, "Opponent team not found")
    payload = await get_head_to_head_analytics(
        db, team, opponent, current_user.id, recent=recent, top=top
    )
    return HeadToHeadAnalyticsResponse.model_validate(payload)


@router.put(
    "/teams/{team_id}/captains",
    response_model=TeamCaptainsResponse,
    responses={"400": BAD_REQUEST},
    summary="Name or clear the captaincy",
    description=(
        "Both ids are optional in the request: sending `null` for one clears that "
        "title, so a squad can be left with neither. Both must be different people "
        "and both must already be in the playing XI. The response always carries "
        "both ids - `null` meaning that title is currently unset."
    ),
)
async def set_team_captains_endpoint(
    team_id: int,
    data: TeamCaptainsSet,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Ownership is resolved here rather than inside the helper, so a missing team
    # is a 404 and the helper's remaining failures - all of them squad-rule
    # violations - are 400s. The helper's error message is its success message
    # too, which is why the route checks the *result* for failure and never the
    # message: checking the message made every successful request a 400.
    if await get_team(db, team_id, current_user.id) is None:
        raise HTTPException(404, "Team not found")

    captains, message = await set_team_captains(
        db,
        team_id,
        data.captain_player_id,
        data.vice_captain_player_id,
        current_user.id,
    )
    if captains is None:
        raise HTTPException(400, message)
    captain_id, vice_id = captains
    return TeamCaptainsResponse(
        message=message,
        team_id=team_id,
        captain_player_id=captain_id,
        vice_captain_player_id=vice_id,
    )


@router.put(
    "/teams/{team_id}",
    response_model=TeamResponse,
    deprecated=True,
    summary="Replace a team (deprecated)",
    description=(
        "Deprecated: use `PATCH /teams/{team_id}`. This takes the full "
        "`TeamCreate` body, so changing one field means resending all ten or "
        "taking a 422 listing the nine you left out."
    ),
)
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


@router.patch("/teams/{team_id}", response_model=TeamResponse, responses={"409": CONFLICT})
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


@router.post(
    "/teams/{team_id}/upload-logo",
    response_model=TeamLogoResponse,
    responses={"400": BAD_REQUEST},
)
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
        key = storage.image_key("teams", team_id, file.content_type or "")
        await storage.put_image(key, await file.read(), file.content_type or "")
    except storage.InvalidImage as exc:
        raise HTTPException(400, str(exc)) from exc

    # A root-relative path is stored, not the bare key: browsers resolve it
    # against the page, so a relative value breaks on nested routes like
    # "/v1/teams/17". "/uploads/..." is routable from anywhere and is also where
    # the CDN redirect lives.
    team.logo = f"/uploads/{key}"
    await db.commit()
    await db.refresh(team)
    return TeamLogoResponse(team_id=team.id, logo=team.logo)


@router.get(
    "/teams/{team_id}/available-players",
    response_model=list[PlayerDropdownItem],
    responses=paginated_list_response(
        "One page of this account's players who are not on a squad at this level.",
        {"404": NOT_FOUND},
    ),
)
async def get_available_players_endpoint(
    team_id: int,
    q: str | None = Query(None, description="Search by first or last name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    players, error, total = await get_available_players_for_team(
        db, team_id, user_id=current_user.id, q=q, skip=skip, limit=limit
    )
    if error:
        raise HTTPException(404, error)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return players


@router.post(
    "/teams/{team_id}/players",
    status_code=201,
    response_model=TeamPlayerAddResponse,
    responses={"400": BAD_REQUEST},
)
async def add_player_to_team_by_phone_endpoint(
    team_id: int,
    data: TeamPlayerByPhone,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add one player to a squad by typing the number registered to them.

    A number may be shared, so an ambiguous match is refused with the candidates
    and their codes rather than one being picked. Add by id in that case.
    """
    assignment, error = await assign_player_to_team_by_phone(
        db, team_id, data.country_code, data.mobile_number, data.role, user_id=current_user.id
    )
    if error:
        raise HTTPException(400, error)
    return TeamPlayerAddResponse(message="Player added to team successfully", assignment=assignment)


@router.post(
    "/teams/{team_id}/players/bulk",
    response_model=PlayerBulkAssignResponse,
    responses={"400": BAD_REQUEST},
)
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
