from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import get_current_user
from app.database import get_db
from app.leaderboards.analytics import (
    build_batting_leaderboards,
    build_bowling_leaderboards,
    build_fielding_leaderboards,
)
from app.leaderboards.schemas import (
    BattingLeaderboardsResponse,
    BowlingLeaderboardsResponse,
    FieldingLeaderboardsResponse,
)

router = APIRouter(tags=["leaderboards"])


@router.get("/leaderboards/batting", response_model=BattingLeaderboardsResponse)
async def batting_leaderboards_endpoint(
    top: int = Query(
        10,
        ge=1,
        le=100,
        description="How many players per board.",
    ),
    min_balls_faced: int = Query(
        30,
        ge=0,
        description=(
            "Balls a player must have faced before the strike-rate board can rank "
            "them. 0 lifts the floor entirely."
        ),
    ),
    min_dismissals: int = Query(
        3,
        ge=0,
        description=(
            "Dismissals a player must have before the average board can rank them. "
            "0 keeps the almost-always-undefined case out by itself."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Account-wide batting leaderboards.

    Ranks every player who has batted across the account's completed matches on
    six boards: most runs, highest strike rate, most fours, most sixes, the
    highest individual score and the best batting average. Only completed matches
    count - a live match's figures would move under the reader - and Super Over
    innings are excluded. Strike rate and average only rank players who clear the
    ``min_balls_faced`` and ``min_dismissals`` floors respectively.
    """
    return await build_batting_leaderboards(
        db,
        current_user.id,
        top=top,
        min_balls_faced=min_balls_faced,
        min_dismissals=min_dismissals,
    )


@router.get("/leaderboards/bowling", response_model=BowlingLeaderboardsResponse)
async def bowling_leaderboards_endpoint(
    top: int = Query(
        10,
        ge=1,
        le=100,
        description="How many players per board.",
    ),
    min_overs: float = Query(
        3.0,
        ge=0,
        description=(
            "Overs a bowler must have bowled before the economy board can rank "
            "them. 0 lifts the floor entirely."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Account-wide bowling leaderboards.

    Ranks every bowler across the account's completed matches on five boards:
    most wickets, the best economy, the best figures in a single innings, the
    most dot balls and the most maidens. Only completed matches count - a live
    match's figures would move under the reader - and Super Over innings are
    excluded. Economy only ranks bowlers who bowl at least ``min_overs``.
    """
    return await build_bowling_leaderboards(
        db,
        current_user.id,
        top=top,
        min_overs=min_overs,
    )


@router.get("/leaderboards/fielding", response_model=FieldingLeaderboardsResponse)
async def fielding_leaderboards_endpoint(
    top: int = Query(
        10,
        ge=1,
        le=100,
        description="How many players per board.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Account-wide fielding leaderboards.

    Ranks every fielder across the account's completed matches on three boards:
    most catches, most run-outs and most stumpings. Only completed matches count
    and Super Over innings are excluded, matching the batting and bowling
    boards. A dismissal is counted for a fielder only when the delivery log
    records one - catches, run-outs and stumpings credited to nobody are left
    out rather than guessed.
    """
    return await build_fielding_leaderboards(
        db,
        current_user.id,
        top=top,
    )
