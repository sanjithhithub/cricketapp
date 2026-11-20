from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.scoring.enums import (
    DEAD_BALL_WICKETS,
    WICKETS_ON_NO_BALL,
    WICKETS_ON_WIDE,
    ExtraType,
    MatchFormat,
    WicketType,
)


class DeliveryCreate(BaseModel):
    # Field order matters: Pydantic validates in declaration order and only
    # exposes the fields validated so far through `info.data`. `extra_type`
    # must therefore be declared BEFORE the run fields whose validators depend
    # on it, and the run components must be declared before `runs_extras`,
    # whose validators need to see them. Reorder with care.
    striker_id: int
    non_striker_id: int
    bowler_id: int
    extra_type: ExtraType = ExtraType.NONE
    # --- MCC run components -------------------------------------------------
    # Any non-zero component switches the payload into "component shape": the
    # whole extras figure is then the sum of these, which is the only way to
    # say "a no ball that also went for byes". All zero keeps the legacy shape
    # of one `extra_type` plus one `runs_extras` figure, so older clients are
    # untouched. The engine (resolve_delivery_runs) does the actual splitting
    # and is the single source of truth for the totals.
    runs_wides: int = Field(0, ge=0)
    runs_no_balls: int = Field(0, ge=0)
    runs_byes: int = Field(0, ge=0)
    runs_leg_byes: int = Field(0, ge=0)
    # Penalty runs under Law 28 that belong to neither a wide nor a no ball.
    runs_penalty: int = Field(0, ge=0)
    runs_batsman: int = Field(0, ge=0)
    runs_extras: int = Field(0, ge=0)
    # Optional explicit boundary allowance (4 or 6). Omitted means "work it
    # out from the total": anything that leaves 4 or 6 non-penalty runs was a
    # boundary, and a boundary is never a change of ends.
    boundary_runs: int | None = Field(None, ge=0)
    # Whether the batters had crossed at the instant the ball became dead -
    # the fact a run out turns on (Law 38.3). Omitted means derive it from the
    # parity of the completed runs.
    batters_crossed: bool | None = None
    # The umpire disallowing the leg byes: those runs are struck out and the
    # batters stay where they are.
    leg_byes_allowed: bool = True
    # Law 37.3: wilful obstruction that prevented a catch from being taken.
    obstruction_prevented_catch: bool = False
    # Idempotency key. A retry carrying the same id returns the current
    # scorecard instead of scoring the ball a second time.
    client_request_id: str | None = Field(None, max_length=64)
    wicket_type: WicketType | None = None
    dismissed_player_id: int | None = None
    # Optional fielding credit for a caught, run out or stumped dismissal. It
    # never affects the score; omitting it simply leaves the dismissal
    # unattributed, so older clients keep working unchanged.
    fielder_id: int | None = None

    @staticmethod
    def _component_sum(data) -> int:
        return sum(
            data.get(name, 0) or 0
            for name in (
                "runs_wides",
                "runs_no_balls",
                "runs_byes",
                "runs_leg_byes",
                "runs_penalty",
            )
        )

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
    def validate_wicket_on_extra(cls, v, info):
        extra = info.data.get("extra_type")
        if extra == ExtraType.WIDE:
            allowed = WICKETS_ON_WIDE
        elif extra == ExtraType.NO_BALL:
            allowed = WICKETS_ON_NO_BALL
        else:
            allowed = None
        if v is not None and allowed is not None and v not in allowed:
            label = extra.value.replace("_", " ")
            options = ", ".join(w.value.replace("_", " ") for w in allowed)
            raise ValueError(f"only a {options} can be recorded on a {label}")
        if v is not None and extra == ExtraType.DEAD_BALL and v not in DEAD_BALL_WICKETS:
            raise ValueError("a dead ball must record a retired out or timed out dismissal")
        if v is not None and v in DEAD_BALL_WICKETS and extra != ExtraType.DEAD_BALL:
            raise ValueError(
                f"a {v.value.replace('_', ' ')} dismissal must be recorded as a dead ball"
            )
        return v

    @field_validator("runs_extras")
    @classmethod
    def validate_extras_require_type(cls, v, info):
        # `extra_type` is an enum member by now, and a str enum compares equal to
        # its value, so these comparisons read exactly as they did when the field
        # was a plain string. Runs that arrive as components rather than as a
        # single extras figure are exempt from the "must have a type" rule only
        # when the type they do carry is the one those components name.
        if (
            v != 0
            and info.data.get("extra_type") == ExtraType.NONE
            and cls._component_sum(info.data) == 0
        ):
            raise ValueError("extra runs require an extra type")
        if v != 0 and info.data.get("extra_type") == ExtraType.DEAD_BALL:
            raise ValueError("a dead ball carries no runs")
        return v

    @field_validator("runs_extras")
    @classmethod
    def validate_penalty_extra_minimum(cls, v, info):
        # A wide and a no-ball are penalty deliveries worth at least one extra
        # run before any runs scored off the bat. A payload that states the
        # penalty as a component instead of as `runs_extras` has satisfied the
        # rule too, so the components count towards the minimum.
        if (
            info.data.get("extra_type") in (ExtraType.WIDE, ExtraType.NO_BALL)
            and v < 1
            and cls._component_sum(info.data) == 0
        ):
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
        if extra_type == ExtraType.DEAD_BALL and v != 0:
            raise ValueError("a dead ball carries no runs")
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

    @field_validator("fielder_id")
    @classmethod
    def validate_fielder(cls, v, info):
        # A fielder is meaningless on a delivery with no dismissal; it is the
        # mirror of the dismissed_player_id rule above. Whether the wicket type
        # even has a fielder is left to the client - a bowled dismissal may
        # still carry one (a deflection), and older data has none at all.
        if v is not None and info.data.get("wicket_type") is None:
            raise ValueError("fielder_id cannot be set without a wicket")
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
    # Readable form of ``dismissal`` (``c X b Y``, ``lbw b Y``). Optional so a
    # client built against the earlier payload still validates.
    dismissal_text: str | None = None
    did_not_bat: bool

    model_config = ConfigDict(from_attributes=True)


class FallOfWicketOut(BaseModel):
    # One row per wicket, in the order they fell: "23/1 (4.3 ov)".
    wicket_number: int
    score: int
    player_id: int
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
    over_number: int
    ball_number: int
    overs_str: str

    model_config = ConfigDict(from_attributes=True)


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
    # Wides and no-balls the bowler conceded: runs charged to the bowler that
    # are not counted as legal balls of the over.
    wides: int = 0
    no_balls: int = 0

    model_config = ConfigDict(from_attributes=True)


class PartnershipBatterOut(BaseModel):
    """One batsman's share of the stand at the crease."""

    player_id: int
    name: str
    runs: int
    balls: int
    contribution_percentage: float

    model_config = ConfigDict(from_attributes=True)


class PartnershipOut(BaseModel):
    """The current stand, in the shape a live scoreboard shows it.

    ``total_runs`` counts every team run added while the pair were together,
    extras included, so it can be larger than the two batsmen's own runs. A stand
    with no legal ball yet reports a 0.0 run rate.
    """

    batter1: PartnershipBatterOut
    batter2: PartnershipBatterOut
    total_runs: int
    total_balls: int
    run_rate: float

    model_config = ConfigDict(from_attributes=True)


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
    fall_of_wickets: list[FallOfWicketOut] = []
    # The stand at the crease right now, or null between a wicket and the
    # incoming pick (and before the first ball of an innings with fewer than two
    # batsmen). Its total_runs include partnership extras.
    partnership: PartnershipOut | None = None

    model_config = ConfigDict(from_attributes=True)


# --- completed-match summary ------------------------------------------------


class SummaryTeam(BaseModel):
    id: int
    name: str
    short_name: str


class TossSummary(BaseModel):
    winner: SummaryTeam
    decision: str


class InningsSummary(BaseModel):
    innings_number: int
    team: SummaryTeam
    total: int
    wickets: int
    overs_bowled: float
    overs_bowled_str: str
    run_rate: float
    extras: int
    target: int | None
    is_super_over: bool
    completed: bool


class PlayerSummary(BaseModel):
    player_id: int
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
    team_id: int | None = None
    team_name: str | None = None


class TopRunScorerSummary(PlayerSummary):
    # Aggregated across every innings of the match, from either side.
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    strike_rate: float


class BestBowlerSummary(PlayerSummary):
    # Aggregated across every innings of the match, from either side.
    balls_bowled: int
    overs: float
    overs_str: str
    maidens: int
    runs_conceded: int
    wickets: int
    economy: float
    wides: int = 0
    no_balls: int = 0


class HighlightSummary(BaseModel):
    type: str
    text: str
    player_id: int | None = None
    team_id: int | None = None


class MatchSummaryResponse(BaseModel):
    match_id: int
    status: str
    result_text: str | None = None
    winner: SummaryTeam | None = None
    margin: str | None = None
    toss: TossSummary | None = None
    venue: str
    match_date: date
    match_time: str
    match_type: str
    teams: list[SummaryTeam]
    innings: list[InningsSummary]
    player_of_match: PlayerSummary | None = None
    top_run_scorer: TopRunScorerSummary | None = None
    best_bowler: BestBowlerSummary | None = None
    highlights: list[HighlightSummary]


# --- ball-by-ball analytics -------------------------------------------------


class DeliveryEventOut(BaseModel):
    """One delivery in chronological order, with its running match state."""

    over_number: int
    ball_number: int
    ball_label: str
    striker_id: int
    striker_name: str | None = None
    non_striker_id: int
    non_striker_name: str | None = None
    bowler_id: int
    bowler_name: str | None = None
    runs_batsman: int
    runs_extras: int
    total_runs: int
    extra_type: str
    is_legal_ball: bool
    is_boundary: bool = False
    is_four: bool = False
    is_six: bool = False
    is_dot_ball: bool = False
    wicket_type: str | None = None
    dismissed_player_id: int | None = None
    dismissed_player_name: str | None = None
    team_total: int
    team_wickets: int
    legal_balls: int
    run_rate: float
    display: str


class ExtrasBreakdownOut(BaseModel):
    wides: int = 0
    no_balls: int = 0
    byes: int = 0
    leg_byes: int = 0
    total: int = 0


class ScoringPatternOut(BaseModel):
    """How the runs were made: dot balls and the run-scoring shot pattern."""

    deliveries: int
    legal_balls: int
    dot_balls: int
    singles: int
    doubles: int
    triples: int
    fours: int
    sixes: int
    boundaries: int
    boundary_runs: int
    runs_off_bat: int
    extras: int


class OverSummaryOut(BaseModel):
    over_number: int
    bowler_id: int | None = None
    bowler_name: str | None = None
    legal_balls: int
    runs: int
    wickets: int
    extras: int
    fours: int
    sixes: int
    dot_balls: int
    run_rate: float
    cumulative_runs: int
    cumulative_wickets: int
    cumulative_run_rate: float


class RunRatePointOut(BaseModel):
    over_number: int
    legal_balls: int
    cumulative_runs: int
    cumulative_wickets: int
    run_rate: float
    required_run_rate: float | None = None


class InningsAnalyticsOut(BaseModel):
    innings_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    is_super_over: bool
    total: int
    wickets: int
    legal_balls: int
    overs_bowled: float
    overs_bowled_str: str
    run_rate: float
    target: int | None
    extras: ExtrasBreakdownOut
    scoring_pattern: ScoringPatternOut
    deliveries: list[DeliveryEventOut]
    over_by_over: list[OverSummaryOut]
    run_rate_progression: list[RunRatePointOut]


class MatchAnalyticsResponse(BaseModel):
    match_id: int
    innings: list[InningsAnalyticsOut]
