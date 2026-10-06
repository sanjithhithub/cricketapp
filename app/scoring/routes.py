from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST, CONFLICT, NOT_FOUND
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.scoring.crud import (
    ScoringRepository,
    add_batsman,
    advance_match_after_innings,
    ensure_current_innings,
    get_innings_by_number,
    get_match,
    start_scoring,
    sync_match_result,
)
from app.scoring.engine import (
    DeliveryInput,
    ExtraType,
    InningsEndedError,
    ScoreEngine,
    ScoreEngineError,
    WicketType,
)
from app.scoring.enums import MatchStatus
from app.scoring.schemas import (
    AddBatsmanRequest,
    DeliveryCreate,
    ScorecardResponse,
    StartInningsRequest,
)

router = APIRouter(tags=["scoring"])


# Both codes show up on the scoring routes and neither is interchangeable with the
# other: 400 is "this ball is not legal", 409 is "this innings is over, or the
# match is, and the scorecard is final". A client that retries a 400 is right; a
# client that retries a 409 will never succeed and must refresh instead.
SCORING_RESPONSES = {"400": BAD_REQUEST, "409": CONFLICT}


def _map_engine_error(exc: ScoreEngineError) -> HTTPException:
    if isinstance(exc, InningsEndedError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def _to_delivery_input(data: DeliveryCreate) -> DeliveryInput:
    return DeliveryInput(
        striker_id=data.striker_id,
        non_striker_id=data.non_striker_id,
        bowler_id=data.bowler_id,
        runs_batsman=data.runs_batsman,
        runs_extras=data.runs_extras,
        extra_type=ExtraType(data.extra_type),
        wicket_type=WicketType(data.wicket_type) if data.wicket_type else None,
        dismissed_player_id=data.dismissed_player_id,
    )


@router.post(
    "/matches/{match_id}/deliveries",
    response_model=ScorecardResponse,
    status_code=201,
    responses=SCORING_RESPONSES,
)
async def submit_delivery(
    match_id: int,
    data: DeliveryCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.status == MatchStatus.COMPLETED.value:
        raise HTTPException(409, "Match already completed")

    innings, error = await ensure_current_innings(db, match)
    if error:
        raise HTTPException(400, error)

    engine = ScoreEngine(ScoringRepository(db))
    try:
        await engine.apply_delivery(innings.id, _to_delivery_input(data))
    except ScoreEngineError as exc:
        raise _map_engine_error(exc)

    await db.commit()

    engine = ScoreEngine(ScoringRepository(db))
    try:
        scorecard = await engine.get_scorecard(innings.id)
    except ScoreEngineError as exc:
        raise _map_engine_error(exc)
    if scorecard.completed:
        # Advance to the next innings (second innings, or a Super Over in case
        # of a tie) or finish the match. A wicket awaiting a new batsman is not
        # completed, so the innings is held open until one is picked.
        await advance_match_after_innings(db, match, innings, scorecard)

    response = ScorecardResponse.model_validate(scorecard)
    if match.status == MatchStatus.COMPLETED.value:
        await db.refresh(match)
        response.match_result = match.result
    return response


@router.post(
    "/matches/{match_id}/batting-order",
    response_model=ScorecardResponse,
    responses=SCORING_RESPONSES,
)
async def add_next_batsman(
    match_id: int,
    data: AddBatsmanRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.status == MatchStatus.COMPLETED.value:
        raise HTTPException(409, "Match already completed")
    if match.current_innings_number == 0:
        raise HTTPException(400, "No innings started for this match")

    innings = await get_innings_by_number(db, match.id, match.current_innings_number)
    if not innings:
        raise HTTPException(404, "No innings available for this match")

    innings, error = await add_batsman(db, innings, data.player_id)
    if error:
        raise HTTPException(400, error)

    engine = ScoreEngine(ScoringRepository(db))
    try:
        scorecard = await engine.get_scorecard(innings.id)
    except ScoreEngineError as exc:
        raise _map_engine_error(exc)
    return ScorecardResponse.model_validate(scorecard)


@router.post(
    "/matches/{match_id}/start-innings",
    response_model=ScorecardResponse,
    responses=SCORING_RESPONSES,
)
async def start_innings_endpoint(
    match_id: int,
    data: StartInningsRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.status == MatchStatus.COMPLETED.value:
        raise HTTPException(409, "Match already completed")

    innings, error = await start_scoring(db, match, data.batting_order)
    if error:
        raise HTTPException(400, error)

    engine = ScoreEngine(ScoringRepository(db))
    try:
        scorecard = await engine.get_scorecard(innings.id)
    except ScoreEngineError as exc:
        raise _map_engine_error(exc)
    return ScorecardResponse.model_validate(scorecard)


@router.get(
    "/matches/{match_id}/scorecard",
    response_model=ScorecardResponse,
    responses={"404": NOT_FOUND},
)
async def get_match_scorecard(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.current_innings_number == 0:
        raise HTTPException(404, "No innings started for this match")
    innings = await get_innings_by_number(db, match.id, match.current_innings_number)
    if not innings:
        raise HTTPException(404, "No innings available for this match")

    engine = ScoreEngine(ScoringRepository(db))
    try:
        scorecard = await engine.get_scorecard(innings.id)
    except ScoreEngineError as exc:
        raise _map_engine_error(exc)
    response = ScorecardResponse.model_validate(scorecard)
    if match.status == MatchStatus.COMPLETED.value:
        # Recompute-and-persist so stale/manual results (e.g. a leftover
        # 'tie' from the create-match form) are replaced by the official
        # result derived from the innings scorecards.
        await sync_match_result(db, match)
        await db.refresh(match)
        response.match_result = match.result
    return response
