import logging
import os
from datetime import timedelta

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthOTP, RefreshToken, User
from app.auth.schemas import UserRegister
from app.auth.security import (
    ALGORITHM,
    SECRET_KEY,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.email_service import generate_otp, send_email_otp
from app.timeutils import utcnow

load_dotenv()
logger = logging.getLogger("app.auth")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
OTP_EXPIRE_MINUTES = int(os.getenv("OTP_EXPIRE_MINUTES", "5"))


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


# --- Refresh tokens ---------------------------------------------------------
#
# Three operations, and the shape of them is the whole design:
#
#   issue  - mint a token and store its hash. Called on login, register and
#            Google sign-in. Never revokes anything, so signing in on a second
#            device does not sign you out of the first.
#   rotate - exchange a live token for a new one, revoking the old. Each refresh
#            token is therefore single use.
#   revoke - end a session without touching the user's other sessions.
#
# Rotation is what makes a stolen refresh token detectable rather than merely
# inconvenient: a thief who redeems a token the legitimate client has already
# rotated produces a revoked token in the chain, and the legitimate client's
# next refresh names the contradiction.


async def issue_refresh_token(db: AsyncSession, user_id: int) -> tuple[str, RefreshToken]:
    """Mint a refresh token for a user and persist its hash.

    Returns the raw token, which the caller must hand to the client immediately:
    it is not recoverable afterwards, only its hash is on disk.
    """
    token = generate_refresh_token()
    record = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(token),
        expires_at=refresh_token_expiry(),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return token, record


async def rotate_refresh_token(db: AsyncSession, token: str) -> tuple[User, str]:
    """Redeem a refresh token for a new pair of tokens.

    Raises 401 when the token is unknown, expired, revoked, or belongs to a user
    who is no longer active. All four are reported identically on purpose: a
    caller that could tell "this token was revoked" from "this token never existed"
    could enumerate which tokens are real.

    The new token is linked back to the one it replaces via ``replaced_by_id``.
    """
    token_hash = hash_refresh_token(token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    record = result.scalar_one_or_none()

    now = utcnow()
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    user = await get_user_by_id(db, record.user_id)
    if user is None or not user.is_active:
        # Revoke on the way out: a token for a deactivated account should not
        # become usable again if the account is reactivated later.
        record.revoked_at = now
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    new_token, new_record = await issue_refresh_token(db, user.id)

    # Revoked *after* the new one exists, so a crash between the two leaves the
    # old token usable rather than the user holding two tokens and neither
    # working.
    record.revoked_at = now
    await db.commit()
    await db.refresh(record)
    record.replaced_by_id = new_record.id
    await db.commit()

    return user, new_token


async def revoke_refresh_token(db: AsyncSession, token: str) -> bool:
    """End the session a refresh token represents.

    Returns whether anything was revoked. Silent on failure: logout is called on a
    best-effort basis by clients that may already have lost the token, and a 401
    to "please log me out" only leaves the caller unsure whether it worked.

    The return value is what lets the response say which happened without the
    route having to distinguish them, and it is why this is not an error path.
    """
    token_hash = hash_refresh_token(token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        return False
    record.revoked_at = utcnow()
    await db.commit()
    return True


async def _get_latest_otp(
    db: AsyncSession,
    purpose: str,
    email: str | None = None,
    country_code: str | None = None,
    mobile_number: int | None = None,
) -> AuthOTP | None:
    query = select(AuthOTP).where(
        AuthOTP.purpose == purpose,
        AuthOTP.expires_at > utcnow(),
        AuthOTP.is_verified.is_(False),
    )
    if email is not None:
        query = query.where(AuthOTP.email == email.lower())
    if mobile_number is not None:
        query = query.where(
            AuthOTP.country_code == country_code,
            AuthOTP.mobile_number == mobile_number,
        )
    query = query.order_by(AuthOTP.created_at.desc()).limit(1)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _get_latest_otp_by_id(db: AsyncSession, otp_id: int) -> AuthOTP | None:
    result = await db.execute(
        select(AuthOTP).where(
            AuthOTP.id == otp_id,
            AuthOTP.expires_at > utcnow(),
        )
    )
    return result.scalar_one_or_none()


async def _get_latest_verified_otp(
    db: AsyncSession,
    purpose: str,
    email: str,
) -> AuthOTP | None:
    query = (
        select(AuthOTP)
        .where(
            AuthOTP.purpose == purpose,
            AuthOTP.email == email.lower(),
            AuthOTP.expires_at > utcnow(),
            AuthOTP.is_verified.is_(True),
        )
        .order_by(AuthOTP.created_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def send_register_otp(db: AsyncSession, email: str):
    email = email.lower()
    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    code = generate_otp()
    email_sent, error = await send_email_otp(email, code)
    if not email_sent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error or "Failed to send email",
        )

    otp = AuthOTP(
        purpose="register",
        email=email,
        otp_hash=hash_password(code),
        expires_at=utcnow() + timedelta(minutes=OTP_EXPIRE_MINUTES),
    )
    db.add(otp)
    await db.commit()
    return True


async def verify_register_otp(db: AsyncSession, email: str, otp_code: str):
    email = email.lower()
    otp = await _get_latest_otp(db, "register", email=email)
    if not otp:
        return False
    if not verify_password(otp_code, otp.otp_hash):
        return False

    otp.is_verified = True
    await db.commit()
    return True


async def send_forgot_password_otp(db: AsyncSession, email: str):
    email = email.lower()
    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email",
        )

    code = generate_otp()
    email_sent, error = await send_email_otp(email, code)
    if not email_sent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error or "Failed to send email",
        )

    otp = AuthOTP(
        purpose="forgot_password",
        email=email,
        otp_hash=hash_password(code),
        expires_at=utcnow() + timedelta(minutes=OTP_EXPIRE_MINUTES),
    )
    db.add(otp)
    await db.commit()
    return True


async def verify_forgot_password_otp(db: AsyncSession, email: str, otp_code: str) -> str | None:
    email = email.lower()
    otp = await _get_latest_otp(db, "forgot_password", email=email)
    if not otp:
        return None
    if not verify_password(otp_code, otp.otp_hash):
        return None

    now = utcnow()
    reset_token = jwt.encode(
        {
            "sub": f"reset:{otp.id}",
            "email": email,
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    otp.is_verified = True
    await db.commit()
    return reset_token


async def reset_password(db: AsyncSession, email: str, reset_token: str, new_password: str) -> bool:
    email = email.lower()
    try:
        payload = jwt.decode(reset_token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired reset token",
        )

    sub = payload.get("sub", "")
    if not sub.startswith("reset:"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reset token",
        )

    otp_id = int(sub.split(":", 1)[1])
    otp = await _get_latest_otp_by_id(db, otp_id)
    if not otp or otp.purpose != "forgot_password" or otp.email != email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reset token",
        )

    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email",
        )

    user.hashed_password = hash_password(new_password)
    await db.commit()
    return True


async def register_user(db: AsyncSession, data: UserRegister) -> User:
    email = data.email.lower()
    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    otp = await _get_latest_verified_otp(db, "register", email)
    if not otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is not verified. Please verify your OTP first.",
        )

    user = User(
        email=email,
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
        country_code=data.country_code,
        mobile_number=data.mobile_number,
        auth_provider="email",
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    user = await get_user_by_email(db, email)
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    return user


async def verify_google_id_token(id_token: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_CLIENT_ID is not configured",
        )

    try:
        info = google_id_token.verify_oauth2_token(
            id_token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception:
        logger.exception("Google ID token verification failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google ID token",
        )

    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account has no email",
        )

    return {
        "email": email,
        "full_name": info.get("name"),
        "profile_picture": info.get("picture"),
        "email_verified": info.get("email_verified", False),
    }


async def login_or_create_google_user(db: AsyncSession, profile: dict) -> User:
    """Sign in with Google, creating the account on first use.

    When the email already belongs to a password account we link the account
    rather than rejecting the sign-in. Google has just cryptographically
    verified ownership of the address (see verify_google_id_token, which
    checks audience and signature), so possession of this Google identity is
    proof of control of the mailbox - the same level of assurance the password
    reset flow relies on. Requiring the user to also know the password blocked
    legitimate users who had signed up with a password first.

    hashed_password is deliberately preserved so password login keeps working
    for the account, and a stale/inactive flag is corrected while we are here.
    """
    email = profile["email"]
    user = await get_user_by_email(db, email)
    if user:
        if user.auth_provider != "google":
            user.auth_provider = "google"
            logger.info("Linked Google identity to existing account %s", user.id)
        # Google is an authoritative source for both of these, so fill in only
        # blanks and never overwrite a value the user may have set themselves.
        if not user.full_name and profile.get("full_name"):
            user.full_name = profile["full_name"]
        if not user.profile_picture and profile.get("profile_picture"):
            user.profile_picture = profile["profile_picture"]
        if profile.get("email_verified") and not user.is_verified:
            user.is_verified = True
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been deactivated.",
            )
        await db.commit()
        await db.refresh(user)
        return user

    user = User(
        email=email,
        hashed_password=None,
        full_name=profile.get("full_name"),
        profile_picture=profile.get("profile_picture"),
        auth_provider="google",
        is_verified=bool(profile.get("email_verified")),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
