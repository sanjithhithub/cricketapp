from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.levels.models import TeamLevel
from app.levels.schemas import LevelCreate


async def get_levels(db: AsyncSession):
    result = await db.execute(select(TeamLevel))
    return result.scalars().all()


async def get_level(db: AsyncSession, level_id: int):
    result = await db.execute(select(TeamLevel).where(TeamLevel.id == level_id))
    return result.scalar_one_or_none()


async def create_level(db: AsyncSession, data: LevelCreate):
    level = TeamLevel(**data.model_dump())
    db.add(level)
    await db.commit()
    await db.refresh(level)
    return level


async def delete_level(db: AsyncSession, level_id: int):
    result = await db.execute(select(TeamLevel).where(TeamLevel.id == level_id))
    level = result.scalar_one_or_none()
    if not level:
        return False
    await db.delete(level)
    await db.commit()
    return True
