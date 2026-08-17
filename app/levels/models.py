from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class TeamLevel(Base):
    __tablename__ = "team_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)

    teams: Mapped[list["Team"]] = relationship(back_populates="level")
