from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import OTP, Country, State
from app.players.identity import normalize_country_code
from app.sms import verify_otp
from app.timeutils import utcnow


async def get_all_countries(db: AsyncSession):
    result = await db.execute(
        select(Country).options(selectinload(Country.states).selectinload(State.cities))
    )
    return result.scalars().all()


async def get_country_codes():
    return [
        {"code": "+91", "name": "India"},
        {"code": "+1", "name": "USA/Canada"},
        {"code": "+44", "name": "United Kingdom"},
        {"code": "+61", "name": "Australia"},
        {"code": "+7", "name": "Russia"},
        {"code": "+81", "name": "Japan"},
        {"code": "+86", "name": "China"},
        {"code": "+49", "name": "Germany"},
        {"code": "+33", "name": "France"},
        {"code": "+39", "name": "Italy"},
        {"code": "+34", "name": "Spain"},
        {"code": "+55", "name": "Brazil"},
        {"code": "+27", "name": "South Africa"},
        {"code": "+82", "name": "South Korea"},
        {"code": "+65", "name": "Singapore"},
        {"code": "+60", "name": "Malaysia"},
        {"code": "+63", "name": "Philippines"},
        {"code": "+92", "name": "Pakistan"},
        {"code": "+94", "name": "Sri Lanka"},
        {"code": "+971", "name": "UAE"},
        {"code": "+966", "name": "Saudi Arabia"},
        {"code": "+62", "name": "Indonesia"},
        {"code": "+64", "name": "New Zealand"},
        {"code": "+31", "name": "Netherlands"},
        {"code": "+46", "name": "Sweden"},
        {"code": "+47", "name": "Norway"},
        {"code": "+45", "name": "Denmark"},
        {"code": "+41", "name": "Switzerland"},
    ]


async def verify_otp_code(
    db: AsyncSession,
    country_code: str,
    mobile_number: str | int,
    otp_code: str,
):
    now = utcnow()
    result = await db.execute(
        select(OTP)
        .where(
            OTP.country_code == normalize_country_code(country_code),
            OTP.mobile_number == str(mobile_number or ""),
            OTP.is_verified.is_(False),
            OTP.expires_at > now,
        )
        .order_by(OTP.created_at.desc())
        .limit(1)
    )
    otp = result.scalar_one_or_none()
    if not otp:
        return False

    if not otp.session_id:
        return False

    valid = await verify_otp(otp.session_id, otp_code)
    if not valid:
        return False

    otp.is_verified = True
    await db.commit()
    return True
