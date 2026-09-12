from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Integer, String
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

    player: Mapped[Player] = relationship()
    team: Mapped[Team] = relationship()
    level: Mapped[TeamLevel] = relationship()


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
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

    level: Mapped[TeamLevel] = relationship(back_populates="teams")
    assignments: Mapped[list[PlayerTeamAssignment]] = relationship(back_populates="team")
    country: Mapped[Country] = relationship()
    state: Mapped[State] = relationship()
    city: Mapped[City] = relationship()
