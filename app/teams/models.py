from sqlalchemy import String, Integer, ForeignKey, BigInteger, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


team_players = Table(
    "team_players",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id"), primary_key=True),
    Column("player_id", ForeignKey("players.id"), primary_key=True),
)


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

    players: Mapped[list["Player"]] = relationship(secondary=team_players)
    country: Mapped["Country"] = relationship()
    state: Mapped["State"] = relationship()
    city: Mapped["City"] = relationship()
