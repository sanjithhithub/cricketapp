"""Tournament, membership and fixture tables.

A tournament is a competition between a set of a account's teams. Membership is
its own table (``tournament_teams``) because a team plays in many competitions,
and a fixture is its own table rather than a match: a fixture is the *plan* (who
plays whom, when, where, and at which stage), while the match is the scored
result of that plan. Keeping them apart means the round-robin can be generated
in one call before a ball is bowled, and the table stays a plan until a real
match is linked to a fixture.

Points are stored on the tournament rather than hard-coded, because a competition
is allowed to award three for a win or reward a washout differently, and a points
table that silently used 2/1/0 would disagree with the organiser's rules.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.matches.models import Match
    from app.teams.models import Team


class Tournament(Base):
    __tablename__ = "tournaments"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tournaments_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    level_id: Mapped[int | None] = mapped_column(ForeignKey("team_levels.id"), nullable=True)

    format: Mapped[str] = mapped_column(String(20), default="league")
    status: Mapped[str] = mapped_column(String(20), default="upcoming")
    start_date: Mapped[date | None] = mapped_column(nullable=True)
    end_date: Mapped[date | None] = mapped_column(nullable=True)

    points_win: Mapped[int] = mapped_column(Integer, default=2)
    points_tie: Mapped[int] = mapped_column(Integer, default=1)
    points_loss: Mapped[int] = mapped_column(Integer, default=0)
    points_no_result: Mapped[int] = mapped_column(Integer, default=1)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    teams: Mapped[list[TournamentTeam]] = relationship(
        back_populates="tournament", cascade="all, delete-orphan"
    )
    fixtures: Mapped[list[TournamentFixture]] = relationship(
        back_populates="tournament", cascade="all, delete-orphan"
    )


class TournamentTeam(Base):
    """One team's entry in one tournament.

    The unique constraint is on ``(tournament_id, team_id)`` rather than on a
    surrogate id, so a team cannot be entered twice - a duplicate entry would
    otherwise quietly double its fixtures in a generated round-robin.
    """

    __tablename__ = "tournament_teams"
    __table_args__ = (UniqueConstraint("tournament_id", "team_id", name="uq_tournament_team"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)

    tournament: Mapped[Tournament] = relationship(back_populates="teams")
    team: Mapped[Team] = relationship()


class TournamentFixture(Base):
    """A planned match between two entered teams.

    ``match_id`` is the only link to scored data and stays ``None`` until a real
    match is attached, which is what separates a fixture on the schedule from one
    that counts towards the points table.
    """

    __tablename__ = "tournament_fixtures"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"), index=True)
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)

    scheduled_date: Mapped[date | None] = mapped_column(nullable=True)
    scheduled_time: Mapped[str | None] = mapped_column(String(10), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(200), nullable=True)
    stage: Mapped[str] = mapped_column(String(20), default="league")
    round_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tournament: Mapped[Tournament] = relationship(back_populates="fixtures")
    team_a: Mapped[Team] = relationship(foreign_keys=[team_a_id])
    team_b: Mapped[Team] = relationship(foreign_keys=[team_b_id])
    match: Mapped[Match | None] = relationship()
