from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models import Country, State, City, OTP


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
    mobile_number: int,
    otp_code: str,
):
    now = datetime.utcnow()
    result = await db.execute(
        select(OTP).where(
            OTP.country_code == country_code,
            OTP.mobile_number == mobile_number,
            OTP.is_verified == False,
            OTP.expires_at > now,
        ).order_by(OTP.created_at.desc()).limit(1)
    )
    otp = result.scalar_one_or_none()
    if not otp:
        return False

    if otp.otp_code != otp_code:
        return False

    otp.is_verified = True
    await db.commit()
    return True
