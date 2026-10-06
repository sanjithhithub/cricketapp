from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.players.identity import mask_mobile
from app.teams.models import PlayerTeamAssignment, Team
from app.teams.schemas import SquadPlayer, TeamCreate, TeamUpdate

MAX_SQUAD_SIZE = 15


def _squad_row(a: PlayerTeamAssignment) -> SquadPlayer:
    """Build the squad row for one assignment.

    The masked mobile and the verification flag travel with every row because a
    squad is rendered as pickers: the number is what tells two same-named
    players apart, and the flag is what tells a scorer the number is unproven
    and worth verifying before a delivery is charged to it. Captaincy rides along
    so the picker can show who leads the side without a second request.
    """
    player = a.player
    return SquadPlayer(
        id=player.id,
        player_code=player.player_code,
        first_name=player.first_name,
        last_name=player.last_name,
        full_name=f"{player.first_name} {player.last_name}".strip(),
        profile_image=player.profile_image,
        role=a.role,
        mobile_number=mask_mobile(player.country_code, player.mobile_number),
        is_phone_verified=bool(player.is_phone_verified),
        is_captain=bool(a.is_captain),
        is_vice_captain=bool(a.is_vice_captain),
    )


async def set_team_captains(
    db: AsyncSession,
    team_id: int,
    captain_player_id: int | None,
    vice_captain_player_id: int | None,
    user_id: int,
):
    """Name (or clear) a team's captain and vice-captain.

    Returns ``(captains, message)``. When ``captains`` is ``None`` nothing was
    written and ``message`` is the reason; when it is a tuple, ``message`` is a
    human-readable summary of what was applied. That asymmetry is the point: the
    two used to be returned as ``(result, error)`` with the message second, which
    reads exactly like an error on every call and turned every successful request
    into a 400 at the route.

    A rejected request never leaves a half-applied captaincy behind.

    The rules, and why each exists:

    * the same person cannot hold both titles - a side does not have one player
      as both captain and vice-captain;
    * each must already be on the squad;
    * each must be in the playing XI, because a substitute or benched player
      does not lead the side out on the field;
    * both are optional, so a squad can be left with neither. Sending ``None``
      clears that title rather than failing.

    Clearing is done by resetting every flag on the team first, so naming a new
    captain cannot leave the previous one still flagged.
    """
    team_result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    if team_result.scalar_one_or_none() is None:
        return None, "Team not found"

    if (
        captain_player_id is not None
        and vice_captain_player_id is not None
        and captain_player_id == vice_captain_player_id
    ):
        return None, "The captain and vice-captain must be two different players"

    wanted = [pid for pid in (captain_player_id, vice_captain_player_id) if pid is not None]
    assignments: dict[int, PlayerTeamAssignment] = {}
    if wanted:
        rows = await db.execute(
            select(PlayerTeamAssignment).where(
                PlayerTeamAssignment.team_id == team_id,
                PlayerTeamAssignment.player_id.in_(wanted),
            )
        )
        assignments = {a.player_id: a for a in rows.scalars().all()}
        missing = [pid for pid in wanted if pid not in assignments]
        if missing:
            return None, f"Player {missing[0]} is not on this team's squad"
        off_xi = next(
            (a for a in assignments.values() if a.role != "playing_11"),
            None,
        )
        if off_xi is not None:
            return None, (
                f"Player {off_xi.player_id} is a {off_xi.role}, not in the playing XI. "
                "The captain and vice-captain must both be in the XI."
            )

    await db.execute(
        update(PlayerTeamAssignment)
        .where(PlayerTeamAssignment.team_id == team_id)
        .values(is_captain=False, is_vice_captain=False)
    )
    if captain_player_id is not None:
        assignments[captain_player_id].is_captain = True
    if vice_captain_player_id is not None:
        assignments[vice_captain_player_id].is_vice_captain = True

    await db.commit()

    named = []
    if captain_player_id is not None:
        named.append(f"captain: player {captain_player_id}")
    if vice_captain_player_id is not None:
        named.append(f"vice-captain: player {vice_captain_player_id}")
    return (captain_player_id, vice_captain_player_id), (
        f"Updated {' and '.join(named)}" if named else "Cleared the captain and vice-captain"
    )


async def get_teams(
    db: AsyncSession,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    level_id: int | None = None,
) -> tuple[list[dict], int]:
    """One page of this account's teams, and the total that page is drawn from."""
    where = [Team.user_id == user_id]
    if level_id is not None:
        where.append(Team.level_id == level_id)

    total = int((await db.execute(select(func.count(Team.id)).where(*where))).scalar() or 0)

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
        .where(*where)
    )
    stmt = stmt.offset(skip).limit(limit)

    result = await db.execute(stmt)
    rows = result.all()

    teams = []
    for team, total_players, playing_11, substitutes, bench in rows:
        teams.append(
            {
                "id": team.id,
                "name": team.name,
                "short_name": team.short_name,
                "logo": team.logo,
                "level": team.level.name if team.level else None,
                "total_players": total_players,
                "playing_11": playing_11,
                "substitutes": substitutes,
                "bench": bench,
            }
        )
    return teams, total


async def get_team_options(db: AsyncSession, user_id: int, level_id: int | None = None):
    query = select(Team)
    query = query.where(Team.user_id == user_id)
    if level_id is not None:
        query = query.where(Team.level_id == level_id)
    query = query.order_by(Team.id)
    result = await db.execute(query)
    return result.scalars().all()


async def get_team(db: AsyncSession, team_id: int, user_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    return result.scalar_one_or_none()


async def get_team_detail(db: AsyncSession, team_id: int, user_id: int):
    result = await db.execute(
        select(Team)
        .options(
            selectinload(Team.level),
            selectinload(Team.country),
            selectinload(Team.state),
            selectinload(Team.city),
        )
        .where(Team.id == team_id, Team.user_id == user_id)
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
        player_data = _squad_row(a)
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


async def get_team_squad(db: AsyncSession, team_id: int, user_id: int):
    result = await db.execute(
        select(Team)
        .options(selectinload(Team.level))
        .where(Team.id == team_id, Team.user_id == user_id)
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
        player_data = _squad_row(a)
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


async def get_team_squad_count(db: AsyncSession, team_id: int, user_id: int):
    team_exists = await get_team(db, team_id, user_id)
    if not team_exists:
        return None
    result = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == team_id
        )
    )
    return result.scalar()


async def _load_team_players(db: AsyncSession, team_id: int, user_id: int):
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
        player_data = _squad_row(a)
        if a.role == "playing_11":
            playing_11.append(player_data)
        elif a.role == "bench":
            bench.append(player_data)
        else:
            substitutes.append(player_data)

    return assignments, playing_11, substitutes, bench


async def create_team(db: AsyncSession, data: TeamCreate, user_id: int):
    team = Team(**data.model_dump(), user_id=user_id)
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return team


async def update_team(db: AsyncSession, team_id: int, data: TeamUpdate, user_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(team, key, val)
    await db.commit()
    return team


async def replace_team(db: AsyncSession, team_id: int, data: TeamCreate, user_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    for key, val in data.model_dump().items():
        setattr(team, key, val)
    await db.commit()
    return team


async def delete_team(db: AsyncSession, team_id: int, user_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = result.scalar_one_or_none()
    if not team:
        return False
    await db.delete(team)
    await db.commit()
    return True


async def update_team_logo(db: AsyncSession, team_id: int, logo_path: str, user_id: int):
    result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = result.scalar_one_or_none()
    if not team:
        return None
    team.logo = logo_path
    await db.commit()
    await db.refresh(team)
    return team
