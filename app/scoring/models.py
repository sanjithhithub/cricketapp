from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.scoring.enums import ExtraType


class Innings(Base):
    __tablename__ = "innings"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    batting_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    bowling_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    innings_number: Mapped[int] = mapped_column(Integer)
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)

    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="innings")
    batsmen_order: Mapped[list["InningsBatsman"]] = relationship(back_populates="innings")


class InningsBatsman(Base):
    __tablename__ = "innings_batsmen"
    __table_args__ = (
        UniqueConstraint("innings_id", "position", name="uq_innings_batsman_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    innings_id: Mapped[int] = mapped_column(ForeignKey("innings.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    position: Mapped[int] = mapped_column(Integer)

    innings: Mapped["Innings"] = relationship(back_populates="batsmen_order")


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    innings_id: Mapped[int] = mapped_column(ForeignKey("innings.id"), index=True)
    over_number: Mapped[int] = mapped_column(Integer)
    ball_number: Mapped[int] = mapped_column(Integer)
    striker_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    non_striker_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    bowler_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    runs_batsman: Mapped[int] = mapped_column(Integer, default=0)
    runs_extras: Mapped[int] = mapped_column(Integer, default=0)
    extra_type: Mapped[str] = mapped_column(String(20), default=ExtraType.NONE.value)
    wicket_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dismissed_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)

    innings: Mapped["Innings"] = relationship(back_populates="deliveries")
