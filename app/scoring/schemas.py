from pydantic import BaseModel, Field, field_validator

from app.scoring.enums import ExtraType, MatchFormat, WicketType


class DeliveryCreate(BaseModel):
    # Field order matters: Pydantic validates in declaration order and only
    # exposes the fields validated so far through `info.data`. `extra_type`
    # must therefore be declared BEFORE the run fields whose validators depend
    # on it, otherwise those validators silently never run.
    striker_id: int
    non_striker_id: int
    bowler_id: int
    extra_type: ExtraType = ExtraType.NONE
    runs_batsman: int = Field(0, ge=0)
    runs_extras: int = Field(0, ge=0)
    wicket_type: WicketType | None = None
    dismissed_player_id: int | None = None

    @field_validator("wicket_type", mode="before")
    @classmethod
    def validate_wicket_type(cls, v):
        if v is None or v == "":
            return None
        # An empty string is how a client says "no wicket"; anything else must be
        # a real dismissal, and coercion turns it into the documented member.
        return WicketType(v) if not isinstance(v, WicketType) else v

    @field_validator("wicket_type")
    @classmethod
    def validate_wicket_on_penalty_extra(cls, v, info):
        # A wide / no-ball is dead by the time it reaches the batsman, so the
        # only wicket it can carry is a run out.
        if (
            v is not None
            and info.data.get("extra_type") in (ExtraType.WIDE, ExtraType.NO_BALL)
            and v != WicketType.RUN_OUT
        ):
            raise ValueError("only a run out can be recorded on a wide or no-ball")
        return v

    @field_validator("runs_extras")
    @classmethod
    def validate_extras_require_type(cls, v, info):
        # `extra_type` is an enum member by now, and a str enum compares equal to
        # its value, so these comparisons read exactly as they did when the field
        # was a plain string.
        if v != 0 and info.data.get("extra_type") == ExtraType.NONE:
            raise ValueError("extra runs require an extra type")
        return v

    @field_validator("runs_extras")
    @classmethod
    def validate_penalty_extra_minimum(cls, v, info):
        # A wide and a no-ball are penalty deliveries worth at least one extra
        # run before any runs scored off the bat.
        if info.data.get("extra_type") in (ExtraType.WIDE, ExtraType.NO_BALL) and v < 1:
            raise ValueError("a wide or no-ball is worth at least 1 extra run")
        return v

    @field_validator("runs_batsman")
    @classmethod
    def validate_run_attribution(cls, v, info):
        extra_type = info.data.get("extra_type")
        if extra_type == ExtraType.WIDE and v != 0:
            raise ValueError("runs on a wide must be recorded as extras")
        if extra_type in (ExtraType.BYE, ExtraType.LEG_BYE) and v != 0:
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


class AddBatsmanRequest(BaseModel):
    player_id: int


class BatsmanCardOut(BaseModel):
    player_id: int
    position: int
    # Name and permanent code, so a card can show who batted even when two
    # players share a name. Additive: existing consumers still get player_id.
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
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
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
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
    extras: int
    target: int | None
    completed: bool
    end_reason: str | None
    max_overs: int | None = None
    last_over_bowler_id: int | None = None
    is_super_over: bool = False
    # True when a wicket fell and the innings is waiting for a new batsman to
    # be picked. At least one of striker_id / non_striker_id is None whenever
    # this is set.
    #
    # This is the only flag for "the innings needs a batsman". It used to have a
    # twin, `order_exhausted`, set from the very same expression - so the two
    # were always equal, and the name promised something the value never said:
    # it fired on the seventh wicket, long before the batting order could
    # actually be exhausted. A consumer that took it at face value would stop
    # offering new batsmen while eleven were still at the crease.
    awaiting_batsman: bool = False
    match_result: str | None = None
    striker_id: int | None
    non_striker_id: int | None
    batsmen: list[BatsmanCardOut]
    bowlers: list[BowlerCardOut]

    class Config:
        from_attributes = True
