"""The people who own the teams: the owners, and the ownership records.

Two tables, because ownership is not a property of a person. A person is a
person - a name, a date of birth, a nationality, a way to reach them - and that
is what ``team_owners`` holds. Holding a team is a relationship, and a
relationship carries its own facts: what kind of stake it is, how much of the
team it is, when it started and when it ended, and whether it is still live.
That is what ``team_ownerships`` holds.

The split is what lets the app answer both questions without the two answers
contradicting each other. An owner may own four teams: outright one, a third of
another, and none of the other two any more. Storing the ownership type on the
owner would force one type on all four, and storing the teams as a list of ids
on the owner would lose the dates and the percentages.

Two rules the tables are built around:

* a team may have more than one owner, and an owner may have more than one
  team, so the link between them is a table of its own rather than a column;
* an ownership that has ended is kept. Ending a stake sets the end date and
  the status; it does not delete the row, because "this club was owned by that
  family until 2019" is a fact about the club's past and deleting it would
  leave the history of the team with a hole in it.

The team itself is never touched when an owner is deleted or when an ownership
ends. A team outlives its owners, and so do the players and the matches that
belong to it. The foreign keys are set to RESTRICT where a row would otherwise
take a team or an owner with it, and the service layer refuses the delete while
an ownership record still stands.

``teams.owner`` stays as it was - a free-text name, the club's own way of
writing down who runs the team - and is kept in step with the ownership rows
the same way the match's official slots are. It is a display mirror, not a
second source of truth: the owner records are the truth, and the mirror says
whatever they say.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.players.identity import (
    normalize_country_code,
    normalize_mobile,
    phone_e164,
)
from app.timeutils import utcnow

if TYPE_CHECKING:
    from app.auth.models import User
    from app.models import Country
    from app.teams.models import Team


class TeamOwner(Base):
    """A person on the account's register of owners.

    The row is the person, not the ownership: it answers who this is and how to
    reach them. Which teams they hold, and on what terms, lives in
    ``team_ownerships``.

    ``ownership_type`` and ``ownership_status`` are the owner's standing - the
    kind of owner they generally are, and whether they are currently on the
    register at all. They are the *default* for a new ownership relationship
    rather than a copy of one: a new stake in a team starts from the owner's
    own type, and an existing stake keeps the type it was taken on with, so
    changing an owner today cannot rewrite what a team's ownership looked like
    last season.
    """

    __tablename__ = "team_owners"
    __table_args__ = (
        # An email address is unique *within an account*, case-insensitively.
        # The stored value is lower-cased on the way in, so this is a plain
        # unique constraint on (user_id, email) and it already means "one
        # address per club". Two clubs may share a shared address - a league's
        # fixture desk, say - because they are different rows.
        UniqueConstraint("user_id", "email", name="uq_team_owners_user_email"),
        Index("ix_team_owners_email", "email"),
        Index("ix_team_owners_name_lookup", "user_id", "last_name", "first_name"),
        Index("ix_team_owners_phone_e164", "phone_e164"),
        # The columns the owners list is filtered and sorted on.
        Index("ix_team_owners_user_status", "user_id", "ownership_status"),
        Index("ix_team_owners_user_type", "user_id", "ownership_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The account the register belongs to. Another account's owner is reported
    # as missing rather than as forbidden, so the response cannot be used to
    # probe which ids exist.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    # The account this person signs in with, when they have one. A user who
    # signed in with Google and then completed an owner profile has a row here
    # that is theirs: they may edit it and read their own teams, and nobody
    # else may.
    linked_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )

    # --- personal ------------------------------------------------------------
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)
    # The gender the owner records, from the app's own closed set of values.
    gender: Mapped[str] = mapped_column(String(20))
    # Nationality: the country the owner is from, taken from the same reference
    # data the app uses for players and teams.
    country_id: Mapped[int] = mapped_column(Integer, ForeignKey("countries.id"))
    profile_image: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- contact ------------------------------------------------------------
    email: Mapped[str] = mapped_column(String(100))
    # Dial code of the phone number, with the plus. Deliberately not the
    # nationality's code: an owner may be from one country and reachable on
    # another country's number.
    country_code: Mapped[str] = mapped_column(String(5), default="")
    # Text, not an integer: leading zeros and the country code have to survive
    # a round trip through the database, and an international number is not ten
    # digits long.
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Country code + national number in one canonical string. This is the
    # column duplicate detection compares; `mobile_number` is kept as the user
    # typed it for display.
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- standing -----------------------------------------------------------
    # The kind of owner this person generally is, and the default for a new
    # stake in a team.
    ownership_type: Mapped[str] = mapped_column(String(30))
    # Whether this person is currently on the register as an owner at all.
    ownership_status: Mapped[str] = mapped_column(String(20), default="active")
    # The organisation the ownership is held through, when it is held through
    # one at all. Required for a company or a franchise, and left empty for a
    # person who owns a team in their own name.
    organization_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # The company's registration number, for the same reason.
    registration_number: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # --- administrative -----------------------------------------------------
    # Internal notes. Never shown to a reader who is not an administrator of
    # the account.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    # The country the owner is from, loaded so the list can label the
    # nationality without the client joining the reference data itself.
    country: Mapped[Country] = relationship()

    # Who put the owner on the register and who last touched the record.
    # Two separate relationships to the same table, so each column is told
    # apart from the other.
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_id])

    # The stakes this owner holds. A list, because an owner may hold several
    # teams and the link is a table of its own.
    #
    # No delete-orphan cascade on purpose: an ownership record is part of a
    # team's record, so removing an owner must not silently remove it. The
    # delete is refused while a stake stands, and the foreign key's RESTRICT is
    # the backstop for any path that bypasses that check.
    ownerships: Mapped[list[TeamOwnership]] = relationship(
        back_populates="owner",
        passive_deletes=True,
    )


class TeamOwnership(Base):
    """One owner holding one team.

    The rules the table is built around:

    * a team may have several owners and an owner may hold several teams, so
      the link is a row rather than a column;
    * one person cannot hold the same team twice *at the same time*. The
      uniqueness is on the active rows only, so an owner who hands a team over
      and takes it back later has two records - the old one ended and kept, the
      new one live - rather than one record that has been rewritten;
    * the percentages of the live owners of a team add up to no more than a
      hundred. They are not required to add up to a hundred: a club may leave
      part of its equity unallocated, and the app has no business insisting
      otherwise.
    """

    __tablename__ = "team_ownerships"
    __table_args__ = (
        # One live ownership per (team, owner). The index is partial - it only
        # covers the live rows - so an ended ownership keeps its place in the
        # history without blocking a fresh one.
        Index(
            "uq_team_ownerships_active",
            "team_id",
            "owner_id",
            unique=True,
            sqlite_where=text("ownership_status = 'active'"),
            postgresql_where=text("ownership_status = 'active'"),
        ),
        # The columns the team panel and the owners list are filtered on. The
        # plain index on owner_id comes from the column itself.
        Index("ix_team_ownerships_team_status", "team_id", "ownership_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT, not CASCADE: deleting an owner must not take a team, its
    # players or its matches with it. The delete is refused while a stake
    # stands, and this is the backstop for any path that bypasses that check.
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("team_owners.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # The kind of stake this is. Taken from the owner when the caller does not
    # say, because an owner of a company does not hold every team they are
    # involved in the same way.
    ownership_type: Mapped[str] = mapped_column(String(30))
    # How much of the team this stake is, as a percentage. Optional: a club
    # that does not split its equity does not have to pretend to.
    ownership_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    ownership_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # When the stake ended. Set when the ownership is ended, and kept
    # afterwards: it is the date the club's history turns on.
    ownership_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Whether this stake is live.
    ownership_status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    team: Mapped[Team] = relationship(back_populates="ownerships")
    owner: Mapped[TeamOwner] = relationship(back_populates="ownerships")


@event.listens_for(TeamOwner, "before_insert")
def _derive_owner_identity(mapper: Any, connection: Any, target: TeamOwner) -> None:
    """Guarantee the identity invariants for every insert, whoever writes it.

    The same reasoning as the referees' hook: a row that reaches the database
    without a canonical phone key, or with an email in a different case, breaks
    the two things this table is built on. Doing it here rather than in the
    service layer means tests, seed scripts and any future code path get it
    right by default instead of by remembering.
    """
    target.country_code = normalize_country_code(target.country_code)
    target.mobile_number = normalize_mobile(target.mobile_number)
    target.phone_e164 = phone_e164(target.country_code, target.mobile_number)
    # The email is stored lower-cased so the case-insensitive uniqueness the
    # schema promises is a plain database constraint rather than a rule that
    # has to be remembered at every write.
    if target.email:
        target.email = str(target.email).strip().lower()


@event.listens_for(TeamOwner, "before_update")
def _derive_owner_update(mapper: Any, connection: Any, target: TeamOwner) -> None:
    """Keep the derived columns honest on an update too.

    A PATCH that changes the phone number without changing the country code
    would otherwise leave the canonical comparison key pointing at the old
    number, and the duplicate check would stop seeing the number it is meant to
    see.
    """
    target.country_code = normalize_country_code(target.country_code)
    target.mobile_number = normalize_mobile(target.mobile_number)
    target.phone_e164 = phone_e164(target.country_code, target.mobile_number)
    if target.email:
        target.email = str(target.email).strip().lower()


@event.listens_for(TeamOwnership, "before_insert")
@event.listens_for(TeamOwnership, "before_update")
def _derive_ownership(mapper: Any, connection: Any, target: TeamOwnership) -> None:
    """Keep a stake's numbers in range whatever wrote it.

    A percentage outside 0-100 is not a share of anything, and a start date
    after the end date is a stake that ends before it begins. Both are refused
    here as well as in the schema, so a seed script cannot put one in the
    table either.
    """
    if target.ownership_percentage is not None:
        target.ownership_percentage = float(target.ownership_percentage)
        if not 0 <= target.ownership_percentage <= 100:
            raise ValueError("ownership_percentage must be between 0 and 100")
    if (
        target.ownership_start_date
        and target.ownership_end_date
        and target.ownership_end_date < target.ownership_start_date
    ):
        raise ValueError("ownership_end_date cannot be before ownership_start_date")
