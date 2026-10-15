"""Response bodies for the account-wide leaderboards.

One entry class backs each side's boards: every ranked row is the same
aggregated player, and only ``value`` - the metric the board sorted by - changes
between them. A client renders a board by iterating its list; it does not need to
know which metric each board uses to show the row, because ``value`` is already
set.
"""

from pydantic import BaseModel, Field


class BattingLeaderboardEntry(BaseModel):
    position: int
    player_id: int
    player_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    team_id: int | None = None
    team_name: str | None = None
    team_short_name: str | None = None
    matches: int
    innings: int
    not_outs: int
    dismissals: int
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    highest_score: int
    highest_score_not_out: bool
    highest_score_display: str = Field(description='The best innings, e.g. "104*" when unbeaten.')
    # None while the player has never been dismissed: an average of runs/0 is
    # undefined, not zero, and the average board refuses to rank one.
    average: float | None = None
    strike_rate: float
    value: float = Field(description="The metric this board ranked the row by.")


class BattingLeaderboardsResponse(BaseModel):
    scope: str = "account"
    matches_considered: int = Field(description="Completed matches the boards were built from.")
    min_balls_faced: int = Field(description="The strike-rate board's bottom limit on balls faced.")
    min_dismissals: int = Field(description="The average board's bottom limit on dismissals.")
    most_runs: list[BattingLeaderboardEntry]
    highest_strike_rate: list[BattingLeaderboardEntry]
    most_fours: list[BattingLeaderboardEntry]
    most_sixes: list[BattingLeaderboardEntry]
    highest_score: list[BattingLeaderboardEntry]
    best_batting_average: list[BattingLeaderboardEntry]


class BowlingLeaderboardEntry(BaseModel):
    position: int
    player_id: int
    player_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    team_id: int | None = None
    team_name: str | None = None
    team_short_name: str | None = None
    matches: int
    innings: int
    balls_bowled: int
    overs: float = Field(description="Legal balls as decimal overs, e.g. 2.17 for 13.")
    overs_str: str = Field(description='Overs in cricket notation, e.g. "2.1".')
    maidens: int
    runs_conceded: int
    wickets: int
    economy: float
    wides: int
    no_balls: int
    dot_balls: int
    best_figures_wickets: int
    best_figures_runs: int
    best_figures_balls: int
    best_figures_display: str = Field(description='Best spell, e.g. "4/23".')
    value: float = Field(description="The metric this board ranked the row by.")


class BowlingLeaderboardsResponse(BaseModel):
    scope: str = "account"
    matches_considered: int = Field(description="Completed matches the boards were built from.")
    min_overs: float = Field(description="The economy board's bottom limit on overs bowled.")
    most_wickets: list[BowlingLeaderboardEntry]
    best_economy: list[BowlingLeaderboardEntry]
    best_bowling_figures: list[BowlingLeaderboardEntry]
    most_dot_balls: list[BowlingLeaderboardEntry]
    most_maidens: list[BowlingLeaderboardEntry]


class FieldingLeaderboardEntry(BaseModel):
    position: int
    player_id: int
    player_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    team_id: int | None = None
    team_name: str | None = None
    team_short_name: str | None = None
    matches: int
    innings: int
    catches: int
    run_outs: int
    stumpings: int
    total_dismissals: int = Field(description="Catches + run outs + stumpings for this fielder.")
    value: float = Field(description="The metric this board ranked the row by.")


class FieldingLeaderboardsResponse(BaseModel):
    scope: str = "account"
    matches_considered: int = Field(description="Completed matches the boards were built from.")
    # Only dismissals the delivery log actually attributes to a fielder appear
    # here; a match scored before attribution existed contributes nothing.
    most_catches: list[FieldingLeaderboardEntry]
    most_run_outs: list[FieldingLeaderboardEntry]
    most_stumpings: list[FieldingLeaderboardEntry]
