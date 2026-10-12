"""Response bodies for the visualization endpoints.

Every chart ships as a bare data structure a charting client can pass almost
straight to its series builder: a bar chart reads ``overs[*].runs``, a run
chart reads ``progression[*].cumulative_runs``, and so on. No label is derived
client-side; each row carries the ids and names it needs.
"""

from datetime import date

from pydantic import BaseModel, Field


class InningsVisualizationFrame(BaseModel):
    innings_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    is_super_over: bool
    target: int | None = None
    total: int | None = None
    wickets: int | None = None
    legal_balls: int | None = None
    overs_bowled_str: str | None = None


# --- Match charts ----------------------------------------------------------


class OverPoint(BaseModel):
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


class RunsByOverInnings(InningsVisualizationFrame):
    overs: list[OverPoint]


class RunsByOverResponse(BaseModel):
    match_id: int
    innings: list[RunsByOverInnings]


class ProgressionPoint(BaseModel):
    over_number: int
    legal_balls: int
    cumulative_runs: int
    cumulative_wickets: int
    run_rate: float
    required_run_rate: float | None = None


class InningsProgressionInnings(InningsVisualizationFrame):
    progression: list[ProgressionPoint]


class InningsProgressionResponse(BaseModel):
    match_id: int
    innings: list[InningsProgressionInnings]


class PlayerRunsEntry(BaseModel):
    player_id: int
    player_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    team_id: int | None = None
    innings: int
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    highest_score: int = 0
    highest_score_not_out: bool = False
    highest_score_display: str = ""
    strike_rate: float


class PlayerRunsResponse(BaseModel):
    match_id: int
    players: list[PlayerRunsEntry]


class BowlerStatsEntry(BaseModel):
    player_id: int
    player_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    team_id: int | None = None
    innings: int
    balls_bowled: int
    overs: float
    overs_str: str
    runs_conceded: int
    wickets: int
    maidens: int
    economy: float


class BowlerStatsResponse(BaseModel):
    match_id: int
    bowlers: list[BowlerStatsEntry]


class RunDistribution(BaseModel):
    dot_balls: int = Field(description="Legal balls with no runs at all.")
    ones: int
    twos: int
    threes: int
    fours: int
    sixes: int
    other: int = Field(description="Legal balls scoring any other number of runs.")
    legal_balls: int
    deliveries: int = Field(description="Every ball bowled, including wides/no-balls.")
    runs_off_bat: int
    extras: int


class ExtrasBreakdown(BaseModel):
    wides: int = 0
    no_balls: int = 0
    byes: int = 0
    leg_byes: int = 0
    total: int = 0


class RunDistributionInnings(InningsVisualizationFrame):
    distribution: RunDistribution
    extras_breakdown: ExtrasBreakdown


class RunDistributionResponse(BaseModel):
    match_id: int
    match_totals: RunDistribution
    innings: list[RunDistributionInnings]


class PartnershipEntry(BaseModel):
    partnership_number: int
    batsman_1_id: int
    batsman_1_name: str
    batsman_2_id: int
    batsman_2_name: str
    runs: int
    balls: int
    for_wicket: int
    unbroken: bool
    player_out_id: int | None = None
    player_out_name: str | None = None


class PartnershipsInnings(InningsVisualizationFrame):
    partnerships: list[PartnershipEntry]


class PartnershipsResponse(BaseModel):
    match_id: int
    innings: list[PartnershipsInnings]


# --- Team charts -----------------------------------------------------------


class TeamFormPoint(BaseModel):
    match_id: int
    match_date: date
    match_type: str
    opponent_id: int
    opponent_name: str | None = None
    result: str
    result_code: str
    team_score: int | None = None
    opponent_score: int | None = None
    margin: str | None = None


class WinLossTrendPoint(TeamFormPoint):
    cumulative_wins: int
    cumulative_losses: int
    cumulative_draws: int
    cumulative_matches: int
    win_percentage: float


class WinLossTrendResponse(BaseModel):
    team_id: int
    team_name: str
    short_name: str
    matches_played: int
    wins: int
    losses: int
    draws: int
    win_percentage: float
    trend: list[WinLossTrendPoint]


class RecentFormResponse(BaseModel):
    team_id: int
    team_name: str
    short_name: str
    recent_form: list[TeamFormPoint]
    recent_form_string: str = Field(
        description="One letter per result, newest first, e.g. \"WWLDW\"."
    )
