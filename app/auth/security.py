import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.hash import pbkdf2_sha256
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.database import get_db

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
# Refresh tokens outlive access tokens by design: they are the thing a client
# falls back to when the short-lived one has expired. 30 days is long enough to
# survive a laptop being closed for a week and short enough that a stolen token
# is not a permanent credential.
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pbkdf2_sha256.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pbkdf2_sha256.verify(plain_password, hashed_password)
    except Exception:
        return False


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    expire = expires_minutes or ACCESS_TOKEN_EXPIRE_MINUTES
    now = datetime.now(UTC)
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": now + timedelta(minutes=expire),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def generate_refresh_token() -> str:
    """A new opaque refresh token.

    Opaque and not a JWT, on purpose. The access token is self-contained so the
    hot path needs no lookup; a refresh token is only ever exchanged once per hour
    at most, so paying a database read costs nothing measurable - and it buys two
    things a JWT cannot: the token can be revoked server-side, and rotating it is
    just another insert.

    32 bytes from ``secrets`` is 256 bits of entropy, so the token cannot be
    guessed and needs no special characters to be URL-safe.
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """The value stored for a refresh token.

    Plain SHA-256, not a password hash. That is the right call *because* the input
    is 256 bits of CSPRNG output rather than a human-chosen password: there is no
    dictionary to attack, and hashing a 60-odd byte token with a slow KDF would
    add latency to every refresh to buy nothing. The digest is one-way regardless,
    so a database leak still yields no usable tokens.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    """When a token issued now would expire.

    Naive UTC, to match the ``DateTime`` columns written by
    :class:`~app.auth.models.RefreshToken`; comparing a naive expiry against an
    aware ``now`` raises, which would turn every refresh into a 500.
    """
    return (datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).replace(tzinfo=None)


def refresh_token_expires_in() -> int:
    """The refresh token's remaining life in seconds, for the login response."""
    return REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
