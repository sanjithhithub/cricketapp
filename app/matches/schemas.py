from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.enums import MatchStatus, MatchType, TossDecision, coerce_enum


class MatchBase(BaseModel):
    match_date: date
    # 24-hour local start time as "HH:MM". A string rather than a time because the
    # venue's timezone is not recorded on the match, so a bare "14:30" is what
    # actually gets displayed next to the date.
    match_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=5)
    venue: str = Field(..., min_length=1, max_length=200)
    match_type: MatchType
    result: str | None = Field(None, max_length=500)
    team_a_id: int
    team_b_id: int
    toss_winner_id: int
    toss_decision: TossDecision
    referee_1_name: str | None = Field(None, max_length=100)
    referee_2_name: str | None = Field(None, max_length=100)
    match_referee_name: str | None = Field(None, max_length=100)
    # Player of the match on the completed-match summary. Never derived, may be
    # set to a player from either side, and optional until a match is done.
    player_of_match_id: int | None = None
    # A level main match goes to a one-over-per-side Super Over instead of
    # a tied result. super_over_repeat keeps playing extra Super Overs (batting
    # order swapped each time) when the Super Over is itself level.
    super_over_enabled: bool = False
    super_over_repeat: bool = True

    @field_validator("match_type", mode="before")
    @classmethod
    def validate_match_type(cls, v):
        return coerce_enum(MatchType, v)

    @field_validator("toss_decision", mode="before")
    @classmethod
    def validate_toss_decision(cls, v):
        return coerce_enum(TossDecision, v)


class MatchCreate(MatchBase):
    pass


class MatchUpdate(BaseModel):
    match_date: date | None = None
    match_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=5)
    venue: str | None = Field(None, min_length=1, max_length=200)
    match_type: MatchType | None = None
    result: str | None = Field(None, max_length=500)
    team_a_id: int | None = None
    team_b_id: int | None = None
    toss_winner_id: int | None = None
    toss_decision: TossDecision | None = None
    referee_1_name: str | None = Field(None, max_length=100)
    referee_2_name: str | None = Field(None, max_length=100)
    match_referee_name: str | None = Field(None, max_length=100)
    super_over_enabled: bool | None = None
    super_over_repeat: bool | None = None
    player_of_match_id: int | None = None

    @field_validator("match_type", mode="before")
    @classmethod
    def validate_match_type(cls, v):
        if v is None:
            return None
        return coerce_enum(MatchType, v)

    @field_validator("toss_decision", mode="before")
    @classmethod
    def validate_toss_decision(cls, v):
        if v is None:
            return None
        return coerce_enum(TossDecision, v)


class MatchTeamInfo(BaseModel):
    id: int
    name: str
    short_name: str

    class Config:
        from_attributes = True


class MatchResponse(MatchBase):
    id: int
    status: MatchStatus = MatchStatus.SCHEDULED
    current_innings_number: int = 0
    team_a: MatchTeamInfo
    team_b: MatchTeamInfo
    toss_winner: MatchTeamInfo
    player_of_match_id: int | None = None

    class Config:
        from_attributes = True
