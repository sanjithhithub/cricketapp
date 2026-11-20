"""The tables behind match-level fielding.

``FieldingSetup`` is one row per (match, innings): the fielding side and its
designated wicketkeeper. The wicketkeeper is a column, not a row in
``FieldingAssignment``, precisely so "exactly one keeper" is structural - a
single nullable column cannot hold two players, and re-designating a keeper
overwrites the column instead of appending a second claim. It is also a
deliberately separate fact from captaincy: captaincy lives on
``player_team_assignments`` and is never read or written here.

``FieldingAssignment`` is one row per positioned fielder - an *active* member
of the eleven on the field. Its constraints:

* ``(setup_id, player_id)`` stays unique, so a player can never appear twice
  on the same field;
* ``(setup_id, position_key)`` is deliberately **not** unique any more. Two
  fielders may legitimately hold the same named area (a second slip and a
  third slip both standing in "the cordon", or a captain who wants two men at
  point) as long as their coordinates differ, so uniqueness by name alone was
  wrong and is enforced nowhere. The coordinates are what tell the two apart.

``is_substitute`` records whether the fielder came onto the field through a
substitution rather than starting in the XI. It is *derived* from
``FieldSubstitution`` on every write (a full-replace save re-derives it), so
the flag can never drift away from the history that produced it.

``FieldSubstitution`` is the append-only history of every substitution: who
left, who came on, for which side and innings, why, whether it was an ordinary
replacement or a competition-approved special one (concussion and the like),
and who authorised it. Nothing ever deletes these rows - they are match
history - and they are the source of truth for "is this player an ordinary
substitute who may not bowl, keep wicket or captain?".

Nothing here references deliveries, innings scores or results, and nothing
references back: saving a fielding setup or recording a substitution cannot
change a ball, a run, a dismissal or a standings row.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutils import utcnow

if TYPE_CHECKING:
    from app.auth.models import User
    from app.matches.models import Match
    from app.players.models import Player
    from app.teams.models import Team


class FieldingSetup(Base):
    __tablename__ = "fielding_setups"
    __table_args__ = (
        UniqueConstraint("match_id", "innings_number", name="uq_fielding_setup_match_innings"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    # Keyed by innings *number* rather than innings.id on purpose: the first
    # innings' fielding side is decided by the toss, before the innings row
    # exists (it is created by start_scoring). Numbering also survives a
    # Super Over, where innings 3+ swap the fielding side again.
    innings_number: Mapped[int] = mapped_column(Integer)
    fielding_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    # Nullable: a setup may exist before a keeper is picked. Exactly one row
    # per (match, innings) means at most one keeper, always.
    wicketkeeper_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"), nullable=True
    )

    match: Mapped[Match] = relationship(foreign_keys=[match_id])
    fielding_team: Mapped[Team] = relationship(foreign_keys=[fielding_team_id])
    wicketkeeper: Mapped[Player | None] = relationship(foreign_keys=[wicketkeeper_player_id])
    assignments: Mapped[list[FieldingAssignment]] = relationship(
        back_populates="setup",
        cascade="all, delete-orphan",
        order_by="FieldingAssignment.id",
    )
    # No delete-orphan cascade: this is append-only match history, and a
    # setup row is never deleted by this application.
    substitutions: Mapped[list[FieldSubstitution]] = relationship(
        back_populates="setup",
        order_by="FieldSubstitution.id",
    )


class FieldingAssignment(Base):
    __tablename__ = "fielding_assignments"
    # Only "one position per fielder". The old unique-on-position constraint
    # is gone: two fielders may share a named area when their coordinates
    # differ, which is what a real cordon looks like.
    __table_args__ = (
        UniqueConstraint("setup_id", "player_id", name="uq_fielding_assignment_setup_player"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int] = mapped_column(ForeignKey("fielding_setups.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    position_key: Mapped[str] = mapped_column(String(40))
    # Anchors default to the catalog's value; stored so the diagram can be
    # redrawn from the database alone even if the catalog's defaults ever
    # shift. Normalized 0-100, x right / y down.
    anchor_x: Mapped[float] = mapped_column(Float)
    anchor_y: Mapped[float] = mapped_column(Float)
    # True when this fielder is on the field as a substitute rather than as a
    # member of the starting XI. Derived from FieldSubstitution on every save.
    is_substitute: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    # A free-text label for ``custom_position``: a marker dropped where no
    # anchor reaches keeps whatever name the scorer gave it. Null for every
    # catalog position.
    custom_label: Mapped[str | None] = mapped_column(String(60), nullable=True)

    setup: Mapped[FieldingSetup] = relationship(back_populates="assignments")
    player: Mapped[Player] = relationship(foreign_keys=[player_id])


class FieldSubstitution(Base):
    """One substitution, kept forever as match history."""

    __tablename__ = "field_substitutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    setup_id: Mapped[int] = mapped_column(ForeignKey("fielding_setups.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    innings_number: Mapped[int] = mapped_column(Integer)
    # Stable ids, never names: a renamed player must not rewrite history.
    outgoing_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    incoming_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    # Free text chosen by the scorer ("injury", "illness", "tactical",
    # "concussion", ...) - see app.fielding.rules for the allowed set.
    reason: Mapped[str] = mapped_column(String(60))
    # "normal" | "special". A special replacement (an approved concussion
    # replacement, for instance) may take the field with the full privileges
    # of an XI player *when the competition's rules allow it*; a normal
    # substitute may not bowl, keep wicket or captain.
    replacement_type: Mapped[str] = mapped_column(String(20), default="normal")
    # Who authorised it, when the app knows. Null for substitutions recorded
    # before audit columns existed or by a system import.
    authorized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    setup: Mapped[FieldingSetup] = relationship(back_populates="substitutions")
    outgoing_player: Mapped[Player] = relationship(foreign_keys=[outgoing_player_id])
    incoming_player: Mapped[Player] = relationship(foreign_keys=[incoming_player_id])
    authorized_by: Mapped[User | None] = relationship(foreign_keys=[authorized_by_user_id])
