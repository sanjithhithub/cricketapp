from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.levels.models import TeamLevel
    from app.models import City, Country, State
    from app.players.models import Player


class PlayerTeamAssignment(Base):
    __tablename__ = "player_team_assignments"
    __table_args__ = (
        Column("player_id", ForeignKey("players.id"), primary_key=True),
        Column("team_id", ForeignKey("teams.id"), primary_key=True),
        Column("level_id", ForeignKey("team_levels.id"), primary_key=True),
    )

    role: Mapped[str] = mapped_column(String(20), default="playing_11")

    # Captaincy is deliberately separate from `role`: `role` decides which bucket
    # the player sits in and how many may be in the playing XI, so a captaincy
    # flag keeps its own meaning instead of competing with that. Both default to
    # false, which is "this team has not named one yet" - a squad is allowed to
    # have neither, and the server refuses only to name the same player twice.
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_vice_captain: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    player: Mapped[Player] = relationship()
    team: Mapped[Team] = relationship()
    level: Mapped[TeamLevel] = relationship()


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_teams_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    short_name: Mapped[str] = mapped_column(String(10))
    logo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    homeground: Mapped[str] = mapped_column(String(200))
    founder: Mapped[str] = mapped_column(String(100))
    founded_year: Mapped[int] = mapped_column(Integer)
    owner: Mapped[str] = mapped_column(String(200))
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"))
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("team_levels.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    level: Mapped[TeamLevel] = relationship(back_populates="teams")
    assignments: Mapped[list[PlayerTeamAssignment]] = relationship(back_populates="team")
    country: Mapped[Country] = relationship()
    state: Mapped[State] = relationship()
    city: Mapped[City] = relationship()
