from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.teams.models import PlayerTeamAssignment, Team
from app.teams.schemas import SquadPlayer, TeamCreate, TeamUpdate

MAX_SQUAD_SIZE = 15


async def get_teams(db: AsyncSession, skip: int = 0, limit: int = 100, level_id: int | None = None):
    stmt = (
        select(
            Team,
            func.count(PlayerTeamAssignment.player_id),
            func.coalesce(
                func.sum(case((PlayerTeamAssignment.role == "playing_11", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((PlayerTeamAssignment.role == "substitute", 1), else_=0)), 0
            ),
            func.coalesce(func.sum(case((PlayerTeamAssignment.role == "bench", 1), else_=0)), 0),
        )
        .options(selectinload(Team.level))
        .outerjoin(PlayerTeamAssignment, PlayerTeamAssignment.team_id == Team.id)
        .group_by(Team.id)
        .order_by(Team.id)
    )
    if level_id is not None:
        stmt = stmt.where(Team.level_id == level_id)
    stmt = stmt.offset(skip).limit(limit)

    result = await db.execute(stmt)
    rows = result.all()

    teams = []
    for team, total, playing_11, substitutes, bench in rows:
        teams.append(
            {
                "id": team.id,
                "name": team.name,
                "short_name": team.short_name,
                "logo": team.logo,
                "level": team.level.name if team.level else None,
                "total_players": total,
                "playing_11": playing_11,
                "substitutes": substitutes,
                "bench": bench,
            }
        )
    return teams


async def get_team_options(db: AsyncSession, level_id: int | None = None):
    query = select(Team)
    if level_id is not None:
        query = query.where(Team.level_id == level_id)
    query = query.order_by(Team.id)
    result = await db.execute(query)
    return result.scalars().all()


async def get_team(db: AsyncSession, team_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id))
    return result.scalar_one_or_none()


async def get_team_detail(db: AsyncSession, team_id: int):
    result = await db.execute(
        select(Team)
        .options(
            selectinload(Team.level),
            selectinload(Team.country),
            selectinload(Team.state),
            selectinload(Team.city),
        )
        .where(Team.id == team_id)
    )
    team = result.scalar_one_or_none()
    if not team:
        return None

    assignments_result = await db.execute(
        select(PlayerTeamAssignment)
        .options(selectinload(PlayerTeamAssignment.player))
        .where(PlayerTeamAssignment.team_id == team_id)
    )
    assignments = assignments_result.scalars().all()

    playing_11 = []
    substitutes = []
    bench = []
    for a in assignments:
        player_data = SquadPlayer(
            id=a.player.id,
            first_name=a.player.first_name,
            last_name=a.player.last_name,
            profile_image=a.player.profile_image,
            role=a.role,
        )
        if a.role == "playing_11":
            playing_11.append(player_data)
        elif a.role == "bench":
            bench.append(player_data)
        else:
            substitutes.append(player_data)

    return {
        "id": team.id,
        "name": team.name,
        "short_name": team.short_name,
        "logo": team.logo,
        "homeground": team.homeground,
        "founder": team.founder,
        "founded_year": team.founded_year,
        "owner": team.owner,
        "country_id": team.country_id,
        "state_id": team.state_id,
        "city_id": team.city_id,
        "level_id": team.level_id,
        "country": team.country.name if team.country else None,
        "state": team.state.name if team.state else None,
        "city": team.city.name if team.city else None,
        "level": team.level.name if team.level else None,
        "total": len(assignments),
        "playing_11": playing_11,
        "substitutes": substitutes,
        "bench": bench,
    }


async def get_team_squad(db: AsyncSession, team_id: int):
    result = await db.execute(
        select(Team).options(selectinload(Team.level)).where(Team.id == team_id)
    )
    team = result.scalar_one_or_none()
    if not team:
        return None

    assignments_result = await db.execute(
        select(PlayerTeamAssignment)
        .options(selectinload(PlayerTeamAssignment.player))
        .where(PlayerTeamAssignment.team_id == team_id)
    )
    assignments = assignments_result.scalars().all()

    playing_11 = []
    substitutes = []
    bench = []
    for a in assignments:
        player_data = SquadPlayer(
            id=a.player.id,
            first_name=a.player.first_name,
            last_name=a.player.last_name,
            profile_image=a.player.profile_image,
            role=a.role,
        )
        if a.role == "playing_11":
            playing_11.append(player_data)
        elif a.role == "bench":
            bench.append(player_data)
        else:
            substitutes.append(player_data)

    return {
        "team_id": team.id,
        "team_name": team.name,
        "level": team.level.name if team.level else "",
        "total": len(assignments),
        "playing_11": playing_11,
        "substitutes": substitutes,
        "bench": bench,
    }


async def get_team_squad_count(db: AsyncSession, team_id: int):
    result = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == team_id
        )
    )
    return result.scalar()


async def _load_team_players(db: AsyncSession, team_id: int):
    assignments_result = await db.execute(
        select(PlayerTeamAssignment)
        .options(selectinload(PlayerTeamAssignment.player))
        .where(PlayerTeamAssignment.team_id == team_id)
    )
    assignments = assignments_result.scalars().all()

    playing_11 = []
    substitutes = []
    bench = []
    for a in assignments:
        player_data = SquadPlayer(
            id=a.player.id,
            first_name=a.player.first_name,
            last_name=a.player.last_name,
            profile_image=a.player.profile_image,
            role=a.role,
        )
        if a.role == "playing_11":
            playing_11.append(player_data)
        elif a.role == "bench":
            bench.append(player_data)
        else:
            substitutes.append(player_data)

    return assignments, playing_11, substitutes, bench


async def create_team(db: AsyncSession, data: TeamCreate):
    team = Team(**data.model_dump())
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return team


async def update_team(db: AsyncSession, team_id: int, data: TeamUpdate):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(team, key, val)
    await db.commit()
    return team


async def replace_team(db: AsyncSession, team_id: int, data: TeamCreate):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump().items():
        setattr(team, key, val)
    await db.commit()
    return team


async def delete_team(db: AsyncSession, team_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        return False
    await db.delete(team)
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
