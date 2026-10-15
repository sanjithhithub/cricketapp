"""Response bodies for the deep-analytics endpoints.

Two families live here. The partnership bodies describe the stands a pair of
batsmen put together before a wicket fell, one list per innings plus the two
stands a scorecard calls out - the opening pair and the biggest stand. The career
bodies describe a single player's record split every way a selectors' page needs
it: season, competition, tournament and team, with the career total and a recent
innings log on top.

Both reuse the player stat blocks from :mod:`app.players.schemas`, so a career
figure and the same figure on a profile are the same shape and the same number.
"""

from datetime import date

from pydantic import BaseModel, Field

from app.players.schemas import (
    PlayerBattingStats,
    PlayerBowlingStats,
    PlayerFieldingStats,
)

# --- Partnership analysis ---------------------------------------------------


class PartnershipEntry(BaseModel):
    partnership_number: int = Field(description="1 for the opening stand, then 2, 3, ...")
    batsman_1_id: int
    batsman_1_name: str
    batsman_2_id: int
    batsman_2_name: str
    runs: int = Field(description="Runs added while the pair were together.")
    balls: int = Field(description="Legal balls faced together (wides/no-balls excluded).")
    run_rate: float = Field(description="Partnership runs per over.")
    for_wicket: int = Field(description="The wicket this stand is the fall for, e.g. 1.")
    unbroken: bool = Field(description="True when the stand was still together at the end.")
    player_out_id: int | None = None
    player_out_name: str | None = None


class PartnershipInnings(BaseModel):
    innings_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    is_super_over: bool
    total: int | None = None
    wickets: int | None = None
    legal_balls: int | None = None
    overs_bowled_str: str | None = None
    partnership_count: int
    # The first stand of the innings, and the biggest one. Null only for an
    # innings with no deliveries.
    opening_partnership: PartnershipEntry | None = None
    highest_partnership: PartnershipEntry | None = None
    partnerships: list[PartnershipEntry] = []


class PartnershipAnalysisResponse(BaseModel):
    match_id: int
    # The biggest stand anywhere in the match, so a caller does not have to walk
    # every innings to find it.
    highest_partnership: PartnershipEntry | None = None
    innings: list[PartnershipInnings]


# --- Player career statistics -----------------------------------------------


class CareerStatsBlock(BaseModel):
    """One slice of a career: the total, or a season / competition / team split."""

    matches_played: int
    innings_played: int
    batting: PlayerBattingStats
    bowling: PlayerBowlingStats
    fielding: PlayerFieldingStats


class SeasonSplit(CareerStatsBlock):
    season: int = Field(description="Calendar year the match was played in.")


class CompetitionSplit(CareerStatsBlock):
    # The TeamLevel a side plays at: the app's existing competition vocabulary
    # (IPL, World Cup, Ranji Trophy, ...). 0/"Unknown competition" only when a
    # side could not be resolved to a level.
    level_id: int
    level_name: str


class TournamentSplit(CareerStatsBlock):
    tournament_id: int | None = None
    tournament_name: str


class TeamSplit(CareerStatsBlock):
    team_id: int | None = None
    team_name: str | None = None
    team_short_name: str | None = None


class RecentBattingLine(BaseModel):
    did_bat: bool
    runs: int = 0
    balls_faced: int = 0
    fours: int = 0
    sixes: int = 0
    strike_rate: float = 0.0
    out: bool = False
    dismissal: str | None = None


class RecentBowlingLine(BaseModel):
    bowled: bool
    balls_bowled: int = 0
    overs_str: str = "0.0"
    runs_conceded: int = 0
    wickets: int = 0
    economy: float = 0.0


class RecentFormEntry(BaseModel):
    match_id: int
    match_date: date
    match_type: str
    innings_number: int
    is_super_over: bool
    batting: RecentBattingLine
    bowling: RecentBowlingLine
    fielding: PlayerFieldingStats


class PlayerCareerStatsResponse(BaseModel):
    player_id: int
    player_code: str
    first_name: str
    last_name: str
    full_name: str
    # "completed" when only finished matches count (the default), "all" when live
    # matches are included too.
    scope: str
    career: CareerStatsBlock
    by_season: list[SeasonSplit] = []
    by_competition: list[CompetitionSplit] = []
    by_tournament: list[TournamentSplit] = []
    by_team: list[TeamSplit] = []
    recent_form: list[RecentFormEntry] = []
