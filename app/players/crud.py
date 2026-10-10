import os
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.levels.models import TeamLevel
from app.models import OTP, City, Country, State
from app.players.identity import (
    alias_key,
    full_name,
    generate_player_code,
    mask_mobile,
    normalize_country_code,
    normalize_mobile,
    phone_e164,
)
from app.players.models import Player, PlayerAlias
from app.players.schemas import PlayerCreate, PlayerUpdate, TeamAssignment
from app.sms import send_otp
from app.teams.models import PlayerTeamAssignment, Team
from app.timeutils import utcnow

MAX_SQUAD_SIZE = 15
# A side fields 11 on the field for every format this app scores (T20, ODI and
# TEST all bat 11). Squads are larger - MAX_SQUAD_SIZE above - so this caps only
# the playing XI, not the squad.
PLAYING_XI_SIZE = 11
# How many times code generation is retried when two concurrent registrations
# happen to draw the same code. The unique index is the real guarantee; this just
# keeps a collision from surfacing as a 500.
CODE_GENERATION_ATTEMPTS = 5


async def get_players(
    db: AsyncSession, user_id: int, skip: int = 0, limit: int = 100
) -> tuple[list[Player], int]:
    """One page of this account's players, and the total that page is drawn from.

    The count is returned rather than left to the caller to guess, because a bare
    array gives no way to tell "that is all of them" from "there are more past the
    limit" - and a limit of 500 that silently truncates is how players disappear
    from a list without anything failing.
    """
    total = int(
        (await db.execute(select(func.count(Player.id)).where(Player.user_id == user_id))).scalar()
        or 0
    )
    # Newest first: a player someone has just registered has to be visible in
    # the list without the user knowing whether the default page covers them.
    result = await db.execute(
        select(Player)
        .where(Player.user_id == user_id)
        .order_by(Player.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_player(db: AsyncSession, player_id: int, user_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_player_by_code(db: AsyncSession, player_code: str, user_id: int):
    result = await db.execute(
        select(Player).where(
            func.upper(Player.player_code) == str(player_code or "").strip().upper(),
            Player.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_players_by_phone(
    db: AsyncSession, country_code: str, mobile_number: str | int, user_id: int
) -> list[Player]:
    """Every player registered with this number. Usually zero or one.

    A list, not a single row, because the number is not a unique key: it may be
    absent, and it may be shared. Callers that need a decision (assigning a
    player to a team from a typed number) have to cope with more than one hit.
    """
    key = phone_e164(country_code, mobile_number)
    if not key:
        return []
    result = await db.execute(
        select(Player)
        .where(Player.phone_e164 == key, Player.user_id == user_id)
        .order_by(Player.id)
    )
    return list(result.scalars().all())


async def get_player_by_phone(
    db: AsyncSession, country_code: str, mobile_number: str | int, user_id: int
):
    """The single player with this number, or ``None``.

    Returns ``None`` when the number matches more than one player rather than
    raising: shared numbers are legitimate, so the caller is told "ambiguous" and
    asked to disambiguate by player code instead of getting a 500.
    """
    matches = await get_players_by_phone(db, country_code, mobile_number, user_id)
    if len(matches) == 1:
        return matches[0]
    return None


async def get_player_aliases(db: AsyncSession, player_id: int) -> list[str]:
    result = await db.execute(
        select(PlayerAlias.alias)
        .where(PlayerAlias.player_id == player_id)
        .order_by(PlayerAlias.alias)
    )
    return list(result.scalars().all())


async def add_player_alias(db: AsyncSession, player_id: int, alias: str) -> list[str]:
    """Attach a nickname to an existing player id.

    Idempotent: re-adding the same nickname (in any casing or spacing) is a no-op
    rather than an error, because a scorer typing "  rahul " twice should not hit
    a failure.
    """
    cleaned = " ".join(str(alias or "").split())
    if not cleaned:
        return await get_player_aliases(db, player_id)
    key = alias_key(cleaned)
    existing = await db.execute(
        select(PlayerAlias).where(PlayerAlias.player_id == player_id, PlayerAlias.alias_key == key)
    )
    if existing.scalar_one_or_none() is None:
        db.add(PlayerAlias(player_id=player_id, alias=cleaned, alias_key=key))
        await db.flush()
    return await get_player_aliases(db, player_id)


def _search_predicate(query: str, user_id: int):
    """The name/alias/code predicate a search counts and pages with.

    One function so the count cannot drift from the page: if they were built
    separately, ``X-Total-Count`` would describe a different query than the rows
    returned beside it.
    """
    term = f"%{query.strip()}%"
    alias_player_ids = select(PlayerAlias.player_id).where(PlayerAlias.alias_key.ilike(term))
    return or_(
        Player.first_name.ilike(term),
        Player.last_name.ilike(term),
        Player.player_code.ilike(term),
        Player.id.in_(alias_player_ids),
    )


async def search_players(
    db: AsyncSession, query: str, user_id: int, skip: int = 0, limit: int = 100
) -> tuple[list[Player], int]:
    """Name search that also resolves nicknames, plus the total match count.

    A recorded alias matches the player it points at rather than creating a
    second one, so searching "Rohit" finds the player whose alias is "Rohit" even
    if their name field says something longer.
    """
    predicate = _search_predicate(query, user_id)
    total = int(
        (
            await db.execute(
                select(func.count(Player.id)).where(Player.user_id == user_id, predicate)
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        select(Player)
        .where(Player.user_id == user_id, predicate)
        .order_by(Player.last_name, Player.first_name, Player.id)
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_unassigned_players(db: AsyncSession, user_id: int, skip: int = 0, limit: int = 100):
    subq = select(PlayerTeamAssignment.player_id).distinct().scalar_subquery()
    result = await db.execute(
        select(Player)
        .where(Player.id.notin_(subq), Player.user_id == user_id)
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


async def _create_otp_record(db: AsyncSession, country_code: str, mobile_number: str | None):
    """Raise an OTP for a phone number. A player with no number gets none."""
    if not mobile_number:
        return None, False

    number = str(mobile_number)
    if os.getenv("OTP_SMS_ENABLED", "true").lower() == "false":
        expires_at = utcnow() + timedelta(minutes=5)
        otp = OTP(
            country_code=country_code,
            mobile_number=number,
            otp_code="",
            session_id=None,
            expires_at=expires_at,
        )
        db.add(otp)
        await db.flush()
        return otp, False

    otp_sent, session_info = await send_otp(country_code, number)
    expires_at = utcnow() + timedelta(minutes=5)
    otp = OTP(
        country_code=country_code,
        mobile_number=number,
        otp_code="",
        session_id=session_info,
        expires_at=expires_at,
    )
    db.add(otp)
    await db.flush()
    return otp, otp_sent


async def _assign_new_player_code(db: AsyncSession) -> str:
    """Draw a code that is not already taken.

    The unique index on `player_code` is the actual guarantee against two
    players sharing one; this loop just makes the rare collision invisible
    instead of turning it into a failed registration.
    """
    for _ in range(CODE_GENERATION_ATTEMPTS):
        code = generate_player_code()
        taken = await db.execute(
            select(Player.id).where(func.upper(Player.player_code) == code).limit(1)
        )
        if taken.scalar_one_or_none() is None:
            return code
    return generate_player_code()


class DuplicatePhoneError(ValueError):
    """A phone number is already on file and the caller has not confirmed.

    Carries the candidates so the API can show them and ask, instead of
    returning a bare "duplicate" that leaves the user stuck.
    """

    def __init__(self, matches: list[Player], message: str | None = None):
        self.matches = matches
        super().__init__(message or "This mobile number is already registered")


class DuplicateNameWarningError(ValueError):
    """The name matches existing players but the number does not.

    A warning, not a block: different people genuinely share names. The route
    turns this into a soft confirmation the user can accept.
    """

    def __init__(self, matches: list[Player], message: str):
        self.matches = matches
        super().__init__(message)


async def find_phone_matches(
    db: AsyncSession, country_code: str, mobile_number: str | int, user_id: int
) -> list[Player]:
    return await get_players_by_phone(db, country_code, mobile_number, user_id)


async def find_email_matches(db: AsyncSession, email: str, user_id: int) -> list[Player]:
    """Players already registered under the same email address.

    An email may be shared (a parent's email for two children, a club's shared
    address), so this is advisory only - the caller warns, it never blocks. The
    comparison is case-insensitive so "A@x.com" and "a@x.com" are recognised as
    the same inbox, and it stays inside the account so another account's record
    is never surfaced.
    """
    needle = (email or "").strip().lower()
    if not needle:
        return []
    result = await db.execute(
        select(Player).where(
            Player.user_id == user_id,
            func.lower(func.trim(Player.email)) == needle,
        )
    )
    return list(result.scalars().all())


async def find_name_matches(
    db: AsyncSession,
    first_name: str,
    last_name: str,
    user_id: int,
    exclude_id: int | None = None,
) -> list[Player]:
    """Players already registered under the same full name.

    A name is not a key, so this is advisory information only. Matched on the
    normalised name so "Rahul Sharma" and "rahul  sharma" are recognised as the
    same string while remaining two separate people.
    """
    first = alias_key(first_name)
    last = alias_key(last_name)
    if not first or not last:
        return []
    result = await db.execute(
        select(Player)
        .where(
            Player.user_id == user_id,
            func.lower(func.trim(Player.first_name)) == first,
            func.lower(func.trim(Player.last_name)) == last,
        )
        .order_by(Player.id)
    )
    matches = list(result.scalars().all())
    if exclude_id is not None:
        matches = [p for p in matches if p.id != exclude_id]
    return matches


async def check_for_duplicates(
    db: AsyncSession,
    user_id: int,
    *,
    first_name: str = "",
    last_name: str = "",
    country_code: str = "",
    mobile_number: str | int | None = None,
    date_of_birth=None,
    email: str = "",
) -> dict:
    """Collect possible duplicates for a registration that has not happened yet.

    Returns candidates and a recommended next action. It never decides that two
    records are the same person and never blocks anything on its own - the caller
    shows the candidates and a human confirms.
    """
    phone_matches = await find_phone_matches(db, country_code, mobile_number, user_id)
    name_matches = await find_name_matches(db, first_name, last_name, user_id)
    email_matches = await find_email_matches(db, email, user_id)

    # A name match that is also the same phone is the same candidate, not two.
    phone_ids = {p.id for p in phone_matches}
    name_only = [p for p in name_matches if p.id not in phone_ids]
    # Same for email matches: a player who matches on both the number and the
    # address is reported once, under the number.
    email_only = [p for p in email_matches if p.id not in phone_ids]

    if phone_matches:
        next_action = "confirm_same_person"
        message = (
            f"{len(phone_matches)} player(s) are already registered with this mobile "
            "number. Confirm whether this is the same person to update them, or "
            "continue to register a separate player who shares the number."
        )
    elif name_only:
        next_action = "confirm_duplicate_name"
        message = (
            f"{len(name_only)} player(s) already have this name. Names are not "
            "unique in this app - continue to register a separate player, or pick "
            "the existing player if this is the same person."
        )
    elif email_only:
        next_action = "none"
        message = (
            f"{len(email_only)} player(s) already have this email address. Emails "
            "are not unique in this app either - continue if this is a separate "
            "player, or pick the existing player if this is the same person."
        )
    else:
        next_action = "none"
        message = "No possible duplicates found."

    return {
        "phone_matches": phone_matches,
        "name_matches": name_only,
        "email_matches": email_only,
        "duplicate_name": bool(name_only),
        "phone_taken": bool(phone_matches),
        "requires_confirmation": bool(phone_matches or name_only),
        "next_action": next_action,
        "message": message,
    }


async def create_player(db: AsyncSession, data: PlayerCreate, user_id: int):
    """Register a player, after asking about anything that looks like a duplicate.

    The order matters and is the whole point of this function:

    * A number already on file stops the registration and hands back the
      candidates, unless the caller confirmed it is the same person
      (``existing_player_id``) or confirmed they are two different people who
      share a number (``duplicate_confirmed``).
    * A name that already exists is only a warning. It never blocks, and the
      player is never merged into the existing record.
    * A player with no number is allowed. That is the one case with no signal to
      compare, so an explicit confirmation is required before a second record is
      created for the same name.
    """
    country_code = normalize_country_code(data.country_code)
    mobile = normalize_mobile(data.mobile_number)

    if data.existing_player_id is not None:
        return await _link_to_existing_player(db, data, user_id, country_code, mobile)

    phone_matches = await find_phone_matches(db, country_code, mobile, user_id) if mobile else []
    name_matches = await find_name_matches(db, data.first_name, data.last_name, user_id)

    if phone_matches and not data.duplicate_confirmed:
        raise DuplicatePhoneError(phone_matches)

    if not phone_matches and name_matches and not data.duplicate_confirmed:
        # No usable signal (no number, or a number nobody else has) and the name
        # is taken. Ask once; after that the registration goes through.
        raise DuplicateNameWarningError(
            name_matches,
            f"{len(name_matches)} player(s) already have the name "
            f"{full_name(data.first_name, data.last_name)!r}. Confirm to register a "
            "separate player with the same name.",
        )

    # The phone fields come from the normalised values, not from the raw payload,
    # so the stored number and its comparison key can never disagree.
    fields = data.model_dump(
        exclude={
            "team_id",
            "role",
            "duplicate_confirmed",
            "alias",
            "existing_player_id",
            "country_code",
            "mobile_number",
        }
    ) | {"country_code": country_code, "mobile_number": mobile}

    player = Player(
        player_code=await _assign_new_player_code(db),
        **fields,
        phone_e164=phone_e164(country_code, mobile),
        user_id=user_id,
    )
    db.add(player)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Only a player_code collision is recoverable here: the code is drawn
        # randomly, so two concurrent registrations can land on the same draw and
        # the unique index correctly refuses the second. Draw another code and
        # try again rather than failing the request.
        #
        # Anything else - a NOT NULL failure, an FK reference to a row that does
        # not exist, a constraint that was never meant to be retried - is rolled
        # back and reported as a plain conflict instead of re-running the
        # *identical* insert. The old code retried unconditionally, so a duplicate
        # email (then unique) inserted twice, failed twice, and leaked a 500.
        if "player_code" not in str(exc.orig).lower():
            await db.rollback()
            raise ValueError(
                "The player could not be registered: it conflicts with an existing "
                "record or constraint. Nothing was created."
            ) from exc
        await db.rollback()
        player = Player(
            player_code=await _assign_new_player_code(db),
            **fields,
            phone_e164=phone_e164(country_code, mobile),
            user_id=user_id,
        )
        db.add(player)
        await db.flush()

    if data.alias:
        await add_player_alias(db, player.id, data.alias)

    otp_sent = False
    if mobile:
        _, otp_sent = await _create_otp_record(db, country_code, mobile)

    team_assignment = None
    if data.team_id is not None:
        team_result = await db.execute(
            select(Team).where(Team.id == data.team_id, Team.user_id == user_id)
        )
        team = team_result.scalar_one_or_none()
        if not team:
            raise ValueError("Team not found")

        assignment, error = await assign_player_to_team(
            db,
            player.id,
            TeamAssignment(team_id=data.team_id, level_id=team.level_id, role=data.role),
            user_id,
        )
        if error:
            raise ValueError(error)

        level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == team.level_id))
        level = level_result.scalar_one_or_none()
        team_assignment = {
            "team_id": team.id,
            "team_name": team.name,
            "level_id": team.level_id,
            "level_name": level.name if level else "Unknown",
            "role": data.role,
        }
    else:
        await db.commit()

    await db.refresh(player)
    return player, otp_sent, team_assignment


async def _link_to_existing_player(
    db: AsyncSession,
    data: PlayerCreate,
    user_id: int,
    country_code: str,
    mobile: str | None,
):
    """Treat a confirmed registration as an update of an existing player.

    The user said "this is the same person", so no second row is created. The
    existing record is topped up with anything it is missing rather than
    overwritten, because their match history is attached to it.
    """
    existing = await get_player(db, data.existing_player_id, user_id)
    if not existing:
        raise ValueError("Player not found")

    if mobile and not existing.mobile_number:
        existing.country_code = country_code or existing.country_code
        existing.mobile_number = mobile
        existing.phone_e164 = phone_e164(existing.country_code, mobile)

    if data.alias:
        await add_player_alias(db, existing.id, data.alias)

    team_assignment = None
    if data.team_id is not None:
        team_result = await db.execute(
            select(Team).where(Team.id == data.team_id, Team.user_id == user_id)
        )
        team = team_result.scalar_one_or_none()
        if not team:
            raise ValueError("Team not found")
        assignment, error = await assign_player_to_team(
            db,
            existing.id,
            TeamAssignment(team_id=data.team_id, level_id=team.level_id, role=data.role),
            user_id,
        )
        if error:
            raise ValueError(error)
        level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == team.level_id))
        level = level_result.scalar_one_or_none()
        team_assignment = {
            "team_id": team.id,
            "team_name": team.name,
            "level_id": team.level_id,
            "level_name": level.name if level else "Unknown",
            "role": data.role,
        }

    await db.commit()
    await db.refresh(existing)
    return existing, False, team_assignment


async def resend_otp_for_player(db: AsyncSession, player_id: int, user_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    player = result.scalar_one_or_none()
    if not player:
        return None, False

    _, otp_sent = await _create_otp_record(db, player.country_code, player.mobile_number)

    await db.commit()
    return player, otp_sent


async def update_player(db: AsyncSession, player_id: int, data: PlayerUpdate, user_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    player = result.scalar_one_or_none()
    if not player:
        return None
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(player, key, val)
    # A number change has to re-derive the comparison key, or the duplicate
    # check would keep matching the player on a number they no longer have.
    if "mobile_number" in data.model_fields_set or "country_code" in data.model_fields_set:
        player.country_code = normalize_country_code(player.country_code) or player.country_code
        player.mobile_number = normalize_mobile(player.mobile_number)
        player.phone_e164 = phone_e164(player.country_code, player.mobile_number)
        # The new number has not been proven to belong to them.
        player.is_phone_verified = False
    await db.commit()
    await db.refresh(player)
    return player


async def replace_player(db: AsyncSession, player_id: int, data: PlayerCreate, user_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    player = result.scalar_one_or_none()
    if not player:
        return None
    for key, val in data.model_dump(
        exclude={"team_id", "role", "duplicate_confirmed", "alias", "existing_player_id"}
    ).items():
        setattr(player, key, val)
    player.country_code = normalize_country_code(player.country_code) or player.country_code
    player.mobile_number = normalize_mobile(player.mobile_number)
    player.phone_e164 = phone_e164(player.country_code, player.mobile_number)
    if data.alias:
        await add_player_alias(db, player.id, data.alias)
    await db.commit()
    await db.refresh(player)
    return player


async def delete_player(db: AsyncSession, player_id: int, user_id: int):
    result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    player = result.scalar_one_or_none()
    if not player:
        return False
    await db.delete(player)
    await db.commit()
    return True


async def mark_player_phone_verified(
    db: AsyncSession,
    country_code: str,
    mobile_number: str | int,
    user_id: int,
):
    """Flag the players holding this number as verified.

    Iterates the matches rather than demanding exactly one: a shared number
    means both holders were sent the same code, so both are confirmed. Returning
    a count lets the caller report honestly instead of implying one player.
    """
    matches = await get_players_by_phone(db, country_code, mobile_number, user_id)
    for player in matches:
        player.is_phone_verified = True
    if matches:
        await db.commit()
        return len(matches)
    return 0


async def assign_player_to_team(
    db: AsyncSession, player_id: int, data: TeamAssignment, user_id: int
):
    player_result = await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user_id)
    )
    player = player_result.scalar_one_or_none()
    if not player:
        return None, "Player not found"

    team_result = await db.execute(
        select(Team).where(Team.id == data.team_id, Team.user_id == user_id)
    )
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found"

    level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == data.level_id))
    level = level_result.scalar_one_or_none()
    if not level:
        return None, "Level not found"

    existing = await db.execute(
        select(PlayerTeamAssignment).where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.level_id == data.level_id,
        )
    )
    existing_assignment = existing.scalar_one_or_none()
    if existing_assignment:
        existing_team = await db.execute(select(Team).where(Team.id == existing_assignment.team_id))
        existing_team_name = existing_team.scalar_one_or_none()
        team_name = existing_team_name.name if existing_team_name else "another team"
        return None, f"Player already has a team at '{level.name}' level: '{team_name}'"

    squad_count = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == data.team_id,
            PlayerTeamAssignment.level_id == data.level_id,
        )
    )
    count = squad_count.scalar()
    if count >= MAX_SQUAD_SIZE:
        return None, f"Team squad is full (max {MAX_SQUAD_SIZE} players)"

    capacity_error = await _check_xi_capacity(db, data.team_id, data.level_id, data.role)
    if capacity_error:
        return None, capacity_error

    assignment = PlayerTeamAssignment(
        player_id=player_id,
        team_id=data.team_id,
        level_id=data.level_id,
        role=data.role,
    )
    db.add(assignment)
    await db.commit()
    return assignment, None


async def _check_xi_capacity(
    db: AsyncSession, team_id: int, level_id: int, role: str, adding: int = 1
) -> str | None:
    """Refuse to put more than 11 players in a playing XI, or ``None``.

    The squad itself stays at MAX_SQUAD_SIZE; only the "playing_11" bucket is
    capped, because that is the eleven that actually takes the field.
    """
    if role != "playing_11" or adding <= 0:
        return None
    current = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.level_id == level_id,
            PlayerTeamAssignment.role == "playing_11",
        )
    )
    if (current.scalar() or 0) + adding > PLAYING_XI_SIZE:
        return (
            f"Playing XI is limited to {PLAYING_XI_SIZE} players. "
            f"Move someone to substitutes or bench first."
        )
    return None


async def get_player_teams(db: AsyncSession, player_id: int, user_id: int):
    player = await get_player(db, player_id, user_id)
    if not player:
        return None

    result = await db.execute(
        select(PlayerTeamAssignment)
        .join(Team, Team.id == PlayerTeamAssignment.team_id)
        .where(PlayerTeamAssignment.player_id == player_id, Team.user_id == user_id)
    )
    assignments = result.scalars().all()

    teams = []
    for a in assignments:
        team_result = await db.execute(select(Team).where(Team.id == a.team_id))
        team = team_result.scalar_one_or_none()
        level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == a.level_id))
        level = level_result.scalar_one_or_none()

        teams.append(
            {
                "team_id": a.team_id,
                "team_name": team.name if team else "Unknown",
                "level_id": a.level_id,
                "level_name": level.name if level else "Unknown",
                "role": a.role,
            }
        )

    return teams


async def update_player_team_role(
    db: AsyncSession, player_id: int, team_id: int, role: str, user_id: int
):
    result = await db.execute(
        select(PlayerTeamAssignment)
        .join(Team, Team.id == PlayerTeamAssignment.team_id)
        .where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.team_id == team_id,
            Team.user_id == user_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        return None, "Assignment not found"

    if role == "playing_11" and assignment.role != "playing_11":
        team = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
        if team.scalar_one_or_none() is None:
            return None, "Team not found"
        capacity_error = await _check_xi_capacity(db, team_id, assignment.level_id, role)
        if capacity_error:
            return None, capacity_error

    assignment.role = role
    await db.commit()
    return assignment, None


async def remove_player_from_team(db: AsyncSession, player_id: int, team_id: int, user_id: int):
    result = await db.execute(
        select(PlayerTeamAssignment)
        .join(Team, Team.id == PlayerTeamAssignment.team_id)
        .where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.team_id == team_id,
            Team.user_id == user_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        return False
    await db.delete(assignment)
    await db.commit()
    return True


async def get_available_players_for_team(
    db: AsyncSession,
    team_id: int,
    user_id: int,
    q: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> tuple[list[dict] | None, str | None, int]:
    """Players this user owns who are not on a squad at this level.

    The rows carry player_code, photo and current club alongside the name, and
    mask the phone number, because a dropdown is exactly where two same-named
    players have to be told apart and where a full number does not belong.

    Returns ``(players, error, total)``; the total counts every match for this
    query, not just this page.
    """
    team_result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found", 0

    level_id = team.level_id

    assigned_subq = (
        select(PlayerTeamAssignment.player_id)
        .where(PlayerTeamAssignment.level_id == level_id)
        .distinct()
        .scalar_subquery()
    )

    where = [Player.id.notin_(assigned_subq), Player.user_id == user_id]
    if q:
        where.append(_search_predicate(q, user_id))

    total = int((await db.execute(select(func.count(Player.id)).where(*where))).scalar() or 0)

    query = (
        select(Player, Country.name, State.name, City.name)
        .join(Country, Player.country_id == Country.id)
        .join(State, Player.state_id == State.id)
        .join(City, Player.city_id == City.id)
        .where(*where)
    )

    query = query.order_by(Player.last_name, Player.first_name, Player.id).offset(skip).limit(limit)
    result = await db.execute(query)
    rows = result.all()

    players = []
    for player, country_name, state_name, city_name in rows:
        players.append(
            {
                "id": player.id,
                "player_code": player.player_code,
                "first_name": player.first_name,
                "last_name": player.last_name,
                "full_name": full_name(player.first_name, player.last_name),
                "date_of_birth": player.date_of_birth,
                "gender": player.gender,
                "batting_hand": player.batting_hand,
                "batting_position": player.batting_position,
                "bowling_type": player.bowling_type,
                "country_code": player.country_code,
                "mobile_number": mask_mobile(player.country_code, player.mobile_number),
                "country_name": country_name,
                "state_name": state_name,
                "city_name": city_name,
                "profile_image": player.profile_image,
                "team_name": None,
            }
        )

    await _attach_team_names(db, players)
    return players, None, total


async def _attach_team_names(db: AsyncSession, players: list[dict]) -> None:
    """Fill in each player's club, in one query, keyed by id.

    Two players with the same name are told apart in the picker by their code,
    photo and club; this is the club half of that.
    """
    if not players:
        return
    ids = [p["id"] for p in players]
    rows = await db.execute(
        select(PlayerTeamAssignment.player_id, Team.name)
        .join(Team, Team.id == PlayerTeamAssignment.team_id)
        .where(PlayerTeamAssignment.player_id.in_(ids))
    )
    for player_id, team_name in rows:
        for p in players:
            if p["id"] == player_id and not p.get("team_name"):
                p["team_name"] = team_name


async def assign_player_to_team_by_phone(
    db: AsyncSession,
    team_id: int,
    country_code: str,
    mobile_number: str | int,
    role: str = "playing_11",
    user_id: int | None = None,
):
    """Add a player to a team by typing their number.

    A shared number is a real case, so an ambiguous lookup is refused with the
    candidates and their codes rather than picking one: the scorer then adds the
    player by id, which is the only unambiguous handle there is.
    """
    team_result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, "Team not found"

    matches = await get_players_by_phone(db, country_code, mobile_number, user_id)
    if not matches:
        return None, "Player not found with this phone number"
    if len(matches) > 1:
        codes = ", ".join(
            f"{full_name(p.first_name, p.last_name)} ({p.player_code})" for p in matches
        )
        return None, (
            f"{len(matches)} players share this phone number: {codes}. "
            "Add the player by selecting them from the player list instead."
        )
    player = matches[0]

    level_result = await db.execute(select(TeamLevel).where(TeamLevel.id == team.level_id))
    level = level_result.scalar_one_or_none()

    existing = await db.execute(
        select(PlayerTeamAssignment).where(
            PlayerTeamAssignment.player_id == player.id,
            PlayerTeamAssignment.level_id == team.level_id,
        )
    )
    existing_assignment = existing.scalar_one_or_none()
    if existing_assignment:
        existing_team = await db.execute(select(Team).where(Team.id == existing_assignment.team_id))
        existing_team_name = existing_team.scalar_one_or_none()
        t_name = existing_team_name.name if existing_team_name else "another team"
        return None, f"Player already has a team at '{level.name}' level: '{t_name}'"

    squad_count = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.level_id == team.level_id,
        )
    )
    count = squad_count.scalar()
    if count >= MAX_SQUAD_SIZE:
        return None, f"Team squad is full (max {MAX_SQUAD_SIZE} players)"

    capacity_error = await _check_xi_capacity(db, team_id, team.level_id, role)
    if capacity_error:
        return None, capacity_error

    assignment = PlayerTeamAssignment(
        player_id=player.id,
        team_id=team_id,
        level_id=team.level_id,
        role=role,
    )
    db.add(assignment)
    await db.commit()
    return assignment, None


async def assign_players_to_team_bulk(
    db: AsyncSession,
    team_id: int,
    player_ids: list[int],
    role: str = "playing_11",
    user_id: int | None = None,
):
    """Add a whole selection of players to a squad at once.

    Two things are checked before anything is written, because a selection list is
    exactly where these go wrong:

    * the same ``player_id`` appearing twice is rejected outright - the ids are
      what identify players, and a name is not a handle, so a duplicate id is
      unambiguously the same player twice;
    * the selection may not push the playing XI past 11.
    """
    team_result = await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    team = team_result.scalar_one_or_none()
    if not team:
        return None, None, "Team not found"

    unique_ids = list(dict.fromkeys(player_ids))
    duplicated = [pid for pid in unique_ids if player_ids.count(pid) > 1]
    if duplicated:
        return (
            None,
            None,
            (
                f"The same player was selected more than once (id {duplicated[0]}). "
                "Remove the duplicate before adding the squad."
            ),
        )
    if not unique_ids:
        return None, None, "Select at least one player"

    players_result = await db.execute(
        select(Player).where(Player.id.in_(unique_ids), Player.user_id == user_id)
    )
    players = list(players_result.scalars().all())
    found_ids = {p.id for p in players}
    missing = [pid for pid in unique_ids if pid not in found_ids]
    if missing:
        return None, None, f"Player not found: id {missing[0]}"

    already = await db.execute(
        select(PlayerTeamAssignment.player_id).where(
            PlayerTeamAssignment.player_id.in_(unique_ids),
            PlayerTeamAssignment.level_id == team.level_id,
        )
    )
    on_roster = {row[0] for row in already}
    if on_roster:
        clash = next(p for p in players if p.id in on_roster)
        return (
            None,
            None,
            (
                f"{full_name(clash.first_name, clash.last_name)} ({clash.player_code}) already "
                f"has a team at '{team.level_id}' level"
            ),
        )

    squad_count = await db.execute(
        select(func.count(PlayerTeamAssignment.player_id)).where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.level_id == team.level_id,
        )
    )
    if (squad_count.scalar() or 0) + len(unique_ids) > MAX_SQUAD_SIZE:
        return None, None, f"Team squad is full (max {MAX_SQUAD_SIZE} players)"

    capacity_error = await _check_xi_capacity(db, team_id, team.level_id, role, len(unique_ids))
    if capacity_error:
        return None, None, capacity_error

    assignments = [
        PlayerTeamAssignment(player_id=pid, team_id=team_id, level_id=team.level_id, role=role)
        for pid in unique_ids
    ]
    db.add_all(assignments)
    await db.commit()
    return assignments, unique_ids, None
