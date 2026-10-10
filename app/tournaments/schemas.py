"""Request and response bodies for the tournament endpoints.

Every bound here mirrors a column in :mod:`app.tournaments.models`, so a value
that validates is a value the database can hold. The response models are shaped
for the screen that reads them: a fixture carries both team names (so a schedule
renders without a second request), and a points-table row carries its own
position rather than leaving the client to number the list.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.tournaments.enums import FixtureStage, TournamentFormat, TournamentStatus


class TournamentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: str | None = Field(None, max_length=500)
    level_id: int | None = None
    format: TournamentFormat = TournamentFormat.LEAGUE
    status: TournamentStatus = TournamentStatus.UPCOMING
    start_date: date | None = None
    end_date: date | None = None
    points_win: int = Field(2, ge=0)
    points_tie: int = Field(1, ge=0)
    points_loss: int = Field(0, ge=0)
    points_no_result: int = Field(1, ge=0)


class TournamentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=150)
    description: str | None = Field(None, max_length=500)
    level_id: int | None = None
    format: TournamentFormat | None = None
    status: TournamentStatus | None = None
    start_date: date | None = None
    end_date: date | None = None
    points_win: int | None = Field(None, ge=0)
    points_tie: int | None = Field(None, ge=0)
    points_loss: int | None = Field(None, ge=0)
    points_no_result: int | None = Field(None, ge=0)


class TournamentResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    level_id: int | None = None
    format: str
    status: str
    start_date: date | None = None
    end_date: date | None = None
    points_win: int
    points_tie: int
    points_loss: int
    points_no_result: int
    team_count: int = 0
    fixture_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class TournamentTeamAdd(BaseModel):
    team_id: int


class TournamentTeamResponse(BaseModel):
    id: int
    team_id: int
    team_name: str
    team_short_name: str
    logo: str | None = None


class TournamentTeamList(BaseModel):
    tournament_id: int
    teams: list[TournamentTeamResponse]


class FixtureCreate(BaseModel):
    team_a_id: int
    team_b_id: int
    scheduled_date: date | None = None
    scheduled_time: str | None = Field(None, max_length=10)
    venue: str | None = Field(None, max_length=200)
    stage: FixtureStage = FixtureStage.LEAGUE
    round_number: int | None = Field(None, ge=1)


class FixtureUpdate(BaseModel):
    team_a_id: int | None = None
    team_b_id: int | None = None
    scheduled_date: date | None = None
    scheduled_time: str | None = Field(None, max_length=10)
    venue: str | None = Field(None, max_length=200)
    stage: FixtureStage | None = None
    round_number: int | None = Field(None, ge=1)


class FixtureResponse(BaseModel):
    id: int
    tournament_id: int
    team_a_id: int
    team_a_name: str | None = None
    team_a_short_name: str | None = None
    team_b_id: int
    team_b_name: str | None = None
    team_b_short_name: str | None = None
    match_id: int | None = None
    scheduled_date: date | None = None
    scheduled_time: str | None = None
    venue: str | None = None
    stage: str
    round_number: int | None = None
    status: str = "scheduled"
    result: str | None = None
    winner_team_id: int | None = None


class FixtureGenerate(BaseModel):
    legs: int = Field(1, ge=1, le=2, description="Round-robin legs: 1 = single, 2 = home and away")
    replace: bool = Field(
        False,
        description="Delete the existing schedule first. Refused if a fixture already has a match.",
    )


class FixtureResult(BaseModel):
    match_id: int


class KnockoutGenerate(BaseModel):
    teams: int = Field(4, description="How many of the top seeds enter the draw: 2, 4 or 8")


class PointsTableRow(BaseModel):
    position: int
    team_id: int
    team_name: str
    team_short_name: str
    logo: str | None = None
    played: int
    won: int
    lost: int
    tied: int
    no_result: int
    points: int
    runs_for: int
    overs_for: float
    runs_against: int
    overs_against: float
    net_run_rate: float
    form: str


class PointsTableResponse(BaseModel):
    tournament_id: int
    tournament_name: str
    format: str
    status: str
    table: list[PointsTableRow]


class TournamentBatterStat(BaseModel):
    player_id: int
    full_name: str
    team_id: int | None = None
    team_name: str | None = None
    matches: int
    innings: int
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    highest_score: int
    strike_rate: float


class TournamentBowlerStat(BaseModel):
    player_id: int
    full_name: str
    team_id: int | None = None
    team_name: str | None = None
    matches: int
    innings: int
    wickets: int
    balls_bowled: int
    overs: float
    runs_conceded: int
    best_wickets: int | None = None
    best_runs_conceded: int | None = None
    economy: float


class TournamentLeaderboardResponse(BaseModel):
    tournament_id: int
    tournament_name: str
    top_run_scorers: list[TournamentBatterStat]
    top_wicket_takers: list[TournamentBowlerStat]
