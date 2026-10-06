from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST, paginated_list_response, set_pagination_headers
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.matches.crud import (
    create_match,
    delete_match,
    get_match,
    get_matches,
    replace_match,
    update_match,
)
from app.matches.schemas import MatchCreate, MatchResponse, MatchUpdate

router = APIRouter(tags=["matches"])


@router.get(
    "/matches",
    response_model=list[MatchResponse],
    responses=paginated_list_response("One page of this account's matches."),
)
async def list_matches(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    matches, total = await get_matches(db, user_id=current_user.id, skip=skip, limit=limit)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return matches


@router.post(
    "/matches",
    response_model=MatchResponse,
    status_code=201,
    responses={"400": BAD_REQUEST},
)
async def create_match_endpoint(
    data: MatchCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match, error = await create_match(db, data, current_user.id)
    if error:
        # A named team does not exist, is not this account's, or the toss winner is
        # neither side. Well formed, but not something the server will carry out.
        raise HTTPException(400, error)
    return match


@router.get("/matches/{match_id}", response_model=MatchResponse)
async def get_match_endpoint(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Match not found")
    return match


@router.put(
    "/matches/{match_id}",
    response_model=MatchResponse,
    deprecated=True,
    summary="Replace a match (deprecated)",
    description=(
        "Deprecated: use `PATCH /matches/{match_id}`. This takes the full "
        "`MatchCreate` body, so changing one field means resending all of them."
    ),
    responses={"400": BAD_REQUEST},
)
async def replace_match_endpoint(
    match_id: int,
    data: MatchCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full replacement of every mutable field on a match. Prefer PATCH."""
    match, error = await replace_match(db, match_id, data, current_user.id)
    if error:
        raise HTTPException(400, error)
    return match


@router.patch("/matches/{match_id}", response_model=MatchResponse, responses={"400": BAD_REQUEST})
async def update_match_endpoint(
    match_id: int,
    data: MatchUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match, error = await update_match(db, match_id, data, current_user.id)
    if error:
        if "not found" in error.lower():
            raise HTTPException(404, error)
        raise HTTPException(400, error)
    return match


@router.delete("/matches/{match_id}", status_code=204)
async def delete_match_endpoint(
    match_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_match(db, match_id, current_user.id)
    if not deleted:
        raise HTTPException(404, "Match not found")
