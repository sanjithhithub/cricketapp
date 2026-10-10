from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.timeutils import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    profile_picture: Mapped[str | None] = mapped_column(String(500), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    mobile_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(20), default="email")
    role: Mapped[str] = mapped_column(String(20), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuthOTP(Base):
    __tablename__ = "auth_otps"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    mobile_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    otp_hash: Mapped[str] = mapped_column(String(255))
    session_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefreshToken(Base):
    """One issued refresh token, stored as a SHA-256 hash of the token.

    The token itself is random bytes handed to the client exactly once and never
    stored: a database dump therefore cannot be replayed as a session. Storing a
    *row* per token rather than a single "last issued" column is what makes
    rotation and revocation observable - an old token can be told apart from a
    current one, and logout can end one session without signing out every device.

    Rotation replaces ``revoked_by`` rather than deleting the row, so replaying a
    superseded token is detectable (its row is revoked and its ``replaced_by`` is
    set) instead of merely failing.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # SHA-256 hex. 64 characters, and deliberately not the raw token: the column
    # is indexed, and a raw secret in an indexed column is one bad backup away
    # from a credential leak.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The token issued when this one was rotated. Chains back to the newest token,
    # which is what lets a replay of an old token be named rather than guessed.
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
