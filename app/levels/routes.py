from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.levels.schemas import LevelCreate, LevelResponse
from app.levels.crud import get_levels, get_level, create_level, delete_level

router = APIRouter(tags=["levels"])


@router.get("/team-levels", response_model=list[LevelResponse])
async def list_levels(db: AsyncSession = Depends(get_db)):
    return await get_levels(db)


@router.get("/team-levels/{level_id}", response_model=LevelResponse)
async def get_level_endpoint(level_id: int, db: AsyncSession = Depends(get_db)):
    level = await get_level(db, level_id)
    if not level:
        raise HTTPException(404, "Level not found")
    return level


@router.post("/team-levels", response_model=LevelResponse, status_code=201)
async def create_level_endpoint(data: LevelCreate, db: AsyncSession = Depends(get_db)):
    return await create_level(db, data)


@router.delete("/team-levels/{level_id}", status_code=204)
async def delete_level_endpoint(level_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await delete_level(db, level_id)
    if not deleted:
        raise HTTPException(404, "Level not found")
