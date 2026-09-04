from datetime import date
from typing import Optional
from pydantic import BaseModel, field_validator


class MatchBase(BaseModel):
    match_date: date
    match_time: str
    venue: str
    match_type: str
    result: Optional[str] = None
    team_a_id: int
    team_b_id: int
    toss_winner_id: int
    toss_decision: str
    referee_1_name: Optional[str] = None
    referee_2_name: Optional[str] = None
    match_referee_name: Optional[str] = None

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
    match_date: Optional[date] = None
    match_time: Optional[str] = None
    venue: Optional[str] = None
    match_type: Optional[str] = None
    result: Optional[str] = None
    team_a_id: Optional[int] = None
    team_b_id: Optional[int] = None
    toss_winner_id: Optional[int] = None
    toss_decision: Optional[str] = None
    referee_1_name: Optional[str] = None
    referee_2_name: Optional[str] = None
    match_referee_name: Optional[str] = None

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
    team_a: MatchTeamInfo
    team_b: MatchTeamInfo
    toss_winner: MatchTeamInfo

    class Config:
        from_attributes = True
