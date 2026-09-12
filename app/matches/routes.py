from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.get("/matches", response_model=list[MatchResponse])
async def list_matches(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    return await get_matches(db, skip=skip, limit=limit)


@router.post("/matches", response_model=MatchResponse, status_code=201)
async def create_match_endpoint(
    data: MatchCreate,
    db: AsyncSession = Depends(get_db),
):
    match, error = await create_match(db, data)
    if error:
        raise HTTPException(400, error)
    return match


@router.get("/matches/{match_id}", response_model=MatchResponse)
async def get_match_endpoint(
    match_id: int,
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id)
    if not match:
        raise HTTPException(404, "Match not found")
    return match


@router.put("/matches/{match_id}", response_model=MatchResponse)
async def replace_match_endpoint(
    match_id: int,
    data: MatchCreate,
    db: AsyncSession = Depends(get_db),
):
    match, error = await replace_match(db, match_id, data)
    if error:
        raise HTTPException(400, error)
    return match


@router.patch("/matches/{match_id}", response_model=MatchResponse)
async def update_match_endpoint(
    match_id: int,
    data: MatchUpdate,
    db: AsyncSession = Depends(get_db),
):
    match, error = await update_match(db, match_id, data)
    if error:
        if "not found" in error.lower():
            raise HTTPException(404, error)
        raise HTTPException(400, error)
    return match


@router.delete("/matches/{match_id}", status_code=204)
async def delete_match_endpoint(
    match_id: int,
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_match(db, match_id)
    if not deleted:
        raise HTTPException(404, "Match not found")
