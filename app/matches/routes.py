from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST, CONFLICT, paginated_list_response, set_pagination_headers
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.matches.crud import (
    create_match,
    delete_match,
    get_match,
    get_matches,
    replace_match,
    update_match,
)
from app.matches.models import Match
from app.matches.schemas import (
    MatchCreate,
    MatchResponse,
    MatchTournamentInfo,
    MatchUpdate,
)
from app.referees.crud import (
    RefereeAssignmentError,
    assign_referee_to_match,
    assignment_info,
    build_match_officials,
    unassign_referee_from_match,
)
from app.referees.schemas import (
    MatchOfficialAssignRequest,
    MatchOfficialAssignResponse,
    MatchOfficials,
)
from app.tournaments.models import Tournament, TournamentFixture

router = APIRouter(tags=["matches"])


def _reveal_contact(current_user: User, matches: list[Match]) -> bool:
    """Whether the caller may see an official's number in full.

    A match is only ever returned to the account that owns it, and an official
    standing in that match is there because this account registered them, so
    the number is shown in full. The masking lives on the referee register,
    where a list of officials is read by people who have no reason to call
    each of them.
    """
    if current_user.role == "admin":
        return True
    return all(match.user_id == current_user.id for match in matches)


async def _tournaments_for(
    db: AsyncSession, match_ids: list[int]
) -> dict[int, MatchTournamentInfo]:
    """Map match id -> the tournament that fixture links it to.

    A match belongs to a tournament only when a ``TournamentFixture`` row points
    at it (``match_id`` is unique across fixtures, so at most one tournament).
    Standalone matches — created straight from the Create Match form — have no
    fixture and therefore get no entry, so callers render them with no
    tournament label. The dict is empty-lookup-safe for a page of matches.
    """
    if not match_ids:
        return {}
    rows = await db.execute(
        select(TournamentFixture.match_id, Tournament.id, Tournament.name)
        .join(Tournament, Tournament.id == TournamentFixture.tournament_id)
        .where(TournamentFixture.match_id.in_(match_ids))
    )
    return {
        match_id: MatchTournamentInfo(id=tournament_id, name=name)
        for match_id, tournament_id, name in rows.all()
    }


async def _to_responses(
    db: AsyncSession, matches: list[Match], current_user: User
) -> list[MatchResponse]:
    """Build the responses for a page of matches, officials panel included."""
    if not matches:
        return []
    bases = [MatchResponse.model_validate(match, from_attributes=True) for match in matches]
    panels = await build_match_officials(
        db, matches, reveal_contact=_reveal_contact(current_user, matches)
    )
    tournaments = await _tournaments_for(db, [match.id for match in matches])
    return [
        base.model_copy(update={"officials": panel, "tournament": tournaments.get(match.id)})
        for base, panel, match in zip(bases, panels, matches, strict=True)
    ]


@router.get(
    "/matches",
    response_model=list[MatchResponse],
    responses=paginated_list_response("One page of this account's matches."),
)
async def list_matches(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    matches, total = await get_matches(db, user_id=current_user.id, skip=skip, limit=limit)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    return await _to_responses(db, matches, current_user)


@router.post(
    "/matches",
    response_model=MatchResponse,
    status_code=201,
    responses={"400": BAD_REQUEST},
)
async def create_match_endpoint(
    data: MatchCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match, error = await create_match(db, data, current_user.id)
    if error:
        # A named team does not exist, is not this account's, the toss winner is
        # neither side, or a named official is not on file. Well formed, but not
        # something the server will carry out.
        raise HTTPException(400, error)
    responses = await _to_responses(db, [match], current_user)
    return responses[0]


@router.get("/matches/{match_id}", response_model=MatchResponse)
async def get_match_endpoint(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Match not found")
    responses = await _to_responses(db, [match], current_user)
    return responses[0]


@router.put(
    "/matches/{match_id}",
    response_model=MatchResponse,
    deprecated=True,
    summary="Replace a match (deprecated)",
    description=(
        "Deprecated: use `PATCH /matches/{match_id}`. This takes the full "
        "`MatchCreate` body, so changing one field means resending all of them."
    ),
    responses={"400": BAD_REQUEST},
)
async def replace_match_endpoint(
    match_id: int,
    data: MatchCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full replacement of every mutable field on a match. Prefer PATCH."""
    match, error = await replace_match(db, match_id, data, current_user.id)
    if error:
        raise HTTPException(400, error)
    responses = await _to_responses(db, [match], current_user)
    return responses[0]


@router.patch("/matches/{match_id}", response_model=MatchResponse, responses={"400": BAD_REQUEST})
async def update_match_endpoint(
    match_id: int,
    data: MatchUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    match, error = await update_match(db, match_id, data, current_user.id)
    if error:
        if "not found" in error.lower():
            raise HTTPException(404, error)
        raise HTTPException(400, error)
    responses = await _to_responses(db, [match], current_user)
    return responses[0]


@router.delete("/matches/{match_id}", status_code=204)
async def delete_match_endpoint(
    match_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        deleted = await delete_match(db, match_id, current_user.id)
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(500, f"Failed to delete match: {str(e)}")
    if not deleted:
        raise HTTPException(404, "Match not found")
    return Response(status_code=204)


@router.get("/matches/{match_id}/officials", response_model=MatchOfficials)
async def list_match_officials(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The officials panel of one match, with every appointment behind it.

    The three seats are always present, whether or not they are filled, and
    ``assignments`` carries the whole appointment list - including the roles
    the panel has no seat for, such as the TV umpire or the reserve.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Match not found")
    panels = await build_match_officials(
        db, [match], reveal_contact=_reveal_contact(current_user, [match])
    )
    return panels[0]


@router.post(
    "/matches/{match_id}/officials",
    response_model=MatchOfficialAssignResponse,
    status_code=201,
    responses={"400": BAD_REQUEST, "409": CONFLICT},
)
async def assign_match_official(
    match_id: int,
    data: MatchOfficialAssignRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Appoint an official to this match in a role.

    The rules the appointment is checked against: the official must be on this
    account's register, the role must be one their own type can fill, an
    official who is not active may not be appointed, and a match has one
    official per role and one role per official.

    ``warnings`` carries what was not refused but is worth saying out loud -
    an official who is already booked on the same day, for instance.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Match not found")

    try:
        assignment, warnings = await assign_referee_to_match(
            db, match, data.referee_id, data.role, current_user.id
        )
    except RefereeAssignmentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    panels = await build_match_officials(db, [match], reveal_contact=True)
    return MatchOfficialAssignResponse(
        assignment=assignment_info(assignment),
        officials=panels[0],
        warnings=warnings,
    )


@router.delete("/matches/{match_id}/officials/{assignment_id}", status_code=204)
async def unassign_match_official(
    match_id: int,
    assignment_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Withdraw an appointment from a match.

    The appointment and the seat it held both go; the match and anything
    already played on it stay exactly as they were.
    """
    match = await get_match(db, match_id, current_user.id)
    if not match:
        raise HTTPException(404, "Match not found")
    if not await unassign_referee_from_match(db, match, assignment_id, current_user.id):
        raise HTTPException(404, "Assignment not found")
