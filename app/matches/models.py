from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.players.models import Player
    from app.teams.models import Team


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_date: Mapped[date] = mapped_column()
    match_time: Mapped[str] = mapped_column(String(10))
    venue: Mapped[str] = mapped_column(String(200))
    match_type: Mapped[str] = mapped_column(String(10))
    result: Mapped[str | None] = mapped_column(String(500), nullable=True)

    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    toss_winner_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    toss_decision: Mapped[str] = mapped_column(String(10))

    referee_1_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    referee_2_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    match_referee_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The human-judged award shown on the completed-match summary. Deliberately
    # optional and never derived: it may belong to the losing side.
    player_of_match_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"), nullable=True
    )
    player_of_match: Mapped[Player] = relationship(foreign_keys=[player_of_match_id])

    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    current_innings_number: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    # Super Over: when the main (2-innings) match finishes level, play a
    # one-over-per-side decider instead of declaring the match tied.
    super_over_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When a Super Over is itself tied, keep playing extra Super Overs (with the
    # batting order of the two teams swapped each time) until one wins.
    super_over_repeat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    team_a: Mapped[Team] = relationship(foreign_keys=[team_a_id])
    team_b: Mapped[Team] = relationship(foreign_keys=[team_b_id])
    toss_winner: Mapped[Team] = relationship(foreign_keys=[toss_winner_id])
