"""Deep-analytics endpoints.

Two read-only routes: partnership analysis for a match, and a player's career
statistics split by season, competition, tournament and team. Each is a thin
wrapper over a builder in this package; the builders hold the scoring rules, the
routes hold the auth and the response shape. Both scope to the requesting account
through the same ownership checks the scoring and player routes use.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.career import get_player_career_stats
from app.analytics.partnerships import get_partnership_analysis
from app.analytics.schemas import (
    PartnershipAnalysisResponse,
    PlayerCareerStatsResponse,
)
from app.api_docs import NOT_FOUND
from app.auth.models import User
from app.auth.security import get_current_user
from app.database import get_db
from app.players.crud import get_player
from app.scoring.crud import get_match

router = APIRouter(tags=["analytics"])


@router.get(
    "/matches/{match_id}/partnerships",
    response_model=PartnershipAnalysisResponse,
    responses={"404": NOT_FOUND},
)
async def partnership_analysis_endpoint(
    match_id: int,
    innings_number: int | None = Query(
        None,
        ge=1,
        description="Limit the analysis to one innings number (1, 2, or a Super Over number).",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partnership analysis for a match.

    Every stand two batsmen put together between one wicket and the next: the
    players involved, the runs they added, the legal balls they faced and the run
    rate, plus each innings' opening partnership and its biggest stand. The
    innings total, wicket count and overs are included so the stands can be read
    against the scorecard. Pass ``innings_number`` to limit the payload to one
    innings; omit it to get every innings of the match, including any Super Over.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    payload = await get_partnership_analysis(db, match, innings_number)
    return PartnershipAnalysisResponse.model_validate(payload)


@router.get(
    "/players/{player_id}/career-stats",
    response_model=PlayerCareerStatsResponse,
    responses={"404": NOT_FOUND},
)
async def player_career_stats_endpoint(
    player_id: int,
    completed_only: bool = Query(
        True,
        description="Count only finished matches (default). Set false to include live matches.",
    ),
    recent: int = Query(
        10,
        ge=1,
        le=100,
        description="How many of the player's most recent innings to return in recent_form.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Career statistics for one player.

    The career total for batting, bowling and fielding, split by season (the
    calendar year of the match), by competition (the level the player's side
    plays at), by tournament (when the match was part of one) and by team, plus a
    per-innings recent-form log, newest first. Only matches this account owns are
    counted, and by default only finished ones; set ``completed_only=false`` to
    include live matches. Fielding is credited only where the delivery log names
    a fielder.
    """
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    payload = await get_player_career_stats(
        db, player, current_user.id, completed_only=completed_only, recent=recent
    )
    return PlayerCareerStatsResponse.model_validate(payload)


__all__ = ["router"]
