from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func
from app.players.models import Player
from app.teams.models import PlayerTeamAssignment, Team
from app.levels.models import TeamLevel
from app.players.schemas import PlayerCreate, PlayerUpdate, TeamAssignment
from app.models import OTP
from app.sms import send_otp

MAX_SQUAD_SIZE = 15


async def get_players(db: AsyncSession, skip: int = 0, limit: int = 100):
    result = await db.execute(
        select(Player).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def get_player(db: AsyncSession, player_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id)
    )
    return result.scalar_one_or_none()


async def get_player_by_phone(db: AsyncSession, country_code: str, mobile_number: int):
    result = await db.execute(
        select(Player).where(
            Player.country_code == country_code,
            Player.mobile_number == mobile_number,
        )
    )
    return result.scalar_one_or_none()


async def search_players(db: AsyncSession, query: str, skip: int = 0, limit: int = 100):
    result = await db.execute(
        select(Player).where(
            or_(
                Player.first_name.ilike(f"%{query}%"),
                Player.last_name.ilike(f"%{query}%"),
            )
        ).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def get_unassigned_players(db: AsyncSession, skip: int = 0, limit: int = 100):
    subq = select(PlayerTeamAssignment.player_id).distinct().scalar_subquery()
    result = await db.execute(
        select(Player).where(Player.id.notin_(subq)).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def _create_otp_record(db: AsyncSession, country_code: str, mobile_number: int):
    otp_sent, session_info = await send_otp(country_code, mobile_number)
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    otp = OTP(
        country_code=country_code,
        mobile_number=mobile_number,
        otp_code="",
        session_id=session_info,
        expires_at=expires_at,
    )
    db.add(otp)
    await db.flush()
    return otp, otp_sent


async def create_player(db: AsyncSession, data: PlayerCreate):
    existing = await get_player_by_phone(db, data.country_code, data.mobile_number)
    if existing:
        raise ValueError("A player with this phone number already exists")

    player = Player(**data.model_dump())
    db.add(player)
    await db.flush()

    _, otp_sent = await _create_otp_record(db, data.country_code, data.mobile_number)

    await db.commit()
    await db.refresh(player)
    return player, otp_sent


async def resend_otp_for_player(db: AsyncSession, player_id: int):
    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if not player:
        return None, False

    _, otp_sent = await _create_otp_record(db, player.country_code, player.mobile_number)

    await db.commit()
    return player, otp_sent


async def update_player(db: AsyncSession, player_id: int, data: PlayerUpdate):
    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if not player:
        return None
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(player, key, val)
    await db.commit()
    await db.refresh(player)
    return player


async def replace_player(db: AsyncSession, player_id: int, data: PlayerCreate):
    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if not player:
        return None
    for key, val in data.model_dump().items():
        setattr(player, key, val)
    await db.commit()
    await db.refresh(player)
    return player


async def delete_player(db: AsyncSession, player_id: int):
    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if not player:
        return False
    await db.delete(player)
    await db.commit()
    return True


async def mark_player_phone_verified(
    db: AsyncSession,
    country_code: str,
    mobile_number: int,
):
    result = await db.execute(
        select(Player).where(
            Player.country_code == country_code,
            Player.mobile_number == mobile_number,
        )
    )
    player = result.scalar_one_or_none()
    if player:
        player.is_phone_verified = True
        await db.commit()
        return True
    return False


async def assign_player_to_team(db: AsyncSession, player_id: int, data: TeamAssignment):
    player_result = await db.execute(select(Player).where(Player.id == player_id))
    player = player_result.scalar_one_or_none()
    if not player:
        return None, "Player not found"

    team_result = await db.execute(select(Team).where(Team.id == data.team_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found"

    level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == data.level_id))
    level = level_result.scalar_one_or_none()
    if not level:
        return None, "Level not found"

    existing = await db.execute(
        select(PlayerTeamAssignment).where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.level_id == data.level_id,
        )
    )
    existing_assignment = existing.scalar_one_or_none()
    if existing_assignment:
        existing_team = await db.execute(select(Team).where(Team.id == existing_assignment.team_id))
        existing_team_name = existing_team.scalar_one_or_none()
        team_name = existing_team_name.name if existing_team_name else "another team"
        return None, f"Player already has a team at '{level.name}' level: '{team_name}'"

    squad_count = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id))
        .where(
            PlayerTeamAssignment.team_id == data.team_id,
            PlayerTeamAssignment.level_id == data.level_id,
        )
    )
    count = squad_count.scalar()
    if count >= MAX_SQUAD_SIZE:
        return None, f"Team squad is full (max {MAX_SQUAD_SIZE} players)"

    assignment = PlayerTeamAssignment(
        player_id=player_id,
        team_id=data.team_id,
        level_id=data.level_id,
        role=data.role,
    )
    db.add(assignment)
    await db.commit()
    return assignment, None


async def get_player_teams(db: AsyncSession, player_id: int):
    result = await db.execute(
        select(PlayerTeamAssignment)
        .where(PlayerTeamAssignment.player_id == player_id)
    )
    assignments = result.scalars().all()

    teams = []
    for a in assignments:
        team_result = await db.execute(select(Team).where(Team.id == a.team_id))
        team = team_result.scalar_one_or_none()
        level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == a.level_id))
        level = level_result.scalar_one_or_none()

        teams.append({
            "team_id": a.team_id,
            "team_name": team.name if team else "Unknown",
            "level_id": a.level_id,
            "level_name": level.name if level else "Unknown",
            "role": a.role,
        })

    return teams


async def update_player_team_role(db: AsyncSession, player_id: int, team_id: int, role: str):
    result = await db.execute(
        select(PlayerTeamAssignment).where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.team_id == team_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        return None, "Assignment not found"

    assignment.role = role
    await db.commit()
    return assignment, None


async def remove_player_from_team(db: AsyncSession, player_id: int, team_id: int):
    result = await db.execute(
        select(PlayerTeamAssignment).where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.team_id == team_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        return False
    await db.delete(assignment)
    await db.commit()
    return True
