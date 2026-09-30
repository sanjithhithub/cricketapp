from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import get_current_user, require_admin
from app.database import get_db
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
from app.players.schemas import (
    PlayerAliasCreate,
    PlayerAliasListResponse,
    PlayerCreate,
    PlayerCreateResponse,
    PlayerDuplicateCheckResponse,
    PlayerDuplicateMatch,
    PlayerDuplicateRequest,
    PlayerResponse,
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


@router.get("/players", response_model=list[PlayerResponse])
async def list_players(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    players = await get_players(db, user_id=current_user.id, skip=skip, limit=limit)
    # Listing is for browsing and picking, so numbers stay masked for everyone
    # who is not an admin. Open the single-player view to see one in full.
    reveal = current_user.role == "admin"
    return [await _to_response(db, p, reveal_phone=reveal) for p in players]


@router.get("/players/search", response_model=list[PlayerResponse])
async def search_players_endpoint(
    q: str = Query(..., min_length=1, description="Search by name, nickname or player code"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    players = await search_players(db, q, user_id=current_user.id, skip=skip, limit=limit)
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
    )
    return PlayerDuplicateCheckResponse(
        phone_matches=[await _duplicate_match(db, p, "phone") for p in result["phone_matches"]],
        name_matches=[await _duplicate_match(db, p, "name") for p in result["name_matches"]],
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


@router.post("/players", response_model=PlayerCreateResponse, status_code=201)
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
            detail={
                "message": str(e),
                "next_action": "confirm_same_person",
                "phone_matches": [
                    # JSON, not a model: an HTTPException detail has to serialise.
                    (await _duplicate_match(db, p, "phone")).model_dump(mode="json")
                    for p in e.matches
                ],
            },
        )
    except DuplicateNameWarningError as e:
        # A name clash is a question, not a refusal. Handing it back as a 409 lets
        # the same confirm-and-resubmit flow handle it; the client sends
        # duplicate_confirmed=true to proceed.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": str(e),
                "next_action": "confirm_duplicate_name",
                "name_matches": [
                    (await _duplicate_match(db, p, "name")).model_dump(mode="json")
                    for p in e.matches
                ],
            },
        )
    except ValueError as e:
        raise HTTPException(409, str(e))

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


@router.put("/players/{player_id}", response_model=PlayerResponse)
async def replace_player_endpoint(
    player_id: int,
    data: PlayerCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
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


@router.post("/players/{player_id}/teams", status_code=201)
async def assign_player_to_team_endpoint(
    player_id: int,
    data: TeamAssignment,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await assign_player_to_team(db, player_id, data, current_user.id)
    if error:
        raise HTTPException(400, error)
    return {"message": "Player assigned to team successfully", "assignment": assignment}


@router.get("/players/{player_id}/teams")
async def get_player_teams_endpoint(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    player = await get_player(db, player_id, current_user.id)
    if not player:
        raise HTTPException(404, "Player not found")
    return await get_player_teams(db, player_id, current_user.id)


@router.patch("/players/{player_id}/teams/{team_id}")
async def update_player_team_role_endpoint(
    player_id: int,
    team_id: int,
    data: TeamAssignmentUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    assignment, error = await update_player_team_role(
        db, player_id, team_id, data.role, current_user.id
    )
    if error:
        raise HTTPException(404, error)
    return {"message": f"Role updated to {data.role}"}


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
