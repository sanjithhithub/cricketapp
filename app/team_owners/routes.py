"""The owners register, and the stakes owners hold on a team.

Two sets of endpoints live here. The first is the register: this account's
owners, searchable, filterable and pageable, each one owned by the account
that entered them. The second is the stake book, reached through the team:
which owner holds which team, on what terms, and whether that stake is still
live.

The rules the whole module runs on:

* a record belongs to the account that created it, and another account's
  record is reported as missing rather than as forbidden, so a response
  cannot be used to probe which ids exist;
* writes are for administrators of the account, with one exception - the
  owner of a record may change their own profile, which is what the
  profile-completion form after a Google sign-in uses;
* the internal notes are internal. They are only carried for a reader who is
  allowed to see them, and the phone number is masked for everyone who is not
  an administrator of the account.
"""

from __future__ import annotations

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
from app.team_owners.crud import (
    DuplicateOwnerEmailError,
    OwnerProfileExistsError,
    OwnershipError,
    OwnershipPercentageError,
    clear_team_owner_profile_image,
    create_team_owner,
    delete_team_owner,
    get_team_owner,
    get_team_owner_by_linked_user,
    get_team_owner_delete_check,
    get_team_owner_options,
    get_team_owner_stats,
    get_team_owners,
    replace_team_owner,
    team_owner_option,
    team_owner_response,
    update_team_owner,
    update_team_owner_profile_image,
)
from app.team_owners.schemas import (
    TeamOwnerCreate,
    TeamOwnerCreateResponse,
    TeamOwnerDeleteCheck,
    TeamOwnerOption,
    TeamOwnerProfileImageResponse,
    TeamOwnerResponse,
    TeamOwnerStats,
    TeamOwnerUpdate,
)

router = APIRouter(tags=["team-owners"])

# The largest photo the API will read into memory before it checks the type.
# The limit is one byte over the storage limit so an oversized file is
# recognised as oversized rather than silently truncated.
_MAX_UPLOAD_BYTES = storage.MAX_IMAGE_BYTES + 1


async def _owner_for_write(db: AsyncSession, owner_id: int, current_user: User):
    """The owner a caller is allowed to change, or ``None``.

    Two ways in. An administrator of the account may change any owner on the
    account's register. Anyone else may change exactly one owner: the one
    linked to their own sign-in, which is the profile they fill in themselves
    after signing in with Google.

    A record the caller may not change is reported as missing rather than as
    forbidden, for the same reason the rest of the app does it: a 403 would
    confirm that the id exists.
    """
    if current_user.role == "admin":
        return await get_team_owner(db, owner_id, current_user.id)
    owner = await get_team_owner_by_linked_user(db, current_user.id)
    if owner is not None and owner.id == owner_id:
        return owner
    return None


def _is_admin(current_user: User) -> bool:
    return current_user.role == "admin"


@router.get(
    "/team-owners",
    response_model=list[TeamOwnerResponse],
    summary="List team owners",
    responses=paginated_list_response("One page of this account's team owners.", paged=True),
)
async def list_team_owners(
    page: int = Query(1, ge=1, description="Page number, starting at 1."),
    page_size: int = Query(100, ge=1, le=500, description="Rows per page."),
    skip: int | None = Query(
        None, ge=0, description="Offset of the first row. Deprecated: use `page`."
    ),
    limit: int | None = Query(
        None, ge=1, le=500, description="Rows per page. Deprecated: use `page_size`."
    ),
    q: str | None = Query(
        None, description="Search by name, email, mobile number or organisation."
    ),
    team_id: int | None = Query(
        None, description="Only owners connected to this team, live stakes or historical ones."
    ),
    ownership_type: str | None = Query(
        None,
        description="Filter by the kind of owner: individual_owner, co_owner, "
        "company_organization or franchise_owner.",
    ),
    ownership_status: str | None = Query(None, description="Filter by status: active or inactive."),
    nationality: str | None = Query(
        None, description="Filter by nationality: a country id or a country name."
    ),
    gender: str | None = Query(
        None, description="Filter by gender: male, female, other or prefer_not_to_say."
    ),
    sort_by: str = Query("id", description="Column to sort on."),
    sort_order: str = Query("asc", description="asc or desc."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    """This account's team owners, one page at a time.

    The phone number is masked here for everyone who is not an admin of the
    account. The internal notes are not carried at all: they are administrative
    detail, the list has no column for them, and a free-text note about a
    person should not travel on a request that is read by whoever loads a page.
    Open one owner to read them in full.
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

    owners, total = await get_team_owners(
        db,
        user_id=current_user.id,
        skip=offset,
        limit=size,
        q=q,
        team_id=team_id,
        ownership_type=ownership_type,
        ownership_status=ownership_status,
        nationality=nationality,
        gender=gender,
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
    reveal = _is_admin(current_user)
    return [team_owner_response(owner, reveal_phone=reveal, reveal_notes=False) for owner in owners]


@router.get(
    "/team-owners/stats",
    response_model=TeamOwnerStats,
    summary="Owner counts for the register header",
)
async def team_owner_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The counts the register's header shows.

    One request, so the header does not have to count the whole register
    itself from the page it happens to be showing.
    """
    return await get_team_owner_stats(db, current_user.id)


@router.get(
    "/team-owners/options", response_model=list[TeamOwnerOption], summary="Owner picker options"
)
async def list_team_owner_options(
    q: str | None = Query(None, description="Search by name, email or organisation."),
    ownership_status: str | None = Query(None, description="Filter by status."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The owner picker: name, photo and kind of owner, and nothing else.

    Enough to pick an owner for a team, and not enough to hand out contact
    details to everyone who can open the dropdown.
    """
    owners = await get_team_owner_options(
        db, user_id=current_user.id, q=q, ownership_status=ownership_status
    )
    return [team_owner_option(owner) for owner in owners]


@router.get("/team-owners/me", response_model=TeamOwnerResponse, summary="My owner profile")
async def get_my_team_owner_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The owner profile the signed-in account has for itself.

    This is the record a person fills in for themselves after signing in - the
    one they may change without being an administrator of somebody else's
    register. An account with no owner profile of its own gets a 404, and
    the front end offers the form that creates one.
    """
    owner = await get_team_owner_by_linked_user(db, current_user.id)
    if owner is None:
        raise HTTPException(404, "No team owner profile is linked to this account")
    return team_owner_response(owner, reveal_phone=True, reveal_notes=_is_admin(current_user))


@router.post(
    "/team-owners/me",
    response_model=TeamOwnerCreateResponse,
    status_code=201,
    summary="Complete my owner profile",
    responses={"400": BAD_REQUEST, "409": CONFLICT},
)
async def create_my_team_owner_profile(
    data: TeamOwnerCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fill in the signed-in account's own owner profile.

    This is the form a person completes after signing in with Google. The link
    to the account comes from the token and never from the body, so a request
    cannot create a profile that belongs to somebody else, and the profile is
    written into the caller's own account.

    The account keeps the first profile it creates: a second request is a 409
    rather than a duplicate record, because the way to change the profile is
    the partial update. Teams may be named here; they are checked against the
    caller's own teams, the same way an administrator's registration is.
    """
    try:
        owner = await create_team_owner(db, data, current_user.id, linked_user_id=current_user.id)
    except OwnerProfileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DuplicateOwnerEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OwnershipPercentageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    response = team_owner_response(owner, reveal_phone=True, reveal_notes=True)
    return TeamOwnerCreateResponse(**response.model_dump(), message="Team owner profile created")


@router.post(
    "/team-owners",
    response_model=TeamOwnerCreateResponse,
    status_code=201,
    summary="Register a team owner",
    responses={"400": BAD_REQUEST, "409": CONFLICT},
)
async def create_team_owner_endpoint(
    data: TeamOwnerCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Register a team owner for this account.

    The owner and the teams they take on are written together: an owner whose
    stake could not be created is not an owner at all.
    """
    try:
        owner = await create_team_owner(db, data, current_user.id)
    except DuplicateOwnerEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OwnershipPercentageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    response = team_owner_response(owner, reveal_phone=True, reveal_notes=True)
    return TeamOwnerCreateResponse(
        **response.model_dump(), message="Team owner created successfully"
    )


@router.get(
    "/team-owners/{owner_id}",
    response_model=TeamOwnerResponse,
    summary="Get one team owner",
    responses={"404": NOT_FOUND},
)
async def get_team_owner_endpoint(
    owner_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One owner of this account, with the number in full."""
    owner = await get_team_owner(db, owner_id, current_user.id)
    if owner is None:
        raise HTTPException(404, "Team owner not found")
    reveal = _is_admin(current_user)
    return team_owner_response(owner, reveal_phone=True, reveal_notes=reveal)


@router.get(
    "/team-owners/{owner_id}/delete-check",
    response_model=TeamOwnerDeleteCheck,
    summary="Check whether an owner can be deleted",
    responses={"404": NOT_FOUND},
)
async def team_owner_delete_check(
    owner_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What deleting this owner would do.

    Asked before the delete is attempted, so the confirmation dialog can say
    what is going to happen rather than discovering it afterwards.
    """
    check = await get_team_owner_delete_check(db, owner_id, current_user.id)
    if check is None:
        raise HTTPException(404, "Team owner not found")
    return check


@router.put(
    "/team-owners/{owner_id}",
    response_model=TeamOwnerResponse,
    deprecated=True,
    summary="Replace a team owner (deprecated)",
    description=(
        "Deprecated: use `PATCH /team-owners/{owner_id}`. This takes the full "
        "`TeamOwnerCreate` body, so a partial update fails with a 422 listing "
        "every field that is missing."
    ),
    responses={"400": BAD_REQUEST, "404": NOT_FOUND, "409": CONFLICT},
)
async def replace_team_owner_endpoint(
    owner_id: int,
    data: TeamOwnerCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Replace every mutable field of one owner. Prefer `PATCH`."""
    try:
        owner = await replace_team_owner(db, owner_id, data, current_user.id)
    except DuplicateOwnerEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OwnershipPercentageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if owner is None:
        raise HTTPException(404, "Team owner not found")
    return team_owner_response(owner, reveal_phone=True, reveal_notes=True)


@router.patch(
    "/team-owners/{owner_id}",
    response_model=TeamOwnerResponse,
    summary="Change a team owner",
    responses={"400": BAD_REQUEST, "404": NOT_FOUND, "409": CONFLICT},
)
async def update_team_owner_endpoint(
    owner_id: int,
    data: TeamOwnerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change one owner of this account.

    A field left out is left as it is. The stakes may be sent with the same
    request: a stake that is new is created, a stake that is already live is
    changed, and a live stake left out is ended rather than deleted, so the
    team's history survives.
    """
    owner = await _owner_for_write(db, owner_id, current_user)
    if owner is None:
        raise HTTPException(404, "Team owner not found")
    try:
        updated = await update_team_owner(
            db, owner_id, data, current_user.id, allow_linked=not _is_admin(current_user)
        )
    except DuplicateOwnerEmailError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OwnershipPercentageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Team owner not found")
    reveal = _is_admin(current_user)
    return team_owner_response(updated, reveal_phone=True, reveal_notes=reveal)


@router.delete(
    "/team-owners/{owner_id}",
    status_code=204,
    summary="Delete a team owner",
    responses={"404": NOT_FOUND, "409": CONFLICT},
)
async def delete_team_owner_endpoint(
    owner_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a team owner.

    An owner who holds a team is part of that team's record, so the delete is
    refused with a 409 while a stake stands: end the ownership first, and the
    club's history is kept. The team, its players and its matches are never
    touched by a delete here in any case.
    """
    deleted = await delete_team_owner(db, owner_id, current_user.id)
    if deleted is None:
        raise HTTPException(404, "Team owner not found")
    if deleted is False:
        raise HTTPException(
            409,
            "This owner still holds a team. End the ownership before deleting the owner.",
        )
    return None


@router.post(
    "/team-owners/{owner_id}/profile-photo",
    response_model=TeamOwnerProfileImageResponse,
    summary="Upload an owner's profile photo",
    responses={"400": BAD_REQUEST, "404": NOT_FOUND},
)
async def upload_team_owner_profile_photo(
    owner_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach a photo to a team owner.

    The file is checked by its content rather than by its name: a caller that
    calls a text file a `.png` does not get a text file stored as one. The
    photo that is being replaced is deleted afterwards, so a replaced photo
    does not linger in the bucket for ever.
    """
    owner = await _owner_for_write(db, owner_id, current_user)
    if owner is None:
        raise HTTPException(404, "Team owner not found")

    # The photo that is being replaced is noted down now. The session holds
    # one object per row, so once the record is updated the old path is gone
    # from it - and the file behind that path is still in the bucket.
    previous_photo = owner.profile_image

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
    key = storage.image_key("team_owners", owner_id, content_type)
    await storage.put_image(key, data, content_type)

    # The path is written through the crud layer, like every other change to an
    # owner's record, so the write is in one place rather than in the route.
    updated = await update_team_owner_profile_image(
        db, owner_id, f"/uploads/{key}", current_user.id
    )
    if updated is None:
        raise HTTPException(404, "Team owner not found")

    # The photo this one replaces is deleted, so a replaced photo does not
    # linger in the bucket for ever. Only a file this module put there is
    # removed: a link to a picture hosted elsewhere is left alone.
    await _delete_stored_photo(previous_photo, key)
    return TeamOwnerProfileImageResponse(owner_id=updated.id, profile_image=updated.profile_image)


@router.delete(
    "/team-owners/{owner_id}/profile-photo",
    status_code=204,
    summary="Remove an owner's profile photo",
    responses={"404": NOT_FOUND},
)
async def delete_team_owner_profile_photo(
    owner_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a team owner's photo.

    The record and the file go together: the row is cleared and the stored
    image is deleted, so no file is left behind pointing at nothing.
    """
    owner = await _owner_for_write(db, owner_id, current_user)
    if owner is None:
        raise HTTPException(404, "Team owner not found")

    # Captured before the row is cleared: the file it points at is the one
    # that has to go, and the row no longer knows its name afterwards.
    previous_photo = owner.profile_image

    updated = await clear_team_owner_profile_image(db, owner_id, current_user.id)
    if updated is None:
        raise HTTPException(404, "Team owner not found")

    await _delete_stored_photo(previous_photo)
    return None


async def _delete_stored_photo(profile_image: str | None, keep: str | None = None) -> None:
    """Delete a stored photo, if this module is the one that stored it.

    Only a path under "/uploads/team_owners/" is removed. A record may carry a
    link to a picture hosted somewhere else, and that link is not this
    application's file to delete.
    """
    if not profile_image:
        return
    prefix = "/uploads/team_owners/"
    if not profile_image.startswith(prefix):
        return
    key = profile_image[len("/uploads/") :]
    if keep is not None and key == keep:
        return
    await storage.delete_image(key)
