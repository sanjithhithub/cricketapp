from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from app.players.models import Player
from app.players.schemas import PlayerCreate, PlayerUpdate
from app.models import OTP
from app.sms import send_otp
import random


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
    result = await db.execute(
        select(Player).where(Player.team_id.is_(None)).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def _create_otp_record(db: AsyncSession, country_code: str, mobile_number: int):
    otp_code = f"{random.randint(100000, 999999)}"
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    otp_sent = await send_otp(country_code, mobile_number, otp_code)
    otp = OTP(
        country_code=country_code,
        mobile_number=mobile_number,
        otp_code=otp_code,
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
