import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST, CONFLICT, NOT_FOUND
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.scoring.analytics import get_match_analytics
from app.scoring.crud import (
    ScoringRepository,
    add_batsman,
    advance_match_after_innings,
    ensure_current_innings,
    find_delivery_by_client_request,
    get_innings_by_number,
    get_match,
    start_scoring,
    sync_match_result,
    undo_last_delivery,
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
    MatchAnalyticsResponse,
    MatchSummaryResponse,
    ScorecardResponse,
    StartInningsRequest,
)
from app.scoring.summary import get_match_summary

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


# One scorer at a time per match. Two identical retries arriving together would
# otherwise both read the log, both find nothing, and both score the ball; the
# lock serialises them so the second one sees the first one's row. The lock is
# recorded with the loop that created it and replaced when that changes, because
# a lock may only ever be awaited on the loop that made it (a test suite runs
# several loops against the same module state; a server runs one).
_submission_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _match_lock(match_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _submission_locks.get(match_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _submission_locks[match_id] = entry
    return entry[1]


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
        fielder_id=data.fielder_id,
        runs_wides=data.runs_wides,
        runs_no_balls=data.runs_no_balls,
        runs_byes=data.runs_byes,
        runs_leg_byes=data.runs_leg_byes,
        runs_penalty=data.runs_penalty,
        boundary_runs=data.boundary_runs,
        batters_crossed=data.batters_crossed,
        leg_byes_allowed=data.leg_byes_allowed,
        obstruction_prevented_catch=data.obstruction_prevented_catch,
        client_request_id=data.client_request_id,
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

    async with _match_lock(match_id):
        innings, error = await ensure_current_innings(db, match)
        if error:
            raise HTTPException(400, error)

        engine = ScoreEngine(ScoringRepository(db))

        # A retry of a ball that already went in must not score it twice. The
        # client's id is the only thing that identifies the attempt, so this
        # check (and the unique constraint behind it) is the whole of the
        # duplicate guard - the lock above only makes the check reliable when
        # both attempts arrive at the same instant.
        if data.client_request_id:
            already = await find_delivery_by_client_request(db, innings.id, data.client_request_id)
            if already is not None:
                return await _delivery_response(db, match, innings, engine)

        try:
            await engine.apply_delivery(innings.id, _to_delivery_input(data))
            await db.commit()
        except ScoreEngineError as exc:
            # Nothing was written; drop the half-built insert so the session
            # cannot leak it into the scorecard read below.
            await db.rollback()
            raise _map_engine_error(exc)
        except IntegrityError:
            # Another process won the race. The unique index on
            # (innings_id, client_request_id) is the real guard, so treat it
            # exactly like the pre-check above.
            await db.rollback()
            if data.client_request_id:
                already = await find_delivery_by_client_request(
                    db, innings.id, data.client_request_id
                )
                if already is not None:
                    return await _delivery_response(db, match, innings, engine)
            raise HTTPException(409, "This delivery has already been recorded")

        return await _delivery_response(db, match, innings, engine)


async def _delivery_response(
    db: AsyncSession, match, innings, engine: ScoreEngine
) -> ScorecardResponse:
    """The updated innings state, plus the match advance a finished innings needs.

    This is the only shape the scoring console ever renders from: whatever the
    backend says the crease, totals and figures are now is what the next ball
    will be submitted against.
    """
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
    "/matches/{match_id}/deliveries/undo",
    response_model=ScorecardResponse,
    responses={**SCORING_RESPONSES, "404": NOT_FOUND},
)
async def undo_last_delivery_endpoint(
    match_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Take back the most recent ball of the match.

    The real undo: the delivery row is removed from the append-only log and the
    server replays everything else - team score, batter and bowler figures,
    balls and overs, wickets, strike, the current over and the ball history -
    so the returned scorecard is the state before that ball. The reopened
    innings (and a successor innings the ball had just opened) is returned so
    the console can carry on scoring it.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.status == MatchStatus.COMPLETED.value:
        # Reopening a completed match would also have to unwind the recorded
        # result and tournament fixture progression.
        raise HTTPException(409, "Match already completed - cannot undo a delivery")

    # Same lock as submit: an undo that read the log while a delivery was being
    # written would replay everything except a row that is about to exist (or
    # drop a row the writer thinks it owns).
    async with _match_lock(match_id):
        innings, error = await undo_last_delivery(db, match)
        if error:
            raise HTTPException(409, error)

        engine = ScoreEngine(ScoringRepository(db))
        try:
            scorecard = await engine.get_scorecard(innings.id)
        except ScoreEngineError as exc:
            raise _map_engine_error(exc)
        return ScorecardResponse.model_validate(scorecard)


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


@router.get(
    "/matches/{match_id}/summary",
    response_model=MatchSummaryResponse,
    responses={"404": NOT_FOUND},
)
async def get_match_summary_endpoint(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The finished-match experience: result, winner, each innings' score and
    overs, toss, venue, player of the match, top run scorer, best bowler and
    one-line highlights.

    Works for any match this account can see. Before the second innings is
    scored the winner and margin are absent (there is nothing to derive them
    from yet); for a completed match the stored result is healed first exactly
    like the scorecard endpoint does.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    if match.status == MatchStatus.COMPLETED.value:
        await sync_match_result(db, match)
        await db.refresh(match)
    payload = await get_match_summary(db, match)
    return MatchSummaryResponse.model_validate(payload)


@router.get(
    "/matches/{match_id}/analytics",
    response_model=MatchAnalyticsResponse,
    responses={"404": NOT_FOUND},
)
async def get_match_analytics_endpoint(
    match_id: int,
    innings_number: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ball-by-ball analytics for a match.

    Every delivery in chronological order (runs, extras, dismissal, running
    score and run rate), an over-by-over breakdown, the scoring pattern (dots,
    singles, twos, threes, fours, sixes, boundaries), an extras breakdown
    (wides, no-balls, byes, leg byes) and the progression of the run rate.

    Pass ``innings_number`` to limit the payload to one innings; omit it to get
    every innings of the match, including any Super Over (numbered 3, 4, ...).
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    payload = await get_match_analytics(db, match, innings_number)
    return MatchAnalyticsResponse.model_validate(payload)
