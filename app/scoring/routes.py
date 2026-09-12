from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.scoring.crud import (
    ScoringRepository,
    ensure_current_innings,
    get_innings_by_number,
    get_match,
    open_second_innings,
    set_match_completed,
    start_scoring,
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
from app.scoring.schemas import DeliveryCreate, ScorecardResponse, StartInningsRequest

router = APIRouter(tags=["scoring"])


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


@router.post("/matches/{match_id}/deliveries", response_model=ScorecardResponse, status_code=201)
async def submit_delivery(
    match_id: int,
    data: DeliveryCreate,
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id)
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
        if innings.innings_number == 1:
            await open_second_innings(db, match, scorecard.total)
        else:
            await set_match_completed(db, match)

    return ScorecardResponse.model_validate(scorecard)


@router.post("/matches/{match_id}/start-innings", response_model=ScorecardResponse)
async def start_innings_endpoint(
    match_id: int,
    data: StartInningsRequest,
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id)
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


@router.get("/matches/{match_id}/scorecard", response_model=ScorecardResponse)
async def get_match_scorecard(
    match_id: int,
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id)
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
    return ScorecardResponse.model_validate(scorecard)
