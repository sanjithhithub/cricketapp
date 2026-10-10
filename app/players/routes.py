from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api_docs import (
    BAD_REQUEST,
    NOT_FOUND,
    paginated_list_response,
    set_pagination_headers,
)
from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
from app.players.analytics import get_player_performance
from app.players.crud import (
    DuplicateNameWarningError,
    DuplicatePhoneError,
    add_player_alias,
    assign_player_to_team,
    check_for_duplicates,
    create_player,
    delete_player,
    get_player,
    get_player_aliases,
    get_player_by_code,
    get_player_teams,
    get_players,
    remove_player_from_team,
    replace_player,
    resend_otp_for_player,
    search_players,
    update_player,
    update_player_team_role,
)
from app.players.identity import full_name, mask_mobile
from app.players.models import Player
from app.players.profile import get_player_profile
from app.players.schemas import (
    PlayerAliasCreate,
    PlayerAliasListResponse,
    PlayerCreate,
    PlayerCreateResponse,
    PlayerDuplicateCheckResponse,
    PlayerDuplicateDetail,
    PlayerDuplicateMatch,
    PlayerDuplicateRequest,
    PlayerPerformanceResponse,
    PlayerProfileImageResponse,
    PlayerProfileResponse,
    PlayerResponse,
    PlayerTeamAssignmentResponse,
    PlayerTeamInfo,
    PlayerTeamRoleResponse,
    PlayerUpdate,
    ResendOTPResponse,
    TeamAssignment,
    TeamAssignmentUpdate,
)
from app.teams.models import PlayerTeamAssignment, Team

router = APIRouter(tags=["players"])


async def _team_name_for(db: AsyncSession, player_id: int) -> str | None:
    """The club a player currently plays for, for disambiguating lookups."""
    result = await db.execute(
        select(Team.name)
        .join(PlayerTeamAssignment, PlayerTeamAssignment.team_id == Team.id)
        .where(PlayerTeamAssignment.player_id == player_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _to_response(db: AsyncSession, player: Player, *, reveal_phone: bool) -> PlayerResponse:
    """Build a response for one player.

    ``reveal_phone`` is the authorisation decision. A list, a search result or a
    duplicate-check candidate masks the number; only an admin, or the user looking
    at their own record, sees it in full. A number is personal data and a scorer
    only ever needs the code, photo and club to tell players apart.
    """
    return PlayerResponse(
        id=player.id,
        player_code=player.player_code,
        first_name=player.first_name,
        last_name=player.last_name,
        full_name=full_name(player.first_name, player.last_name),
        date_of_birth=player.date_of_birth,
        gender=player.gender,
        profile_image=player.profile_image,
        batting_hand=player.batting_hand,
        batting_position=player.batting_position,
        bowling_hand=player.bowling_hand,
        bowling_type=player.bowling_type,
        country_id=player.country_id,
        state_id=player.state_id,
        city_id=player.city_id,
        height=player.height,
        weight=player.weight,
        country_code=player.country_code,
        mobile_number=(
            player.mobile_number
            if reveal_phone
            else mask_mobile(player.country_code, player.mobile_number)
        ),
        phone_display=mask_mobile(player.country_code, player.mobile_number),
        email=player.email,
        is_phone_verified=player.is_phone_verified,
        aliases=await get_player_aliases(db, player.id),
    )


def _can_reveal_phone(user: User, player: Player) -> bool:
    return user.role == "admin" or player.user_id == user.id


@router.get(
    "/players",
    response_model=list[PlayerResponse],
    responses=paginated_list_response("One page of this account's players."),
)
async def list_players(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    players, total = await get_players(db, user_id=current_user.id, skip=skip, limit=limit)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    # Listing is for browsing and picking, so numbers stay masked for everyone
    # who is not an admin. Open the single-player view to see one in full.
    reveal = current_user.role == "admin"
    return [await _to_response(db, p, reveal_phone=reveal) for p in players]


@router.get(
    "/players/search",
    response_model=list[PlayerResponse],
    responses=paginated_list_response("One page of players matching `q`."),
)
async def search_players_endpoint(
    q: str = Query(..., min_length=1, description="Search by name, nickname or player code"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    players, total = await search_players(db, q, user_id=current_user.id, skip=skip, limit=limit)
    set_pagination_headers(response, total=total, skip=skip, limit=limit)
    reveal = current_user.role == "admin"
    return [await _to_response(db, p, reveal_phone=reveal) for p in players]


async def _duplicate_match(
    db: AsyncSession, player: Player, matched_on: str
) -> PlayerDuplicateMatch:
    return PlayerDuplicateMatch(
        id=player.id,
        player_code=player.player_code,
        first_name=player.first_name,
        last_name=player.last_name,
        full_name=full_name(player.first_name, player.last_name),
        profile_image=player.profile_image,
        date_of_birth=player.date_of_birth,
        country_code=player.country_code,
        mobile_number=mask_mobile(player.country_code, player.mobile_number),
        is_phone_verified=player.is_phone_verified,
        team_name=await _team_name_for(db, player.id),
        aliases=await get_player_aliases(db, player.id),
        matched_on=matched_on,
    )


@router.post("/players/check-duplicate", response_model=PlayerDuplicateCheckResponse)
async def check_player_duplicate_endpoint(
    data: PlayerDuplicateRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ask "does this person already exist?" before a player is created.

    Answers with candidates and what to do next, never with a decision. Nothing
    is merged, nothing is blocked on its own, and two players sharing a name or a
    number is reported rather than treated as an error.
    """
    result = await check_for_duplicates(
        db,
        current_user.id,
        first_name=data.first_name,
        last_name=data.last_name,
        country_code=data.country_code,
        mobile_number=data.mobile_number,
        date_of_birth=data.date_of_birth,
        email=data.email,
    )
    return PlayerDuplicateCheckResponse(
        phone_matches=[await _duplicate_match(db, p, "phone") for p in result["phone_matches"]],
        name_matches=[await _duplicate_match(db, p, "name") for p in result["name_matches"]],
        email_matches=[await _duplicate_match(db, p, "email") for p in result["email_matches"]],
        duplicate_name=result["duplicate_name"],
        phone_taken=result["phone_taken"],
        requires_confirmation=result["requires_confirmation"],
        next_action=result["next_action"],
        message=result["message"],
    )


@router.get("/players/by-code/{player_code}", response_model=PlayerResponse)
async def get_player_by_code_endpoint(
    player_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Look a player up by their permanent code.

    The code is the one identifier that survives a rename and distinguishes two
    people who share a name or a number, so it is the handle to use when a phone
    lookup is ambiguous.
    """
    player = await get_player_by_code(db, player_code, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await _to_response(db, player, reveal_phone=_can_reveal_phone(current_user, player))


@router.post(
    "/players",
    response_model=PlayerCreateResponse,
    status_code=201,
    responses={
        "400": BAD_REQUEST,
        "409": {
            "description": (
                "Nothing was created. Either the registration matches someone on "
                "file, or it cannot be carried out against the current state - the "
                "squad is full, the playing XI is capped, or the team named in the "
                "body does not exist.\n\n"
                "Check for `detail.next_action`: when it is present the detail is "
                "the structured duplicate payload and the flow is to show the "
                "matches to a human, then resubmit with `duplicate_confirmed: "
                "true` or `existing_player_id`. When it is absent, `detail` is a "
                "plain sentence explaining what to change."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/DuplicatePlayerConflict"},
                            {"$ref": "#/components/schemas/ErrorResponse"},
                        ]
                    }
                }
            },
        },
    },
)
async def create_player_endpoint(
    data: PlayerCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    duplicate_name_warning = None
    try:
        player, otp_sent, team_assignment = await create_player(db, data, current_user.id)
    except DuplicatePhoneError as e:
        # Report the candidates so the client can show them and ask. 409 because
        # the request conflicts with what is on file, not because it was invalid.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=PlayerDuplicateDetail(
                message=str(e),
                next_action="confirm_same_person",
                phone_matches=[await _duplicate_match(db, p, "phone") for p in e.matches],
            ).model_dump(mode="json"),
        )
    except DuplicateNameWarningError as e:
        # A name clash is a question, not a refusal. Handing it back as a 409 lets
        # the same confirm-and-resubmit flow handle it; the client sends
        # duplicate_confirmed=true to proceed.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=PlayerDuplicateDetail(
                message=str(e),
                next_action="confirm_duplicate_name",
                name_matches=[await _duplicate_match(db, p, "name") for p in e.matches],
            ).model_dump(mode="json"),
        )
    except ValueError as e:
        # Not a duplicate: the squad is full, the playing XI is capped, or the team
        # or linked player named in the body does not exist. Still a 409 - the
        # request is well formed and cannot be carried out against the current
        # state - but with a plain sentence, not the duplicate payload. Hence the
        # `anyOf` on the 409 in this operation's responses: a client that gets a
        # 409 must look for `next_action` and fall back to reading `detail` as a
        # string when it is absent.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e

    if data.duplicate_confirmed:
        duplicate_name_warning = (
            "Registered with a name that already exists. This is a separate player."
        )

    linked_existing = data.existing_player_id is not None
    base = await _to_response(db, player, reveal_phone=True)
    if otp_sent and data.team_id is not None:
        message = "Player created and team assigned. OTP sent to mobile number via SMS."
    elif otp_sent:
        message = "Player created. OTP sent to mobile number via SMS."
    elif data.mobile_number:
        message = (
            "Player created. OTP SMS could not be sent. Please check the SMS service configuration."
        )
    else:
        message = "Player created. No mobile number on file, so no OTP was sent."
    if linked_existing:
        message = (
            f"Matched to existing player {player.player_code}. "
            "Their record was updated instead of creating a duplicate."
        )
    return PlayerCreateResponse(
        **base.model_dump(),
        message=message,
        otp_sent=otp_sent,
        team_assignment=team_assignment,
        linked_existing=linked_existing,
        duplicate_name_warning=duplicate_name_warning,
        # Echoed rather than dropped: the client can then show the nickname it
        # just registered, and knows which one to expect in `aliases` later.
        alias=data.alias or (base.aliases[0] if linked_existing and base.aliases else None),
    )


@router.get("/players/{player_id}/aliases", response_model=PlayerAliasListResponse)
async def list_player_aliases_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return PlayerAliasListResponse(
        player_id=player.id,
        player_code=player.player_code,
        aliases=await get_player_aliases(db, player.id),
    )


@router.post("/players/{player_id}/aliases", response_model=PlayerAliasListResponse)
async def add_player_alias_endpoint(
    player_id: int,
    data: PlayerAliasCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Record a nickname against this player.

    The alias points at the existing player id, so "Rohit" and "Rohit Sharma"
    stay one person instead of becoming two records that drift apart.
    """
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    aliases = await add_player_alias(db, player.id, data.alias)
    await db.commit()
    return PlayerAliasListResponse(
        player_id=player.id, player_code=player.player_code, aliases=aliases
    )


@router.post("/players/{player_id}/resend-otp", response_model=ResendOTPResponse)
async def resend_otp_endpoint(
    player_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    player, otp_sent = await resend_otp_for_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return ResendOTPResponse(
        message=(
            "New OTP sent to mobile number via SMS."
            if otp_sent
            else "New OTP could not be sent. Please check the SMS service configuration."
        ),
        otp_sent=otp_sent,
    )


@router.get(
    "/players/{player_id}/performance",
    response_model=PlayerPerformanceResponse,
    responses={"404": NOT_FOUND},
)
async def get_player_performance_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Career performance for one player.

    Counts only innings from matches this account owns. Batting covers matches,
    innings, runs, average, strike rate, highest score and boundary counts;
    bowling covers overs, wickets, economy, best figures, maidens, wides,
    no-balls and dot balls. Fielding figures (catches, run-outs, stumpings) are
    reported as unavailable because the delivery log does not record the fielder
    who took the dismissal.
    """
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    payload = await get_player_performance(db, player, current_user.id)
    return PlayerPerformanceResponse.model_validate(payload)


@router.get(
    "/players/{player_id}/profile",
    response_model=PlayerProfileResponse,
    responses={"404": NOT_FOUND},
)
async def get_player_profile_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A player's record: their details, career totals and per-competition splits.

    Only completed matches count, and only those owned by this account. A player
    is placed in a competition by the level (league) of the side they turned out
    for, using the app's ``TeamLevel`` vocabulary (IPL, World Cup, Ranji Trophy,
    ...). Fielding figures are reported as unavailable for the same reason as
    ``GET /players/{player_id}/performance``: the delivery log records who was
    dismissed, not the fielder.
    """
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    payload = await get_player_profile(db, player, current_user.id)
    return PlayerProfileResponse(
        player=await _to_response(db, player, reveal_phone=_can_reveal_phone(current_user, player)),
        **payload,
    )


@router.get("/players/{player_id}", response_model=PlayerResponse)
async def get_player_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The one player view that may show a full number.

    Scoped to the owner, so this is the record's own user reading their own
    contact details - which they need, for instance to verify the phone by OTP.
    """
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await _to_response(db, player, reveal_phone=_can_reveal_phone(current_user, player))


@router.put(
    "/players/{player_id}",
    response_model=PlayerResponse,
    deprecated=True,
    summary="Replace a player (deprecated)",
    description=(
        "Deprecated: use `PATCH /players/{player_id}`. This takes the full "
        "`PlayerCreate` body, so a partial update fails with a 422 listing every "
        "field that is missing - which is the opposite of what a client sending "
        "one changed field expects. Kept working so existing callers do not break."
    ),
)
async def replace_player_endpoint(
    player_id: int,
    data: PlayerCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full replacement of every mutable field on a player.

    Prefer PATCH. A PUT here is a footgun precisely because its body is the
    create schema: omitting one field is not "leave it alone", it is a 422.
    """
    player = await replace_player(db, player_id, data, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await _to_response(db, player, reveal_phone=True)


@router.patch("/players/{player_id}", response_model=PlayerResponse)
async def update_player_endpoint(
    player_id: int,
    data: PlayerUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    player = await update_player(db, player_id, data, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await _to_response(db, player, reveal_phone=True)


@router.delete("/players/{player_id}", status_code=204)
async def delete_player_endpoint(
    player_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_player(db, player_id, current_user.id)
    if not deleted:
        raise HTTPException(404, "Player not found")


@router.post(
    "/players/{player_id}/teams",
    status_code=201,
    response_model=PlayerTeamAssignmentResponse,
    responses={"400": BAD_REQUEST},
)
async def assign_player_to_team_endpoint(
    player_id: int,
    data: TeamAssignment,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await assign_player_to_team(db, player_id, data, current_user.id)
    if error:
        # The squad is full, the XI is capped, or the player already plays at this
        # level. Every one of those is fixed by picking differently, not by a
        # conversation about identity.
        raise HTTPException(400, error)
    return PlayerTeamAssignmentResponse(
        message="Player assigned to team successfully", assignment=assignment
    )


@router.get(
    "/players/{player_id}/teams",
    response_model=list[PlayerTeamInfo],
    responses={"404": NOT_FOUND},
)
async def get_player_teams_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every club this player currently plays for, at the level they play at."""
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await get_player_teams(db, player_id, current_user.id)


@router.patch(
    "/players/{player_id}/teams/{team_id}",
    response_model=PlayerTeamRoleResponse,
    responses={"400": BAD_REQUEST},
)
async def update_player_team_role_endpoint(
    player_id: int,
    team_id: int,
    data: TeamAssignmentUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Move a player between the playing XI, the substitutes and the bench.

    Promoting into the XI is refused when it is already full, which is why this
    can be a 400 and not only a 404.
    """
    assignment, error = await update_player_team_role(
        db, player_id, team_id, data.role, current_user.id
    )
    if error:
        raise HTTPException(400, error)
    return PlayerTeamRoleResponse(
        message=f"Role updated to {assignment.role}",
        player_id=player_id,
        team_id=team_id,
        role=assignment.role,
    )


@router.delete("/players/{player_id}/teams/{team_id}", status_code=204)
async def remove_player_from_team_endpoint(
    player_id: int,
    team_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    removed = await remove_player_from_team(db, player_id, team_id, current_user.id)
    if not removed:
        raise HTTPException(404, "Player not found in this team")


@router.post(
    "/players/{player_id}/upload-profile-image",
    response_model=PlayerProfileImageResponse,
    responses={"400": BAD_REQUEST},
)
async def upload_player_profile_image(
    player_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Ownership is checked before the upload so a 404 does not cost an S3 write
    # on every attempt against a player the caller does not own.
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == current_user.id)
    )
    player = result.scalar_one_or_none()
    if not player:
        raise HTTPException(404, "Player not found")

    # The key comes from the database id and the sniffed content type. The
    # client-supplied filename is never used, so it cannot traverse out of the
    # prefix the way the previous os.path.join(UPLOAD_DIR, filename) could.
    try:
        key = storage.image_key("players", player_id, file.content_type or "")
        await storage.put_image(key, await file.read(), file.content_type or "")
    except storage.InvalidImage as exc:
        raise HTTPException(400, str(exc)) from exc

    # Root-relative so the value works from a nested page ("/v1/players/17").
    player.profile_image = f"/uploads/{key}"
    await db.commit()
    await db.refresh(player)
    return PlayerProfileImageResponse(player_id=player.id, profile_image=player.profile_image)
