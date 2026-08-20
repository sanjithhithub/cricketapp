from datetime import datetime
from sqlalchemy import String, ForeignKey, BigInteger, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from app.players.models import Player
from app.teams.models import Team
from app.levels.models import TeamLevel
from app.teams.models import PlayerTeamAssignment


class Country(Base):
    __tablename__ = "countries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    states: Mapped[list["State"]] = relationship(back_populates="country")


class State(Base):
    __tablename__ = "states"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    country: Mapped["Country"] = relationship(back_populates="states")
    cities: Mapped[list["City"]] = relationship(back_populates="state")


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"))
    state: Mapped["State"] = relationship(back_populates="cities")


class OTP(Base):
    __tablename__ = "otps"

    id: Mapped[int] = mapped_column(primary_key=True)
    country_code: Mapped[str] = mapped_column(String(5))
    mobile_number: Mapped[int] = mapped_column(BigInteger)
    otp_code: Mapped[str] = mapped_column(String(6))
    session_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
