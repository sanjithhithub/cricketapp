from pydantic import BaseModel, Field, field_validator

from app.enums import SquadRole, coerce_enum
from app.players.schemas import PlayerTeamInfo


class TeamBase(BaseModel):
    # Lengths mirror the columns in app.teams.models, so a value that validates
    # here is a value the database can hold - the server no longer accepts a
    # request that Postgres would then truncate or reject.
    name: str = Field(..., min_length=1, max_length=100)
    short_name: str = Field(..., min_length=1, max_length=10)
    logo: str | None = Field(None, max_length=500)
    homeground: str = Field(..., min_length=1, max_length=200)
    founder: str = Field(..., min_length=1, max_length=100)
    founded_year: int = Field(..., ge=1800, le=2200)
    owner: str = Field(..., min_length=1, max_length=200)
    country_id: int
    state_id: int
    city_id: int
    level_id: int


class TeamCreate(TeamBase):
    pass


class TeamUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    short_name: str | None = Field(None, min_length=1, max_length=10)
    logo: str | None = Field(None, max_length=500)
    homeground: str | None = Field(None, min_length=1, max_length=200)
    founder: str | None = Field(None, min_length=1, max_length=100)
    founded_year: int | None = Field(None, ge=1800, le=2200)
    owner: str | None = Field(None, min_length=1, max_length=200)
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
    # A string, and nullable: a number may be absent, and an integer could hold
    # neither a leading zero nor a country code alongside the national number.
    mobile_number: str | None = None
    player_code: str | None = None
    email: str

    class Config:
        from_attributes = True


class TeamLogoResponse(BaseModel):
    """200 from ``POST /teams/{team_id}/upload-logo``.

    ``logo`` is a root-relative path (``/uploads/teams/17.png``), routable as-is
    from any page and also the location the CDN redirect serves from.
    """

    team_id: int
    logo: str


class TeamPlayerAddResponse(BaseModel):
    """201 from ``POST /teams/{team_id}/players``."""

    message: str
    assignment: PlayerTeamInfo


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


class TeamListItem(BaseModel):
    id: int
    name: str
    short_name: str
    logo: str | None = None
    level: str | None = None
    total_players: int = 0
    playing_11: int = 0
    substitutes: int = 0
    bench: int = 0


class TeamPlayerAdd(BaseModel):
    country_code: str
    # String, so a number survives as typed. Kept for the assign-by-number
    # endpoint; it refuses a number shared by more than one player rather than
    # guessing which one was meant.
    mobile_number: str


class TeamPlayerAddById(BaseModel):
    player_id: int


class TeamBulkPlayerAdd(BaseModel):
    """A selected group of players, identified by id.

    Ids, not names: a name is not unique in this app. A repeated id here is
    rejected by the server rather than silently de-duplicated, so a UI bug that
    lets a player be picked twice shows up instead of quietly shrinking the XI.
    """

    player_ids: list[int]
    role: SquadRole = SquadRole.PLAYING_11

    @field_validator("player_ids")
    @classmethod
    def validate_player_ids(cls, v):
        if not v:
            raise ValueError("select at least one player")
        if any(pid <= 0 for pid in v):
            raise ValueError("player_ids must be positive player ids")
        return v

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        return coerce_enum(SquadRole, v)


class PlayerBulkAssignResponse(BaseModel):
    message: str
    team_id: int
    role: SquadRole
    added_player_ids: list[int]


class SquadPlayer(BaseModel):
    id: int
    player_code: str = ""
    first_name: str
    last_name: str
    full_name: str = ""
    profile_image: str | None = None
    role: SquadRole
    # Masked, not raw: a squad is a dropdown, and a dropdown is where a scorer
    # has to tell two same-named players apart. The last two digits are the part
    # that separates them. The full number stays on the player record, which is
    # owner-scoped, so only the owner can read it in full.
    mobile_number: str | None = None
    is_phone_verified: bool = False
    # Captaincy travels with the squad row so a scorer sees who leads the side
    # without a second lookup. Both false means the team has not named one.
    is_captain: bool = False
    is_vice_captain: bool = False

    class Config:
        from_attributes = True


class TeamCaptainsSet(BaseModel):
    """Name a team's captain and vice-captain.

    Ids, not names, for the same reason the batting order takes ids: a name is not
    a handle in this app, and two squad members may share one. Both must be
    playing XI members and must be different people - the server enforces that,
    rather than trusting the picker to have offered only valid choices.
    """

    captain_player_id: int | None = None
    vice_captain_player_id: int | None = None

    @field_validator("captain_player_id", "vice_captain_player_id")
    @classmethod
    def validate_ids(cls, v):
        if v is not None and v <= 0:
            raise ValueError("player ids must be positive")
        return v


class TeamCaptainsResponse(BaseModel):
    """The captaincy that is now set on this team.

    Both ids are required and nullable: they are always present in the body, and
    ``null`` is how "this side has named no captain" is expressed. They used to be
    optional, which meant a client reading ``captain_player_id`` after a clear
    could get ``undefined`` - a value the client type did not allow and which no
    amount of null-checking handled.
    """

    message: str
    team_id: int
    captain_player_id: int | None
    vice_captain_player_id: int | None


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
