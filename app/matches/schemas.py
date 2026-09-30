from datetime import date

from pydantic import BaseModel, field_validator


class MatchBase(BaseModel):
    match_date: date
    match_time: str
    venue: str
    match_type: str
    result: str | None = None
    team_a_id: int
    team_b_id: int
    toss_winner_id: int
    toss_decision: str
    referee_1_name: str | None = None
    referee_2_name: str | None = None
    match_referee_name: str | None = None
    # A level main match goes to a one-over-per-side Super Over instead of
    # a tied result. super_over_repeat keeps playing extra Super Overs (batting
    # order swapped each time) when the Super Over is itself level.
    super_over_enabled: bool = False
    super_over_repeat: bool = True

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, v):
        if v not in ("T20", "ODI", "Test"):
            raise ValueError("match_type must be T20, ODI, or Test")
        return v

    @field_validator("toss_decision")
    @classmethod
    def validate_toss_decision(cls, v):
        if v not in ("bat", "bowl"):
            raise ValueError("toss_decision must be bat or bowl")
        return v


class MatchCreate(MatchBase):
    pass


class MatchUpdate(BaseModel):
    match_date: date | None = None
    match_time: str | None = None
    venue: str | None = None
    match_type: str | None = None
    result: str | None = None
    team_a_id: int | None = None
    team_b_id: int | None = None
    toss_winner_id: int | None = None
    toss_decision: str | None = None
    referee_1_name: str | None = None
    referee_2_name: str | None = None
    match_referee_name: str | None = None
    super_over_enabled: bool | None = None
    super_over_repeat: bool | None = None

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, v):
        if v is not None and v not in ("T20", "ODI", "Test"):
            raise ValueError("match_type must be T20, ODI, or Test")
        return v

    @field_validator("toss_decision")
    @classmethod
    def validate_toss_decision(cls, v):
        if v is not None and v not in ("bat", "bowl"):
            raise ValueError("toss_decision must be bat or bowl")
        return v


class MatchTeamInfo(BaseModel):
    id: int
    name: str
    short_name: str

    class Config:
        from_attributes = True


class MatchResponse(MatchBase):
    id: int
    status: str = "scheduled"
    current_innings_number: int = 0
    team_a: MatchTeamInfo
    team_b: MatchTeamInfo
    toss_winner: MatchTeamInfo

    class Config:
        from_attributes = True
