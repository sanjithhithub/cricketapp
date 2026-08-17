from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.teams.models import Team
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
        return None, "Team not found"

    player_result = await db.execute(
        select(Player).where(
            Player.country_code == country_code,
            Player.mobile_number == mobile_number,
        )
    )
    player = player_result.scalar_one_or_none()
    if not player:
        return None, "Player not found"

    if player.team_id is not None:
        if player.team_id == team_id:
            return player, None
        existing_team = await db.execute(select(Team).where(Team.id == player.team_id))
        existing_team_name = existing_team.scalar_one_or_none()
        team_name = existing_team_name.name if existing_team_name else "another team"
        return None, f"Player already belongs to team '{team_name}'"

    player.team_id = team_id
    await db.commit()
    await db.refresh(player)
    return player, None


async def add_player_by_id(
    db: AsyncSession,
    team_id: int,
    player_id: int,
):
    team_result = await db.execute(select(Team).where(Team.id == team_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found"

    player_result = await db.execute(select(Player).where(Player.id == player_id))
    player = player_result.scalar_one_or_none()
    if not player:
        return None, "Player not found"

    if player.team_id is not None:
        if player.team_id == team_id:
            return player, None
        existing_team = await db.execute(select(Team).where(Team.id == player.team_id))
        existing_team_name = existing_team.scalar_one_or_none()
        team_name = existing_team_name.name if existing_team_name else "another team"
        return None, f"Player already belongs to team '{team_name}'"

    player.team_id = team_id
    await db.commit()
    await db.refresh(player)
    return player, None


async def bulk_add_players(
    db: AsyncSession,
    team_id: int,
    player_ids: list[int],
):
    team_result = await db.execute(select(Team).where(Team.id == team_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found"

    added = []
    errors = []
    for pid in player_ids:
        player_result = await db.execute(select(Player).where(Player.id == pid))
        player = player_result.scalar_one_or_none()
        if not player:
            errors.append(f"Player {pid} not found")
            continue
        if player.team_id is not None:
            if player.team_id == team_id:
                added.append(player)
                continue
            existing_team = await db.execute(select(Team).where(Team.id == player.team_id))
            existing_team_name = existing_team.scalar_one_or_none()
            team_name = existing_team_name.name if existing_team_name else "another team"
            errors.append(f"Player '{player.first_name} {player.last_name}' already belongs to team '{team_name}'")
            continue
        player.team_id = team_id
        added.append(player)

    await db.commit()
    return added, errors


async def remove_player_from_team(db: AsyncSession, team_id: int, player_id: int):
    player_result = await db.execute(
        select(Player).where(Player.id == player_id, Player.team_id == team_id)
    )
    player = player_result.scalar_one_or_none()
    if not player:
        return False

    player.team_id = None
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
