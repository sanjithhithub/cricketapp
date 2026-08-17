from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


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

    players: Mapped[list["Player"]] = relationship(back_populates="team")
    country: Mapped["Country"] = relationship()
    state: Mapped["State"] = relationship()
    city: Mapped["City"] = relationship()
