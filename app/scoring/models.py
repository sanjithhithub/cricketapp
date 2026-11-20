from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
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
    # True for Super Over innings (one over per side). These live in the same
    # table/engine so deliveries reuse all existing scoring logic, but they are
    # separate innings (numbered 3, 4, 5, ...) so they never corrupt the normal
    # match statistics.
    is_super_over: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A batsman chosen by hand for the next vacancy at the crease (the "who
    # comes in next?" pick). It overrides the positional batting order for
    # exactly one wicket, and is cleared as soon as a delivery is bowled with
    # that player at the crease. NULL means "no manual pick pending", so the
    # replay falls back to the seeded order.
    next_batsman_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)

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
    # A delivery is submitted once per attempt: a client retry carrying the same
    # ``client_request_id`` must not score the ball twice. NULL is the normal
    # case (an older client, or one that does not care), and SQLite/Postgres
    # both treat NULLs as distinct so every legacy row stays legal.
    __table_args__ = (
        UniqueConstraint(
            "innings_id",
            "client_request_id",
            name="uq_deliveries_innings_client_request",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    innings_id: Mapped[int] = mapped_column(ForeignKey("innings.id"), index=True)
    over_number: Mapped[int] = mapped_column(Integer)
    ball_number: Mapped[int] = mapped_column(Integer)
    striker_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    non_striker_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    bowler_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    runs_batsman: Mapped[int] = mapped_column(Integer, default=0)
    # ``runs_extras`` is kept as the single "how many extras" number every
    # existing query reads, and is invariant to the component split below:
    # runs_extras == runs_wides + runs_no_balls + runs_byes + runs_leg_byes +
    # runs_penalty. The components are what make a no-ball with byes on it, or
    # a wide that raced to the boundary, representable at all - one extra_type
    # cannot say both "this was a no ball" and "two of these were byes".
    runs_extras: Mapped[int] = mapped_column(Integer, default=0)
    extra_type: Mapped[str] = mapped_column(String(20), default=ExtraType.NONE.value)
    # --- run components (MCC Laws of Cricket) ------------------------------
    # Wides carry their own mandatory penalty run inside ``runs_wides``; so do
    # no-balls inside ``runs_no_balls``. ``runs_penalty`` is for penalty runs
    # awarded under Law 28 that belong to neither. Byes and leg byes are never
    # charged to the bowler and never credited to a batter.
    runs_wides: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runs_no_balls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runs_byes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runs_leg_byes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runs_penalty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How the total was made, recorded rather than re-derived on every read.
    # NULL means "not recorded" (a row written before this column existed, or
    # inserted by a test that bypasses the engine) and readers fall back to
    # computing it from the components, so NULL is never a wrong answer.
    completed_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    boundary_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_team_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Whether the two batters had crossed ends when this ball became dead -
    # the fact a run out turns on (Law 38.3). NULL on legacy rows, where the
    # parity of the completed runs is all that can be known.
    batters_crossed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Client-supplied idempotency key; see the unique constraint above.
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wicket_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dismissed_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    # The fielder credited with a caught, run out or stumped dismissal. Optional
    # because older deliveries predate the column and because bowled/lbw/hit
    # wicket have no fielder; NULL means "not attributed", never "no one".
    fielder_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)

    innings: Mapped["Innings"] = relationship(back_populates="deliveries")
