from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.teams.models import Team, team_players
from app.players.models import Player
from app.teams.schemas import TeamCreate, TeamUpdate


async def get_teams(db: AsyncSession, skip: int = 0, limit: int = 100):
    result = await db.execute(
        select(Team).options(selectinload(Team.players)).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def get_team(db: AsyncSession, team_id: int):
    result = await db.execute(
        select(Team).options(selectinload(Team.players)).where(Team.id == team_id)
    )
    return result.scalar_one_or_none()


async def create_team(db: AsyncSession, data: TeamCreate):
    team = Team(**data.model_dump())
    db.add(team)
    await db.commit()
    await db.refresh(team)
    result = await db.execute(
        select(Team).options(selectinload(Team.players)).where(Team.id == team.id)
    )
    return result.scalar_one()


async def update_team(db: AsyncSession, team_id: int, data: TeamUpdate):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(team, key, val)
    await db.commit()
    result = await db.execute(
        select(Team).options(selectinload(Team.players)).where(Team.id == team_id)
    )
    return result.scalar_one_or_none()


async def replace_team(db: AsyncSession, team_id: int, data: TeamCreate):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump().items():
        setattr(team, key, val)
    await db.commit()
    result = await db.execute(
        select(Team).options(selectinload(Team.players)).where(Team.id == team_id)
    )
    return result.scalar_one_or_none()


async def delete_team(db: AsyncSession, team_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return False
    await db.delete(team)
    await db.commit()
    return True


async def add_player_to_team(
    db: AsyncSession,
    team_id: int,
    country_code: str,
    mobile_number: int,
):
    team_result = await db.execute(select(Team).where(Team.id == team_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None

    player_result = await db.execute(
        select(Player).where(
            Player.country_code == country_code,
            Player.mobile_number == mobile_number,
        )
    )
    player = player_result.scalar_one_or_none()
    if not player:
        return None

    existing = await db.execute(
        select(team_players).where(
            team_players.c.team_id == team_id,
            team_players.c.player_id == player.id,
        )
    )
    if existing.first():
        return player

    await db.execute(
        team_players.insert().values(team_id=team_id, player_id=player.id)
    )
    await db.commit()
    return player


async def remove_player_from_team(db: AsyncSession, team_id: int, player_id: int):
    existing = await db.execute(
        select(team_players).where(
            team_players.c.team_id == team_id,
            team_players.c.player_id == player_id,
        )
    )
    if not existing.first():
        return False
    await db.execute(
        team_players.delete().where(
            team_players.c.team_id == team_id,
            team_players.c.player_id == player_id,
        )
    )
    await db.commit()
    return True


async def update_team_logo(db: AsyncSession, team_id: int, logo_path: str):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    team.logo = logo_path
    await db.commit()
    await db.refresh(team)
    return team
