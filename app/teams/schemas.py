from pydantic import BaseModel
from typing import Optional


class TeamBase(BaseModel):
    name: str
    short_name: str
    logo: Optional[str] = None
    homeground: str
    founder: str
    founded_year: int
    owner: str
    country_id: int
    state_id: int
    city_id: int
    level_id: int


class TeamCreate(TeamBase):
    pass


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    short_name: Optional[str] = None
    logo: Optional[str] = None
    homeground: Optional[str] = None
    founder: Optional[str] = None
    founded_year: Optional[int] = None
    owner: Optional[str] = None
    country_id: Optional[int] = None
    state_id: Optional[int] = None
    city_id: Optional[int] = None
    level_id: Optional[int] = None


class PlayerOnTeam(BaseModel):
    id: int
    first_name: str
    last_name: str
    profile_image: Optional[str] = None
    country_code: str
    mobile_number: int
    email: str

    class Config:
        from_attributes = True


class TeamResponse(TeamBase):
    id: int

    class Config:
        from_attributes = True


class TeamPlayerAdd(BaseModel):
    country_code: str
    mobile_number: int


class TeamPlayerAddById(BaseModel):
    player_id: int


class TeamBulkPlayerAdd(BaseModel):
    player_ids: list[int]


class SquadPlayer(BaseModel):
    id: int
    first_name: str
    last_name: str
    profile_image: Optional[str] = None
    role: str

    class Config:
        from_attributes = True


class TeamSquadResponse(BaseModel):
    team_id: int
    team_name: str
    level: str
    total: int
    playing_11: list[SquadPlayer]
    substitutes: list[SquadPlayer]
