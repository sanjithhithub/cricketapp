from datetime import date
from sqlalchemy import String, Integer, Float, Date, ForeignKey, BigInteger, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        UniqueConstraint("country_code", "mobile_number", name="uq_player_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(10))
    profile_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    batting_hand: Mapped[str] = mapped_column(String(1))
    batting_position: Mapped[str] = mapped_column(String(5))
    bowling_hand: Mapped[str] = mapped_column(String(1))
    bowling_type: Mapped[str] = mapped_column(String(10))
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"))
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    height: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float)
    country_code: Mapped[str] = mapped_column(String(5))
    mobile_number: Mapped[int] = mapped_column(BigInteger)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    is_phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)

    team: Mapped["Team | None"] = relationship(back_populates="players")
