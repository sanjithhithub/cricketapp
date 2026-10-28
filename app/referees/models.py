"""The match officials: the people who officiate a match.

A referee is a person; a slot is a place. The slots - "Referee 1",
"Referee 2", "Match Referee" - belong to one match and say where that person
stood in it. The referee record belongs to the account that hired them and
carries what an officials panel needs to show: who they are, where they are
from, when they were born, what they do, how long they have been doing it, what
they look like and how to reach them.

The table is deliberately close to ``players``: same shape of identity (a
permanent code, a name, a date of birth, a nationality, a photo, a phone and an
email) and the same rules about phone numbers - text, never an integer, with a
canonical E.164 key beside the number as typed. Reusing the players' identity
helpers rather than copying them means a number typed three different ways
collapses the same way here as it does there.

What a referee does not share with a player is the cricket: no batting hand, no
bowling type, no height, no weight, and no squad. A referee takes no part in an
innings.

Beyond the identity, the record answers the questions an appointments panel
actually asks: is this official active, are they free, how long have they been
doing it, what certificate do they hold and until when, what formats do they
know, what languages do they speak, where are they based and who put them on
the register.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.players.identity import (
    normalize_country_code,
    normalize_mobile,
    phone_e164,
)
from app.referees.identity import experience_level_for, generate_referee_code
from app.timeutils import utcnow

if TYPE_CHECKING:
    from app.auth.models import User
    from app.models import City, Country, State


class Referee(Base):
    __tablename__ = "referees"
    __table_args__ = (
        # The permanent, human-quotable identity. Unique and never reused, so it
        # can be printed on a scorecard and used to tell two same-named
        # officials apart.
        UniqueConstraint("referee_code", name="uq_referees_referee_code"),
        # A phone number is NOT unique: it is optional (an official registered
        # by a club may not have given one) and may legitimately be shared (one
        # family phone, a club landline). The index exists to make the
        # duplicate check fast, not to forbid a second row.
        Index("ix_referees_phone_e164", "phone_e164"),
        # An email address is unique *within an account*, case-insensitively.
        # The stored value is lower-cased by the schema on the way in, so the
        # constraint below is a plain unique index on (user_id, email) and it
        # already means "one address per club". Two different clubs may still
        # use the same shared address - a league's fixture desk, say - because
        # they are different rows and the uniqueness is scoped to the owner.
        UniqueConstraint("user_id", "email", name="uq_referees_user_email"),
        Index("ix_referees_email", "email"),
        Index("ix_referees_name_lookup", "user_id", "last_name", "first_name"),
        # The columns the officials list is filtered and sorted on.
        Index("ix_referees_user_status", "user_id", "status"),
        Index("ix_referees_user_type", "user_id", "referee_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referee_code: Mapped[str] = mapped_column(String(16), default=generate_referee_code)
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)
    # Nationality: the country the official is from, taken from the same
    # reference data the app uses for players and teams.
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    profile_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # What the official does, not which slot they filled. The same person can
    # umpire one match and referee the next, so the type cannot live on the
    # match.
    referee_type: Mapped[str] = mapped_column(String(30))
    experience_years: Mapped[int] = mapped_column(Integer, default=0)
    # Dial code of the phone number, with the plus. Deliberately not the
    # nationality's code: an official may be from one country and reachable on
    # another country's number.
    country_code: Mapped[str] = mapped_column(String(5), default="")
    # Text, not an integer: leading zeros and the country code have to survive
    # a round trip through the database.
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Country code + national number in one canonical string. This is the column
    # duplicate detection compares; `mobile_number` is kept as the user typed it
    # for display.
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str] = mapped_column(String(100))

    # --- professional -------------------------------------------------------
    # The band the experience falls into. Derived from `experience_years` on
    # every write, so it is never stored in a state that contradicts the years
    # next to it.
    experience_level: Mapped[str] = mapped_column(String(20), default="beginner")
    certification_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    certifying_authority: Mapped[str | None] = mapped_column(String(120), nullable=True)
    certification_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    certification_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The formats and competitions the official knows: "T20", "ODI", "Test",
    # "Domestic". A list, because an official is usually good at more than one,
    # and a plain string would need a delimiter invented to hold it.
    specializations: Mapped[list[str]] = mapped_column(JSON, default=list)
    # The languages the official speaks, used to staff a multilingual panel.
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)

    # --- contact ------------------------------------------------------------
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # The postal address is a place, and the app already has a reference table
    # of states and cities for exactly this. `country_id` above is the
    # nationality; it also serves as the country of the postal address, so no
    # second country column is kept - a second one would only be able to
    # disagree with the first.
    state_id: Mapped[int | None] = mapped_column(ForeignKey("states.id"), nullable=True)
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- administrative -----------------------------------------------------
    # Whether the official may be appointed to a match at all.
    status: Mapped[str] = mapped_column(String(20), default="active")
    # Whether they are free to be booked right now. Separate from `status`:
    # an active official can be unavailable for a fortnight.
    availability_status: Mapped[str] = mapped_column(String(20), default="available")
    joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Internal notes. Never shown to a reader who is not an administrator of
    # the account.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    # The country the official is from, loaded so the officials panel can label
    # the nationality without the client joining the reference data itself.
    country: Mapped[Country] = relationship()
    state: Mapped[State | None] = relationship()
    city: Mapped[City | None] = relationship()

    # Who put the official on the register and who last touched the record.
    # Two separate relationships to the same table, so each column is told
    # apart from the other.
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_id])

    # The matches this official has been appointed to. A match may have several
    # officials in different roles, so this is a list and the link is a table
    # of its own rather than a column.
    #
    # No delete-orphan cascade on purpose: an appointment is part of a match's
    # record, so removing an official must not silently remove it. The delete
    # is refused while the appointment stands, and the foreign key's RESTRICT
    # is the backstop for any path that bypasses that check.
    assignments: Mapped[list[MatchRefereeAssignment]] = relationship(
        back_populates="referee",
        passive_deletes=True,
    )


class MatchRefereeAssignment(Base):
    """One official appointed to one match, in one role.

    A match has more than one official and more than one kind of official: two
    on-field umpires, a TV umpire, a match referee, sometimes a reserve. One
    column per role cannot express that, so the appointment is a row.

    The two unique constraints are the rules the panel depends on:

    * one role per match - a match has one first umpire, not two;
    * one official per match - the same person cannot stand in two slots of the
      same match, which is a mistake in the data and is caught here rather
      than left to be noticed on a scorecard.

    Deleting a match deletes its appointments with it: the appointment only
    exists because of the match. Deleting an official does not - that is
    refused while the appointment stands, so a scorecard never ends up naming
    nobody.
    """

    __tablename__ = "match_referee_assignments"
    __table_args__ = (
        UniqueConstraint("match_id", "role", name="uq_match_referee_assignments_match_role"),
        UniqueConstraint(
            "match_id", "referee_id", name="uq_match_referee_assignments_match_referee"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referee_id: Mapped[int] = mapped_column(
        ForeignKey("referees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Which seat the official stands in. The vocabulary is the referee types,
    # so an official's own record and their appointment cannot disagree about
    # what they do.
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Who made the appointment. Null when the appointment predates the audit
    # columns.
    assigned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    referee: Mapped[Referee] = relationship(back_populates="assignments")


@event.listens_for(Referee, "before_insert")
def _derive_referee_identity(mapper: Any, connection: Any, target: Referee) -> None:
    """Guarantee the identity invariants for every insert, whoever writes it.

    A referee row that reaches the database without a code, or with a phone whose
    comparison key does not match it, would break the two things this table is
    built on: a permanent handle for a human, and duplicate detection. Doing it
    here rather than in the service layer means tests, seed scripts and any
    future code path get it right by default instead of by remembering.
    """
    if not target.referee_code:
        target.referee_code = generate_referee_code()
    target.country_code = normalize_country_code(target.country_code)
    target.mobile_number = normalize_mobile(target.mobile_number)
    target.phone_e164 = phone_e164(target.country_code, target.mobile_number)
    # The band is derived, never taken from the request, so the two columns
    # that describe the same experience cannot drift apart.
    target.experience_level = experience_level_for(target.experience_years)
    # The email is stored lower-cased so the case-insensitive uniqueness the
    # schema promises is a plain database constraint rather than a rule that
    # has to be remembered at every write.
    if target.email:
        target.email = str(target.email).strip().lower()


@event.listens_for(Referee, "before_update")
def _derive_referee_update(mapper: Any, connection: Any, target: Referee) -> None:
    """Keep the derived columns honest on an update too.

    A PATCH that changes the phone number without changing the country code
    would otherwise leave the canonical comparison key pointing at the old
    number, and the duplicate check would stop seeing the number it is meant
    to see.
    """
    target.country_code = normalize_country_code(target.country_code)
    target.mobile_number = normalize_mobile(target.mobile_number)
    target.phone_e164 = phone_e164(target.country_code, target.mobile_number)
    target.experience_level = experience_level_for(target.experience_years)
    if target.email:
        target.email = str(target.email).strip().lower()
