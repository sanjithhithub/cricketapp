"""The three fielding endpoints.

Reads are open to any signed-in user who can see the match, exactly like the
scorecard; the writes require an admin, exactly like every scoring write -
setting the field and sending a substitute on are match-official decisions,
not a spectator's.

None of them touch a ball, a run or a result: they read and write only
``fielding_setups`` / ``fielding_assignments`` / ``field_substitutions`` and
re-derive the fielding side from the match, so saving a setup or recording a
substitution can never change a score, a dismissal, a fixture or a standings
row.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST, NOT_FOUND
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.fielding.crud import (
    bring_substitute_onto_field,
    get_fielding_setup,
    save_fielding_setup,
)
from app.fielding.schemas import (
    FieldingSetupResponse,
    FieldingSetupUpsert,
    FieldingSubstitutionIn,
)
from app.scoring.crud import get_match
from app.scoring.enums import MatchStatus

router = APIRouter(tags=["fielding"])


async def _load_match(match_id: int, user: User, db: AsyncSession):
    match = await get_match(db, match_id, user.id)
    if not match:
        raise HTTPException(404, "Scoring match not found")
    return match


@router.get(
    "/matches/{match_id}/fielding-setup",
    response_model=FieldingSetupResponse,
    responses={"404": NOT_FOUND, "400": BAD_REQUEST},
)
async def get_match_fielding_setup(
    match_id: int,
    innings_number: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The field setup for one innings: the fielding side, its keeper, every
    fielder's position, the reserves who may come on, and the substitution
    history and rules.

    Defaults to the match's current innings. Pass ``innings_number`` to read
    another innings; the fielding side is derived the same way scoring
    derives it (the toss decides innings 1, each later innings swaps), so
    asking for innings 2 never returns innings 1's fielding side.

    The response also carries the fielding side's playing XI, its reserves
    and the position catalog with anchor coordinates, so a client can render
    the whole panel from this one call.
    """
    match = await _load_match(match_id, current_user, db)

    if innings_number is None:
        innings_number = match.current_innings_number or 1

    result = await get_fielding_setup(db, match, innings_number)
    if isinstance(result, str):
        raise HTTPException(400, result)
    return result


@router.put(
    "/matches/{match_id}/fielding-setup",
    response_model=FieldingSetupResponse,
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def put_match_fielding_setup(
    match_id: int,
    data: FieldingSetupUpsert,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Replace the field setup for one innings.

    The whole setup is sent, not a diff: the stored keeper and positions are
    made to match the request inside a single transaction, so changing the
    keeper or moving a fielder updates the existing rows instead of adding
    duplicates, and a player can never end up in two places.

    Every named player must be in the fielding side's playing XI *or* already
    on the field as a substituted-on reserve; a player on the batting side, a
    stranger, or a reserve who has not been substituted on is refused with a
    400 before anything is written. The ``wicket_keeper`` position may only
    be given to the named keeper, and at most 11 players may be named. Two
    fielders may share a position *name* when their coordinates differ.
    """
    match = await _load_match(match_id, current_user, db)
    if match.status == MatchStatus.COMPLETED.value:
        raise HTTPException(400, "Match already completed; the field setup is final")

    result = await save_fielding_setup(db, match, data)
    if isinstance(result, str):
        raise HTTPException(400, result)
    return result


@router.post(
    "/matches/{match_id}/fielding-setup/substitution",
    response_model=FieldingSetupResponse,
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def post_match_fielding_substitution(
    match_id: int,
    data: FieldingSubstitutionIn,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bring a substitute onto the field in place of an active player.

    Validates the whole substitution before writing anything: the outgoing
    player must be on the field, the incoming player must be a reserve of the
    *fielding* side who is not already active, the reason must be one this
    competition permits, and an ordinary substitute may not be handed a job
    (bowling or keeping) the Laws reserve for XI players unless the rules say
    otherwise. Then, in one transaction, the outgoing player's position is
    released, the incoming player takes it (or comes on unplaced), and the
    substitution is appended to the match history with the authorised user.

    Scores, wickets, results and standings are untouched: only the fielding
    tables are written.
    """
    match = await _load_match(match_id, current_user, db)
    if match.status == MatchStatus.COMPLETED.value:
        raise HTTPException(400, "Match already completed; the field setup is final")

    result = await bring_substitute_onto_field(
        db, match, data, authorized_by_user_id=current_user.id
    )
    if isinstance(result, str):
        raise HTTPException(400, result)
    return result
