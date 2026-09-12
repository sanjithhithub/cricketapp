from pydantic import BaseModel


class TeamBase(BaseModel):
    name: str
    short_name: str
    logo: str | None = None
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
    name: str | None = None
    short_name: str | None = None
    logo: str | None = None
    homeground: str | None = None
    founder: str | None = None
    founded_year: int | None = None
    owner: str | None = None
    country_id: int | None = None
    state_id: int | None = None
    city_id: int | None = None
    level_id: int | None = None


class PlayerOnTeam(BaseModel):
    id: int
    first_name: str
    last_name: str
    profile_image: str | None = None
    country_code: str
    mobile_number: int
    email: str

    class Config:
        from_attributes = True


class TeamResponse(TeamBase):
    id: int

    class Config:
        from_attributes = True


class TeamOption(BaseModel):
    id: int
    name: str
    short_name: str
    logo: str | None = None

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
    profile_image: str | None = None
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
    bench: list[SquadPlayer] = []


class TeamDetailResponse(TeamBase):
    id: int
    country: str | None = None
    state: str | None = None
    city: str | None = None
    level: str | None = None
    total: int = 0
    playing_11: list[SquadPlayer] = []
    substitutes: list[SquadPlayer] = []
    bench: list[SquadPlayer] = []

    class Config:
        from_attributes = True
