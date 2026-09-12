from pydantic import BaseModel, field_validator

from app.scoring.enums import ExtraType, MatchFormat, WicketType


class DeliveryCreate(BaseModel):
    striker_id: int
    non_striker_id: int
    bowler_id: int
    runs_batsman: int = 0
    runs_extras: int = 0
    extra_type: str = "none"
    wicket_type: str | None = None
    dismissed_player_id: int | None = None

    @field_validator("extra_type")
    @classmethod
    def validate_extra_type(cls, v):
        if v not in {t.value for t in ExtraType}:
            raise ValueError("extra_type must be one of none, wide, no_ball, bye, leg_bye")
        return v

    @field_validator("wicket_type")
    @classmethod
    def validate_wicket_type(cls, v):
        if v is not None and v not in {t.value for t in WicketType}:
            raise ValueError(
                "wicket_type must be one of bowled, caught, lbw, run_out, stumped, hit_wicket"
            )
        return v

    @field_validator("runs_batsman", "runs_extras")
    @classmethod
    def validate_non_negative(cls, v):
        if v is None or v < 0:
            raise ValueError("runs cannot be negative")
        return v

    @field_validator("runs_batsman")
    @classmethod
    def validate_run_attribution(cls, v, info):
        extra_type = info.data.get("extra_type")
        if extra_type == "wide" and v != 0:
            raise ValueError("runs on a wide must be recorded as extras")
        if extra_type in ("bye", "leg_bye") and v != 0:
            raise ValueError("bye/leg-bye runs must be recorded as extras")
        return v

    @field_validator("dismissed_player_id")
    @classmethod
    def validate_dismissal(cls, v, info):
        wicket_type = info.data.get("wicket_type")
        striker = info.data.get("striker_id")
        non_striker = info.data.get("non_striker_id")
        if wicket_type is not None and v is None:
            raise ValueError("dismissed_player_id is required when a wicket falls")
        if wicket_type is None and v is not None:
            raise ValueError("dismissed_player_id cannot be set without a wicket")
        if v is not None and v not in (striker, non_striker):
            raise ValueError("dismissed player must be the striker or non-striker")
        return v


class MatchCreate(BaseModel):
    team_a_id: int
    team_b_id: int
    venue: str
    format: str
    toss_winner_id: int
    toss_decision: str

    @field_validator("format")
    @classmethod
    def validate_format(cls, v):
        if v not in {f.value for f in MatchFormat}:
            raise ValueError("format must be T20, ODI, or TEST")
        return v

    @field_validator("toss_decision")
    @classmethod
    def validate_toss_decision(cls, v):
        if v not in ("bat", "bowl"):
            raise ValueError("toss_decision must be bat or bowl")
        return v


class StartInningsRequest(BaseModel):
    batting_order: list[int]

    @field_validator("batting_order")
    @classmethod
    def validate_batting_order(cls, v):
        if len(v) < 2:
            raise ValueError("batting_order must list at least two players")
        if len(v) > 11:
            raise ValueError("batting_order cannot have more than 11 players")
        if len(set(v)) != len(v):
            raise ValueError("batting_order cannot contain duplicate players")
        return v


class BatsmanCardOut(BaseModel):
    player_id: int
    position: int
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    strike_rate: float
    out: bool
    dismissal: str | None
    did_not_bat: bool

    class Config:
        from_attributes = True


class BowlerCardOut(BaseModel):
    player_id: int
    balls_bowled: int
    overs: float
    overs_str: str
    maidens: int
    runs_conceded: int
    wickets: int
    economy: float

    class Config:
        from_attributes = True


class ScorecardResponse(BaseModel):
    innings_id: int
    match_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    total: int
    wickets: int
    legal_balls: int
    overs_bowled: float
    overs_bowled_str: str
    current_run_rate: float
    target: int | None
    completed: bool
    end_reason: str | None
    batsmen: list[BatsmanCardOut]
    bowlers: list[BowlerCardOut]

    class Config:
        from_attributes = True
