"""Chart-ready visualization endpoints.

Each route is a thin wrapper over a builder in :mod:`app.visualization.analytics`;
the builders hold the scoring rules, these hold the auth and the response shape.
Match charts scope to the requesting account through the same ownership check
the scoring routes use; team charts scope the same way through the teams module.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import NOT_FOUND
from app.auth.models import User
from app.auth.security import get_current_user
from app.database import get_db
from app.scoring.crud import get_match as get_scoring_match
from app.teams.crud import get_team
from app.visualization.analytics import (
    get_bowler_economies,
    get_innings_progression,
    get_partnerships,
    get_player_runs,
    get_player_strike_rates,
    get_recent_form,
    get_run_distribution,
    get_runs_by_over,
    get_wickets_by_bowler,
    get_win_loss_trend,
)
from app.visualization.schemas import (
    BowlerStatsResponse,
    InningsProgressionResponse,
    PartnershipsResponse,
    PlayerRunsResponse,
    RecentFormResponse,
    RunDistributionResponse,
    RunsByOverResponse,
    WinLossTrendResponse,
)

router = APIRouter(tags=["visualization"])

_RESPONSES = {"404": NOT_FOUND}


async def _owned_match(match_id: int, user: User, db: AsyncSession):
    match = await get_scoring_match(db, match_id, user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    return match


async def _owned_team(team_id: int, user: User, db: AsyncSession):
    team = await get_team(db, team_id, user.id)
    if not team:
        raise HTTPException(404, "Team not found")
    return team


def _innings_param() -> int | None:
    return Query(
        None,
        ge=1,
        description="Limit the chart to one innings number (1, 2, or a Super Over number).",
    )


@router.get(
    "/matches/{match_id}/visualizations/runs-by-over",
    response_model=RunsByOverResponse,
    responses=_RESPONSES,
)
async def runs_by_over_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Runs scored in each over of every innings of a match."""
    return await get_runs_by_over(db, await _owned_match(match_id, current_user, db), innings_number)


@router.get(
    "/matches/{match_id}/visualizations/innings-progression",
    response_model=InningsProgressionResponse,
    responses=_RESPONSES,
)
async def innings_progression_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cumulative runs and run-rate progression through each innings."""
    return await get_innings_progression(
        db, await _owned_match(match_id, current_user, db), innings_number
    )


@router.get(
    "/matches/{match_id}/visualizations/player-runs",
    response_model=PlayerRunsResponse,
    responses=_RESPONSES,
)
async def player_runs_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every player's batting figures for a match, ranked by runs."""
    return await get_player_runs(db, await _owned_match(match_id, current_user, db), innings_number)


@router.get(
    "/matches/{match_id}/visualizations/player-strike-rate",
    response_model=PlayerRunsResponse,
    responses=_RESPONSES,
)
async def player_strike_rate_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    min_balls_faced: int = Query(
        1,
        ge=0,
        description="Balls a player must have faced before their strike rate is ranked.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Player strike rates for a match, ranked best first."""
    return await get_player_strike_rates(
        db,
        await _owned_match(match_id, current_user, db),
        innings_number,
        min_balls_faced=min_balls_faced,
    )


@router.get(
    "/matches/{match_id}/visualizations/bowler-economy",
    response_model=BowlerStatsResponse,
    responses=_RESPONSES,
)
async def bowler_economy_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    min_overs: float = Query(
        0.0,
        ge=0,
        description="Overs a bowler must have bowled before their economy is ranked.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bowling figures for a match, ranked by economy (best first)."""
    return await get_bowler_economies(
        db,
        await _owned_match(match_id, current_user, db),
        innings_number,
        min_overs=min_overs,
    )


@router.get(
    "/matches/{match_id}/visualizations/wickets-by-bowler",
    response_model=BowlerStatsResponse,
    responses=_RESPONSES,
)
async def wickets_by_bowler_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bowling figures for a match, ranked by wickets taken."""
    return await get_wickets_by_bowler(
        db, await _owned_match(match_id, current_user, db), innings_number
    )


@router.get(
    "/matches/{match_id}/visualizations/run-distribution",
    response_model=RunDistributionResponse,
    responses=_RESPONSES,
)
async def run_distribution_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """How the runs were made: the per-ball run histogram of every innings."""
    return await get_run_distribution(
        db, await _owned_match(match_id, current_user, db), innings_number
    )


@router.get(
    "/matches/{match_id}/visualizations/partnerships",
    response_model=PartnershipsResponse,
    responses=_RESPONSES,
)
async def partnerships_endpoint(
    match_id: int,
    innings_number: int | None = _innings_param(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partnership contributions: runs added by each pair before a wicket."""
    return await get_partnerships(
        db, await _owned_match(match_id, current_user, db), innings_number
    )


@router.get(
    "/teams/{team_id}/visualizations/win-loss-trend",
    response_model=WinLossTrendResponse,
    responses=_RESPONSES,
)
async def win_loss_trend_endpoint(
    team_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A team's cumulative win/loss/draw record through its completed matches."""
    return await get_win_loss_trend(db, await _owned_team(team_id, current_user, db), current_user.id)


@router.get(
    "/teams/{team_id}/visualizations/recent-form",
    response_model=RecentFormResponse,
    responses=_RESPONSES,
)
async def recent_form_endpoint(
    team_id: int,
    limit: int = Query(5, ge=1, le=50, description="How many recent results to return."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A team's most recent completed results, newest first."""
    return await get_recent_form(
        db, await _owned_team(team_id, current_user, db), current_user.id, recent=limit
    )
