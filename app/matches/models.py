from datetime import date
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


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

    team_a: Mapped["Team"] = relationship(foreign_keys=[team_a_id])
    team_b: Mapped["Team"] = relationship(foreign_keys=[team_b_id])
    toss_winner: Mapped["Team"] = relationship(foreign_keys=[toss_winner_id])
