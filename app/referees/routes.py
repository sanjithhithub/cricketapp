"""The match officials register.

These are the people an account has on file to officiate its matches. Each one
carries the details the officials panel of a scorecard shows: name, date of
birth, nationality, photo, the kind of official they are, how long they have
been doing it, and how to reach them.

Reading a list masks the phone number, exactly as the player list does: a
picker needs the photo, the name and the type to tell two officials apart, and
not the whole number. Opening one official - or reading the officials panel of
a match, which the account that owns the match is doing for itself - shows it
in full.

The register is account-scoped: an official belongs to the account that entered
them, and another account's official is reported as missing rather than as
forbidden, so the response cannot be used to probe which ids exist.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api_docs import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    paginated_list_response,
    set_pagination_headers,
)
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.players.identity import full_name, mask_mobile
from app.referees.crud import (
    DuplicateRefereeEmailError,
    clear_referee_profile_image,
    create_referee,
    delete_referee,
    get_referee,
    get_referee_by_code,
    get_referee_delete_check,
    get_referee_matches,
    get_referee_options,
    get_referee_stats,
    get_referees,
    referee_option,
    replace_referee,
    update_referee,
    update_referee_profile_image,
)
from app.referees.schemas import (
    RefereeCreate,
    RefereeCreateResponse,
    RefereeDeleteCheck,
    RefereeMatchInfo,
    RefereeOption,
    RefereeProfileImageResponse,
    RefereeResponse,
    RefereeStats,
    RefereeUpdate,
)

router = APIRouter(tags=["referees"])

# The largest photo the API will read into memory before it checks the type.
# The limit is one byte over the storage limit so an oversized file is
# recognised as oversized rather than silently truncated.
_MAX_UPLOAD_BYTES = storage.MAX_IMAGE_BYTES + 1


def _actor_name(user) -> str | None:
    """The name of a user, for the audit columns."""
    if user is None:
        return None
    return user.full_name or user.email


def _certification_expired(referee) -> bool:
    """Whether the certification has a date and that date has passed."""
    expiry = referee.certification_expiry_date
    return bool(expiry and expiry < date.today())


def _to_response(referee, *, reveal_phone: bool, reveal_notes: bool) -> RefereeResponse:
    """Build a response for one official.

    ``reveal_phone`` and ``reveal_notes`` are the authorisation decisions: a
    list masks the number and hides the internal notes, while an admin or the
    account that owns the record sees both in full.
    """
    return RefereeResponse(
        id=referee.id,
        referee_code=referee.referee_code,
        first_name=referee.first_name,
        last_name=referee.last_name,
        full_name=full_name(referee.first_name, referee.last_name),
        date_of_birth=referee.date_of_birth,
        country_id=referee.country_id,
        # The name of the country, so a client does not have to join the
        # reference data itself to label the nationality.
        nationality=referee.country.name if referee.country else None,
        profile_image=referee.profile_image,
        referee_type=referee.referee_type,
        experience_years=referee.experience_years or 0,
        country_code=referee.country_code,
        mobile_number=(
            referee.mobile_number
            if reveal_phone
            else mask_mobile(referee.country_code, referee.mobile_number)
        ),
        phone_display=mask_mobile(referee.country_code, referee.mobile_number),
        email=referee.email,
        experience_level=referee.experience_level,
        certification_level=referee.certification_level,
        certifying_authority=referee.certifying_authority,
        certification_number=referee.certification_number,
        certification_expiry_date=referee.certification_expiry_date,
        certification_expired=_certification_expired(referee),
        specializations=list(referee.specializations or []),
        languages=list(referee.languages or []),
        address=referee.address,
        state_id=referee.state_id,
        city_id=referee.city_id,
        postal_code=referee.postal_code,
        status=referee.status,
        availability_status=referee.availability_status,
        joining_date=referee.joining_date,
        notes=referee.notes if reveal_notes else None,
        created_at=referee.created_at,
        updated_at=referee.updated_at,
        created_by=_actor_name(referee.created_by),
        updated_by=_actor_name(referee.updated_by),
    )


@router.get(
    "/referees",
    response_model=list[RefereeResponse],
    responses=paginated_list_response("One page of this account's match officials.", paged=True),
)
async def list_referees(
    page: int = Query(1, ge=1, description="Page number, starting at 1."),
    page_size: int = Query(100, ge=1, le=500, description="Rows per page."),
    skip: int | None = Query(
        None, ge=0, description="Offset of the first row. Deprecated: use `page`."
    ),
    limit: int | None = Query(
        None, ge=1, le=500, description="Rows per page. Deprecated: use `page_size`."
    ),
    q: str | None = Query(None, description="Search by name, email or referee code"),
    referee_type: str | None = Query(None, description="Filter by the kind of official"),
    nationality: str | None = Query(
        None, description="Filter by nationality: a country id or a country name."
    ),
    status: str | None = Query(
        None, description="Filter by status: active, inactive or suspended."
    ),
    experience_min: int | None = Query(
        None, ge=0, description="Only officials with at least this many years."
    ),
    experience_max: int | None = Query(
        None, ge=0, description="Only officials with at most this many years."
    ),
    certification_level: str | None = Query(None, description="Filter by certification level."),
    sort_by: str = Query("id", description="Column to sort on."),
    sort_order: str = Query("asc", description="asc or desc."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    """This account's match officials, one page at a time.

    The phone number is masked here for everyone who is not an admin, and the
    internal notes are withheld from everyone who is not an administrator;
    open one official to read them in full.
    """
    # The offset parameters are the older way of asking for a page. They are
    # still honoured, so an existing client keeps working, but a page number
    # is what a client with a pager needs.
    if skip is not None or limit is not None:
        offset = skip if skip is not None else (page - 1) * page_size
        size = limit if limit is not None else page_size
        current_page = (offset // size) + 1
    else:
        offset = (page - 1) * page_size
        size = page_size
        current_page = page

    referees, total = await get_referees(
        db,
        user_id=current_user.id,
        skip=offset,
        limit=size,
        q=q,
        referee_type=referee_type,
        nationality=nationality,
        status=status,
        experience_min=experience_min,
        experience_max=experience_max,
        certification_level=certification_level,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    set_pagination_headers(
        response,
        total=total,
        skip=offset,
        limit=size,
        page=current_page,
        page_size=size,
    )
    reveal = current_user.role == "admin"
    return [_to_response(referee, reveal_phone=reveal, reveal_notes=reveal) for referee in referees]


@router.get("/referees/stats", response_model=RefereeStats)
async def referee_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The counts the register's header shows.

    One request, so the header does not have to count the whole register
    itself from the page it happens to be showing.
    """
    return await get_referee_stats(db, current_user.id)


@router.get("/referees/options", response_model=list[RefereeOption])
async def list_referee_options(
    q: str | None = Query(None, description="Search by name, email or referee code"),
    referee_type: str | None = Query(None, description="Filter by the kind of official"),
    status: str | None = Query(None, description="Filter by status."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The officials picker: name, photo and type, and nothing else.

    Enough to fill a slot in the officials panel, and not enough to hand out
    contact details to everyone who can open the dropdown.
    """
    referees = await get_referee_options(
        db, user_id=current_user.id, q=q, referee_type=referee_type, status=status
    )
    return [referee_option(referee) for referee in referees]


@router.post(
    "/referees",
    response_model=RefereeCreateResponse,
    status_code=201,
    responses={"400": BAD_REQUEST, "409": CONFLICT},
)
async def create_referee_endpoint(
    data: RefereeCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Register a match official for this account."""
    try:
        referee = await create_referee(db, data, current_user.id)
    except DuplicateRefereeEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = _to_response(referee, reveal_phone=True, reveal_notes=True)
    return RefereeCreateResponse(**response.model_dump(), message="Referee created successfully")


@router.get("/referees/by-code/{referee_code}", response_model=RefereeResponse)
async def get_referee_by_code_endpoint(
    referee_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Look an official up by the permanent code printed on the scorecard.

    The code survives a rename and tells two same-named officials apart, so it
    is the handle to use when a lookup by name is ambiguous.
    """
    referee = await get_referee_by_code(db, referee_code, current_user.id)
    if not referee:
        raise HTTPException(404, "Referee not found")
    reveal = current_user.role == "admin"
    return _to_response(referee, reveal_phone=True, reveal_notes=reveal)


@router.get("/referees/{referee_id}", response_model=RefereeResponse)
async def get_referee_endpoint(
    referee_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One official of this account, with the number in full."""
    referee = await get_referee(db, referee_id, current_user.id)
    if not referee:
        raise HTTPException(404, "Referee not found")
    reveal = current_user.role == "admin"
    return _to_response(referee, reveal_phone=True, reveal_notes=reveal)


@router.get("/referees/{referee_id}/matches", response_model=list[RefereeMatchInfo])
async def list_referee_matches(
    referee_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The matches an official has been appointed to.

    The appointments are the answer to "who is this official on the panel
    of", and the history behind "why cannot this official be deleted".
    """
    referee = await get_referee(db, referee_id, current_user.id)
    if not referee:
        raise HTTPException(404, "Referee not found")
    return await get_referee_matches(db, referee_id, current_user.id)


@router.get("/referees/{referee_id}/delete-check", response_model=RefereeDeleteCheck)
async def referee_delete_check(
    referee_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What deleting this official would do.

    Asked before the delete is attempted, so the confirmation dialog can say
    what is going to happen rather than discovering it afterwards.
    """
    check = await get_referee_delete_check(db, referee_id, current_user.id)
    if check is None:
        raise HTTPException(404, "Referee not found")
    return check


@router.put(
    "/referees/{referee_id}",
    response_model=RefereeResponse,
    deprecated=True,
    summary="Replace a referee (deprecated)",
    description=(
        "Deprecated: use `PATCH /referees/{referee_id}`. This takes the full "
        "`RefereeCreate` body, so a partial update fails with a 422 listing "
        "every field that is missing. Kept working so existing callers do not break."
    ),
    responses={"409": CONFLICT},
)
async def replace_referee_endpoint(
    referee_id: int,
    data: RefereeCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full replacement of every mutable field on an official. Prefer PATCH."""
    try:
        referee = await replace_referee(db, referee_id, data, current_user.id)
    except DuplicateRefereeEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not referee:
        raise HTTPException(404, "Referee not found")
    return _to_response(referee, reveal_phone=True, reveal_notes=True)


@router.patch("/referees/{referee_id}", response_model=RefereeResponse, responses={"409": CONFLICT})
async def update_referee_endpoint(
    referee_id: int,
    data: RefereeUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        referee = await update_referee(db, referee_id, data, current_user.id)
    except DuplicateRefereeEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not referee:
        raise HTTPException(404, "Referee not found")
    return _to_response(referee, reveal_phone=True, reveal_notes=True)


@router.delete("/referees/{referee_id}", status_code=204, responses={"409": CONFLICT})
async def delete_referee_endpoint(
    referee_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove an official who has not officiated a match.

    An official who is named on a match is part of that match's record and is
    kept: unlink them from the match first.
    """
    deleted = await delete_referee(db, referee_id, current_user.id)
    if deleted is None:
        raise HTTPException(404, "Referee not found")
    if deleted is False:
        raise HTTPException(
            409,
            "This referee has officiated a match and cannot be deleted without "
            "corrupting its scorecard.",
        )


@router.post(
    "/referees/{referee_id}/profile-photo",
    response_model=RefereeProfileImageResponse,
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def upload_referee_profile_photo(
    referee_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Attach a photo to an official.

    The stored value is a root-relative path, so it is routable as-is from any
    page - including a nested one such as "/v1/referees/17".

    The type of the file is decided by what the bytes are, not by what the
    request claims: a file that is not one of the allowed image formats is
    refused rather than stored.
    """
    # Ownership is checked before the upload so a 404 does not cost an S3 write
    # on every attempt against an official the caller does not own.
    referee = await get_referee(db, referee_id, current_user.id)
    if not referee:
        raise HTTPException(404, "Referee not found")

    # The photo that is being replaced is noted down now. The session holds
    # one object per row, so once the record is updated the old path is gone
    # from it - and the file behind that path is still in the bucket.
    previous_photo = referee.profile_image

    data = await file.read(_MAX_UPLOAD_BYTES)
    if len(data) > storage.MAX_IMAGE_BYTES:
        raise HTTPException(
            400, f"Image exceeds {storage.MAX_IMAGE_BYTES // (1024 * 1024)}MB limit"
        )

    content_type = storage.sniff_image_type(data)
    if content_type is None or content_type not in storage.ALLOWED_IMAGE_TYPES:
        allowed = ", ".join(sorted(storage.ALLOWED_IMAGE_TYPES))
        raise HTTPException(400, f"Unsupported image type. Allowed: {allowed}")

    # The key comes from the database id and the sniffed content type. The
    # client-supplied filename is never used, so it cannot traverse out of the
    # prefix the way a filename joined onto a directory could.
    key = storage.image_key("referees", referee_id, content_type)
    await storage.put_image(key, data, content_type)

    # The path is written through the crud layer, like every other change to a
    # referee's record, so the write is in one place rather than in the route.
    updated = await update_referee_profile_image(db, referee_id, f"/uploads/{key}", current_user.id)
    if updated is None:
        raise HTTPException(404, "Referee not found")

    # The photo this one replaces is deleted, so a replaced photo does not
    # linger in the bucket for ever. Only a file this module put there is
    # removed: a link to a picture hosted elsewhere is left alone.
    await _delete_stored_photo(previous_photo, key)
    return RefereeProfileImageResponse(referee_id=updated.id, profile_image=updated.profile_image)


@router.post(
    "/referees/{referee_id}/upload-profile-image",
    response_model=RefereeProfileImageResponse,
    deprecated=True,
    summary="Upload a referee profile image (deprecated)",
    description=(
        "Deprecated: use `POST /referees/{referee_id}/profile-photo`. Kept working "
        "so existing callers do not break."
    ),
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def upload_referee_profile_image(
    referee_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Attach a photo to an official. Prefer `POST /referees/{id}/profile-photo`."""
    return await upload_referee_profile_photo(
        referee_id, file=file, current_user=current_user, db=db
    )


@router.delete(
    "/referees/{referee_id}/profile-photo",
    status_code=204,
    responses={"404": NOT_FOUND},
)
async def delete_referee_profile_photo(
    referee_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove an official's photo.

    The record and the file go together: the row is cleared and the stored
    image is deleted, so no file is left behind pointing at nothing.
    """
    referee = await get_referee(db, referee_id, current_user.id)
    if not referee:
        raise HTTPException(404, "Referee not found")

    # Captured before the row is cleared: the file it points at is the one
    # that has to go, and the row no longer knows its name afterwards.
    previous_photo = referee.profile_image

    updated = await clear_referee_profile_image(db, referee_id, current_user.id)
    if updated is None:
        raise HTTPException(404, "Referee not found")

    await _delete_stored_photo(previous_photo)
    return None


async def _delete_stored_photo(profile_image: str | None, keep: str | None = None) -> None:
    """Delete a stored photo, if this module is the one that stored it.

    Only a path under "/uploads/referees/" is removed. A record may carry a
    link to a picture hosted somewhere else, and that link is not this
    application's file to delete.
    """
    if not profile_image:
        return
    prefix = "/uploads/referees/"
    if not profile_image.startswith(prefix):
        return
    key = profile_image[len("/uploads/") :]
    if keep is not None and key == keep:
        return
    await storage.delete_image(key)
