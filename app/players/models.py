from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.players.identity import (
    generate_player_code,
    normalize_country_code,
    normalize_mobile,
    phone_e164,
)

if TYPE_CHECKING:
    from app.teams.models import PlayerTeamAssignment


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        # The permanent, human-quotable identity. Unique and never reused, so it
        # can be shown on a scorecard and used to tell two same-named players
        # apart. The app never falls back to the name for identity.
        UniqueConstraint("player_code", name="uq_players_player_code"),
        # A phone number is NOT unique: it is optional (youth players, walk-ins)
        # and may legitimately be shared (one family phone, a club landline). The
        # index exists to make the duplicate check fast, not to forbid a second
        # row - the check is a prompt for a human to confirm, never a rejection.
        Index("ix_players_phone_e164", "phone_e164"),
        Index("ix_players_name_lookup", "user_id", "last_name", "first_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_code: Mapped[str] = mapped_column(String(16), default=generate_player_code)
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(10))
    profile_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    batting_hand: Mapped[str] = mapped_column(String(10))
    batting_position: Mapped[str] = mapped_column(String(20))
    bowling_hand: Mapped[str] = mapped_column(String(10))
    bowling_type: Mapped[str] = mapped_column(String(20))
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"))
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    height: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float)
    country_code: Mapped[str] = mapped_column(String(5))
    # Text, not an integer: leading zeros and the country code have to survive a
    # round trip through the database.
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Country code + national number in one canonical string. This is the column
    # duplicate detection compares; `mobile_number` is kept as the user typed it
    # for display.
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    is_phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    assignments: Mapped[list[PlayerTeamAssignment]] = relationship(back_populates="player")
    aliases: Mapped[list[PlayerAlias]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class PlayerAlias(Base):
    """A nickname, initials or "also known as" pointing at one existing player.

    Stored against ``player_id`` rather than creating another player, so a player
    called "Rohit" and a player called "Rohit Sharma" can be told apart while
    both still resolve to the same person when someone types the short form.
    """

    __tablename__ = "player_aliases"
    __table_args__ = (
        UniqueConstraint("player_id", "alias_key", name="uq_player_alias_key"),
        Index("ix_player_aliases_alias_key", "alias_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(100))
    # Lower-cased / space-collapsed copy so uniqueness and lookup are insensitive
    # to how the nickname was typed.
    alias_key: Mapped[str] = mapped_column(String(100))

    player: Mapped[Player] = relationship(back_populates="aliases")


@event.listens_for(Player, "before_insert")
def _derive_player_identity(mapper: Any, connection: Any, target: Player) -> None:
    """Guarantee the identity invariants for every insert, whoever writes it.

    A player row that reaches the database without a code, or with a phone whose
    comparison key does not match it, would break the two things this table is
    built on: a permanent handle for a human, and duplicate detection. Doing it
    here rather than in the service layer means tests, seed scripts and any
    future code path get it right by default instead of by remembering.
    """
    if not target.player_code:
        target.player_code = generate_player_code()
    if target.country_code:
        target.country_code = normalize_country_code(target.country_code)
    target.mobile_number = normalize_mobile(target.mobile_number)
    target.phone_e164 = phone_e164(target.country_code, target.mobile_number)
