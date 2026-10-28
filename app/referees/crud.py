"""Reading and writing match officials.

Two things live here. The first is the referee register: this account's
officials, searchable, filterable and pageable, each one owned by the account
that entered them. The second is the appointment book - which official stands
in which role on which match - and the officials panel a match's scorecard
shows.

Ownership is the same rule the rest of the app uses: a record belongs to the
account that created it, and another account's record is reported as missing
rather than as forbidden, so the response cannot be used to probe which ids
exist.

The appointment book is a table of its own because a match has more officials
than it has seats, and more kinds of official than it has seats for. The three
slot columns on ``matches`` stay as a compatibility mirror - the app's
original way of naming an official on a match - and the service layer keeps
them in step with the appointment rows, so the two can never disagree about
who is standing where.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import (
    OFFICIAL_ROLE_TYPES,
    ROLE_SLOTS,
    SLOT_ROLES,
    AvailabilityStatus,
    RefereeStatus,
)
from app.models import Country
from app.players.identity import full_name, mask_mobile
from app.referees.models import MatchRefereeAssignment, Referee
from app.referees.schemas import (
    MatchOfficialInfo,
    MatchOfficials,
    MatchRefereeAssignmentInfo,
    RefereeCreate,
    RefereeDeleteCheck,
    RefereeMatchInfo,
    RefereeOption,
    RefereeStats,
    RefereeUpdate,
)

# The three slots of a match's officials panel, in the order they are shown.
# Each entry is (slot, id column, name column, label).
OFFICIAL_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    ("referee_1", "referee_1_id", "referee_1_name", "Referee 1"),
    ("referee_2", "referee_2_id", "referee_2_name", "Referee 2"),
    ("match_referee", "match_referee_id", "match_referee_name", "Match Referee"),
)

# The columns the officials list may be sorted on. A whitelist rather than a
# string interpolated into an ORDER BY: the column name comes from the query
# string, and anything not listed here is not a sort the register offers.
SORT_COLUMNS: dict[str, Any] = {
    "id": Referee.id,
    "first_name": Referee.first_name,
    "last_name": Referee.last_name,
    # "name" is the whole name, which is the surname first for most people.
    "name": Referee.last_name,
    "email": Referee.email,
    "referee_type": Referee.referee_type,
    "status": Referee.status,
    "experience_years": Referee.experience_years,
    "experience_level": Referee.experience_level,
    "certification_level": Referee.certification_level,
    "joining_date": Referee.joining_date,
    "created_at": Referee.created_at,
    "updated_at": Referee.updated_at,
}

DEFAULT_SORT = "id"

# The matches an official holds, split by whether the match has been played.
# A match that is still on the calendar can be unlinked; one that has been
# played cannot, because the scorecard names the official who stood in it.
_UPCOMING_MATCH_STATUSES = ("scheduled",)


class DuplicateRefereeEmailError(ValueError):
    """Raised when the address is already on this account's register.

    The email is unique within an account and not across accounts: two clubs
    may both file a fixture desk at the same shared address, but one club may
    not register the same official twice under one address.
    """

    def __init__(self, email: str, existing_id: int | None = None) -> None:
        super().__init__(f"An official with the email address {email} is already on the register.")
        self.email = email
        self.existing_id = existing_id


class RefereeAssignmentError(ValueError):
    """Raised when an appointment is refused.

    ``status_code`` is the answer the request should get: 404 when the
    official or the match is not there, 400 when the role is one the
    official's type cannot fill, and 409 when the appointment conflicts with
    the state of the register or the match.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _name_filter(q: str | None):
    """A case-insensitive "contains" over the fields a search would look at.

    Name parts, the permanent code and the email address: the three things a
    person typing into the search box knows about the official they want.
    """
    if not q:
        return None
    pattern = f"%{q.strip().lower()}%"
    return or_(
        func.lower(Referee.first_name).like(pattern),
        func.lower(Referee.last_name).like(pattern),
        func.lower(Referee.referee_code).like(pattern),
        func.lower(Referee.email).like(pattern),
    )


def _nationality_filter(nationality: str | None):
    """Filter on the country the official is from.

    The register offers a country dropdown, so the value is normally an id.
    A name is accepted as well - the reference data is called by name in half
    the places it is used, and a search box that refuses "India" because it
    wanted 12 is a nuisance rather than a safety feature.
    """
    if not nationality:
        return None
    text = str(nationality).strip()
    if not text:
        return None
    if text.isdigit():
        return Referee.country_id == int(text)
    pattern = f"%{text.lower()}%"
    matching = select(Country.id).where(func.lower(Country.name).like(pattern))
    return Referee.country_id.in_(matching)


def _order_by(sort_by: str | None, sort_order: str | None):
    """The ORDER BY for the officials list, from a whitelist.

    Unknown column names fall back to the id, and an unknown direction falls
    back to ascending: a client that asks for a sort the register does not
    have gets a sensible list rather than an error.
    """
    column = SORT_COLUMNS.get((sort_by or "").strip().lower(), SORT_COLUMNS[DEFAULT_SORT])
    descending = (sort_order or "").strip().lower() in {"desc", "descending", "-1"}
    direction = column.desc() if descending else column.asc()
    # A stable order: two officials with the same name still come back in the
    # same order every time, which is what a paginated list needs.
    return [direction, Referee.first_name.asc(), Referee.id.asc()]


async def get_referees(
    db: AsyncSession,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    q: str | None = None,
    referee_type: str | None = None,
    nationality: str | None = None,
    status: str | None = None,
    experience_min: int | None = None,
    experience_max: int | None = None,
    certification_level: str | None = None,
    sort_by: str | None = DEFAULT_SORT,
    sort_order: str | None = "asc",
) -> tuple[list[Referee], int]:
    """One page of this account's referees, and the total that page is drawn from."""
    where = [Referee.user_id == user_id]

    name_filter = _name_filter(q)
    if name_filter is not None:
        where.append(name_filter)
    if referee_type:
        where.append(Referee.referee_type == referee_type)
    nationality_filter = _nationality_filter(nationality)
    if nationality_filter is not None:
        where.append(nationality_filter)
    if status:
        where.append(Referee.status == status)
    if experience_min is not None:
        where.append(Referee.experience_years >= experience_min)
    if experience_max is not None:
        where.append(Referee.experience_years <= experience_max)
    if certification_level:
        where.append(Referee.certification_level == certification_level)

    total = int((await db.execute(select(func.count(Referee.id)).where(*where))).scalar() or 0)

    result = await db.execute(
        select(Referee)
        .options(
            selectinload(Referee.country),
            selectinload(Referee.created_by),
            selectinload(Referee.updated_by),
        )
        .where(*where)
        .order_by(*_order_by(sort_by, sort_order))
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_referee(db: AsyncSession, referee_id: int, user_id: int) -> Referee | None:
    """One referee of this account, with its nationality loaded."""
    result = await db.execute(
        select(Referee)
        .options(
            selectinload(Referee.country),
            selectinload(Referee.created_by),
            selectinload(Referee.updated_by),
        )
        .where(Referee.id == referee_id, Referee.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_referee_by_code(db: AsyncSession, referee_code: str, user_id: int) -> Referee | None:
    """Look an official up by the permanent code printed on the scorecard."""
    result = await db.execute(
        select(Referee)
        .options(selectinload(Referee.country))
        .where(Referee.referee_code == referee_code, Referee.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_referee_options(
    db: AsyncSession,
    user_id: int,
    q: str | None = None,
    referee_type: str | None = None,
    status: str | None = None,
) -> list[Referee]:
    """The referee picker: this account's officials, unpageinated and light."""
    where = [Referee.user_id == user_id]
    name_filter = _name_filter(q)
    if name_filter is not None:
        where.append(name_filter)
    if referee_type:
        where.append(Referee.referee_type == referee_type)
    if status:
        where.append(Referee.status == status)

    result = await db.execute(
        select(Referee).where(*where).order_by(Referee.first_name, Referee.last_name)
    )
    return list(result.scalars().all())


def referee_option(referee: Referee) -> RefereeOption:
    return RefereeOption(
        id=referee.id,
        referee_code=referee.referee_code,
        full_name=full_name(referee.first_name, referee.last_name),
        profile_image=referee.profile_image,
        referee_type=referee.referee_type,
        experience_years=referee.experience_years or 0,
        status=referee.status or RefereeStatus.ACTIVE.value,
        availability_status=referee.availability_status or AvailabilityStatus.AVAILABLE.value,
    )


async def get_referee_stats(db: AsyncSession, user_id: int) -> RefereeStats:
    """The counts the register's header shows.

    One grouped query per dimension rather than a scan of the whole table in
    Python: the numbers are read on every load of the page.
    """
    status_rows = (
        await db.execute(
            select(Referee.status, func.count(Referee.id))
            .where(Referee.user_id == user_id)
            .group_by(Referee.status)
        )
    ).all()
    by_status = {status: int(count) for status, count in status_rows}

    availability_rows = (
        await db.execute(
            select(Referee.availability_status, func.count(Referee.id))
            .where(Referee.user_id == user_id)
            .group_by(Referee.availability_status)
        )
    ).all()
    by_availability = {status: int(count) for status, count in availability_rows}

    # An official who has been appointed to at least one match is "assigned".
    assigned = int(
        (
            await db.execute(
                select(func.count(func.distinct(MatchRefereeAssignment.referee_id)))
                .join(Referee, Referee.id == MatchRefereeAssignment.referee_id)
                .where(Referee.user_id == user_id)
            )
        ).scalar()
        or 0
    )

    total = sum(by_status.values())
    return RefereeStats(
        total=total,
        active=by_status.get(RefereeStatus.ACTIVE.value, 0),
        inactive=by_status.get(RefereeStatus.INACTIVE.value, 0),
        suspended=by_status.get(RefereeStatus.SUSPENDED.value, 0),
        available=by_availability.get(AvailabilityStatus.AVAILABLE.value, 0),
        unavailable=by_availability.get(AvailabilityStatus.UNAVAILABLE.value, 0),
        assigned=assigned,
    )


async def email_taken(
    db: AsyncSession, email: str, user_id: int, exclude_referee_id: int | None = None
) -> bool:
    """Whether this account already has an official on this address.

    Case-insensitive, and scoped to the account: the same address may be filed
    by two clubs without either of them being wrong.
    """
    needle = (email or "").strip().lower()
    if not needle:
        return False
    query = select(Referee.id).where(
        Referee.user_id == user_id,
        func.lower(Referee.email) == needle,
    )
    if exclude_referee_id is not None:
        query = query.where(Referee.id != exclude_referee_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none() is not None


async def create_referee(db: AsyncSession, data: RefereeCreate, user_id: int) -> Referee:
    """Register an official for this account.

    The identity fields (code, canonical phone key, experience band) are
    derived by the model itself, so a referee created here, by a seed script
    or by a test all end up with the same invariants.
    """
    if await email_taken(db, data.email, user_id):
        raise DuplicateRefereeEmailError(data.email)

    referee = Referee(**data.model_dump(), user_id=user_id)
    db.add(referee)
    await db.commit()
    await db.refresh(referee)
    # The country is loaded here rather than left to a lazy load: the response
    # reads the nationality from it, and a lazy load outside the session's
    # greenlet context is an error, not a query.
    return await get_referee(db, referee.id, user_id)


# The columns a row cannot do without. A PATCH that sends one of them as null
# is asking to clear a field that has to be there, and the database would
# refuse the write with an error the client cannot act on. The field is left
# as it is instead, so a partial update that names an optional field it does
# not want to change does not destroy the record.
_REQUIRED_FIELDS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "country_id",
    "referee_type",
    "email",
    "experience_years",
    "status",
    "availability_status",
)


async def update_referee(
    db: AsyncSession, referee_id: int, data: RefereeUpdate, user_id: int
) -> Referee | None:
    """Apply a partial update to one referee of this account."""
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None
    payload = data.model_dump(exclude_unset=True)
    for field in _REQUIRED_FIELDS:
        if field in payload and payload[field] is None:
            payload.pop(field)
    if payload.get("email") and await email_taken(
        db, payload["email"], user_id, exclude_referee_id=referee_id
    ):
        raise DuplicateRefereeEmailError(payload["email"], existing_id=referee_id)
    for key, val in payload.items():
        setattr(referee, key, val)
    await db.commit()
    await db.refresh(referee)
    return await get_referee(db, referee_id, user_id)


async def replace_referee(
    db: AsyncSession, referee_id: int, data: RefereeCreate, user_id: int
) -> Referee | None:
    """Replace every mutable field of one referee of this account."""
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None
    if await email_taken(db, data.email, user_id, exclude_referee_id=referee_id):
        raise DuplicateRefereeEmailError(data.email, existing_id=referee_id)
    for key, val in data.model_dump().items():
        setattr(referee, key, val)
    await db.commit()
    await db.refresh(referee)
    return await get_referee(db, referee_id, user_id)


async def get_referee_delete_check(
    db: AsyncSession, referee_id: int, user_id: int
) -> RefereeDeleteCheck | None:
    """What deleting an official would do.

    Answered before the delete is attempted, so the confirmation dialog can say
    what is going to happen instead of discovering it afterwards.
    """
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None

    from app.matches.models import Match

    # Every match that names this official, whether through an appointment row
    # or through one of the match's own slot columns. A match named before the
    # appointment book existed still counts: the scorecard names the official
    # either way.
    rows = (
        await db.execute(
            select(Match.id, Match.status)
            .where(
                (Match.referee_1_id == referee_id)
                | (Match.referee_2_id == referee_id)
                | (Match.match_referee_id == referee_id)
                | Match.id.in_(
                    select(MatchRefereeAssignment.match_id).where(
                        MatchRefereeAssignment.referee_id == referee_id
                    )
                )
            )
            .distinct()
        )
    ).all()

    upcoming = sum(1 for _match_id, status in rows if status in _UPCOMING_MATCH_STATUSES)
    historical = len(rows) - upcoming

    total = upcoming + historical
    can_delete = total == 0
    reason = None
    if not can_delete:
        reason = (
            f"This official is named on {total} match"
            f"{'es' if total != 1 else ''} and cannot be deleted without corrupting "
            "the scorecard."
        )
    return RefereeDeleteCheck(
        referee_id=referee_id,
        can_delete=can_delete,
        upcoming_matches=upcoming,
        historical_matches=historical,
        reason=reason,
    )


async def delete_referee(db: AsyncSession, referee_id: int, user_id: int) -> bool | None:
    """Delete an official, unless a match names them.

    Returns ``None`` when the referee does not belong to this account and
    ``False`` when they are named on a match, so the caller can answer 404 and
    409 respectively. A referee who has officiated is part of recorded history:
    the scorecard names them, and deleting the row would leave a match pointing
    at an official who no longer exists. The match is the thing to unlink first.
    """
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None

    from app.matches.models import Match

    referenced = (
        await db.execute(
            select(func.count(Match.id)).where(
                (Match.referee_1_id == referee_id)
                | (Match.referee_2_id == referee_id)
                | (Match.match_referee_id == referee_id)
            )
        )
    ).scalar()
    if referenced:
        return False

    appointments = (
        await db.execute(
            select(func.count(MatchRefereeAssignment.id)).where(
                MatchRefereeAssignment.referee_id == referee_id
            )
        )
    ).scalar()
    if appointments:
        return False

    await db.delete(referee)
    await db.commit()
    return True


async def update_referee_profile_image(
    db: AsyncSession, referee_id: int, profile_image: str, user_id: int
) -> Referee | None:
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None
    referee.profile_image = profile_image
    await db.commit()
    await db.refresh(referee)
    return referee


async def clear_referee_profile_image(
    db: AsyncSession, referee_id: int, user_id: int
) -> Referee | None:
    """Remove the photo from an official's record."""
    referee = await get_referee(db, referee_id, user_id)
    if not referee:
        return None
    referee.profile_image = None
    await db.commit()
    await db.refresh(referee)
    return referee


# --- linking officials to a match -------------------------------------------


def role_accepts_type(role: str, referee_type: str) -> bool:
    """Whether an official of this type may stand in this role.

    The roles are not interchangeable, and the point of recording the role of
    an official is precisely so that the panel can be read: a first umpire
    who has umpired for a decade is not a match referee, and putting one in
    the match referee's seat would put a name in a box that means something
    else.
    """
    accepted = OFFICIAL_ROLE_TYPES.get(role)
    if not accepted:
        return False
    return referee_type in accepted


async def resolve_match_referees(
    db: AsyncSession, referee_ids: dict[str, int | None], user_id: int
) -> tuple[dict[str, Referee], list[str]]:
    """Check the officials a match names and return them by slot.

    Two rules, both reported as errors the caller can show:

    * an official must exist and belong to this account - a match can only be
      officiated by officials this account has on file;
    * the same person cannot stand in two slots of one match. A match with the
      first umpire also listed as the match referee is a mistake in the data,
      and it is caught here rather than left to be noticed on a scorecard.
    """
    wanted = {slot: rid for slot, rid in referee_ids.items() if rid is not None}
    errors: list[str] = []
    if not wanted:
        return {}, errors

    ids = list(wanted.values())
    result = await db.execute(
        select(Referee)
        .options(selectinload(Referee.country))
        .where(Referee.id.in_(ids), Referee.user_id == user_id)
    )
    found = {r.id: r for r in result.scalars().all()}

    resolved: dict[str, Referee] = {}
    for slot, rid in wanted.items():
        referee = found.get(rid)
        if referee is None:
            errors.append(f"Referee {rid} not found")
            continue
        resolved[slot] = referee

    seen: dict[int, str] = {}
    for slot, referee in resolved.items():
        previous = seen.get(referee.id)
        if previous is not None:
            errors.append(
                f"The same official cannot fill two slots of a match ({previous} and {slot})"
            )
            continue
        seen[referee.id] = slot

    return resolved, errors


def _slot_columns(slot: str) -> tuple[str, str]:
    """The (id column, name column) of a panel slot."""
    for candidate, id_column, name_column, _ in OFFICIAL_SLOTS:
        if candidate == slot:
            return id_column, name_column
    return "", ""


def _mirror_slot(match, slot: str, referee: Referee | None) -> None:
    """Write an official into a panel slot's mirror columns."""
    id_column, name_column = _slot_columns(slot)
    if not id_column:
        return
    setattr(match, id_column, referee.id if referee is not None else None)
    setattr(
        match,
        name_column,
        full_name(referee.first_name, referee.last_name) if referee is not None else None,
    )


async def sync_match_assignments(
    db: AsyncSession,
    match,
    resolved: dict[str, Referee],
    *,
    user_id: int,
) -> None:
    """Keep the appointment rows in step with the match's three slots.

    The slot columns on a match are the app's original way of naming an
    official; the appointment rows are the real book. Every write to a match's
    slots is mirrored into the book here, and the reverse is true when an
    appointment is made or withdrawn through the officials endpoint, so the two
    never drift apart.

    Appointments in roles the panel has no seat for - the TV umpire, the
    fourth umpire, the reserve - are left alone: the slots say nothing about
    them either way.
    """
    existing = list(
        (
            await db.execute(
                select(MatchRefereeAssignment).where(MatchRefereeAssignment.match_id == match.id)
            )
        )
        .scalars()
        .all()
    )
    by_role = {assignment.role: assignment for assignment in existing}

    for slot, _, _, _ in OFFICIAL_SLOTS:
        role = SLOT_ROLES[slot]
        referee = resolved.get(slot)
        assignment = by_role.get(role)
        if referee is None:
            if assignment is not None:
                await db.delete(assignment)
            continue
        if assignment is None:
            db.add(
                MatchRefereeAssignment(
                    match_id=match.id,
                    referee_id=referee.id,
                    role=role,
                    assigned_by_id=user_id,
                )
            )
        elif assignment.referee_id != referee.id:
            assignment.referee_id = referee.id


async def get_match_assignments(
    db: AsyncSession, match_ids: list[int]
) -> dict[int, list[MatchRefereeAssignment]]:
    """Every appointment on the given matches, officials included.

    One query for a whole page of matches, so a list of matches does not turn
    into three queries per match.
    """
    if not match_ids:
        return {}
    result = await db.execute(
        select(MatchRefereeAssignment)
        .options(selectinload(MatchRefereeAssignment.referee))
        .where(MatchRefereeAssignment.match_id.in_(match_ids))
        .order_by(MatchRefereeAssignment.id)
    )
    grouped: dict[int, list[MatchRefereeAssignment]] = {mid: [] for mid in match_ids}
    for assignment in result.scalars().all():
        grouped.setdefault(assignment.match_id, []).append(assignment)
    return grouped


async def _schedule_conflicts(db: AsyncSession, match, referee_id: int) -> list[str]:
    """Warn when the official is already booked around the same time.

    A warning rather than a refusal: an official can legitimately be double
    booked by an accident, and the fix is a phone call, not a rejection that
    leaves the fixture unstaffed.
    """
    from app.matches.models import Match

    rows = (
        await db.execute(
            select(Match.match_date, Match.match_time, Match.venue, Match.id)
            .join(MatchRefereeAssignment, MatchRefereeAssignment.match_id == Match.id)
            .where(
                MatchRefereeAssignment.referee_id == referee_id,
                Match.id != match.id,
                Match.match_date == match.match_date,
            )
        )
    ).all()

    warnings: list[str] = []
    for _match_date, match_time, venue, match_id in rows:
        if str(match_time) == str(match.match_time):
            warnings.append(
                f"This official is already appointed to match {match_id} at "
                f"{match_time} on the same day ({venue})."
            )
        else:
            warnings.append(
                f"This official is already appointed to match {match_id} on the same day ({venue})."
            )
    return warnings


async def assign_referee_to_match(
    db: AsyncSession,
    match,
    referee_id: int,
    role: str,
    user_id: int,
) -> tuple[MatchRefereeAssignment, list[str]]:
    """Appoint an official to a match in a role.

    Returns the appointment and the warnings worth saying out loud. Raises
    :class:`RefereeAssignmentError` when the appointment is refused. The
    rules, in the order they are checked:

    * the official must be on this account's register;
    * the role must be one their own type can fill;
    * an official who is not active may not be appointed - a suspended
      official stays on the matches they have already officiated, but is not
      put on new ones;
    * a match has one official per role, and one role per official.
    """
    if match is None or match.user_id != user_id:
        raise RefereeAssignmentError("Match not found", status_code=404)

    result = await db.execute(
        select(Referee)
        .options(selectinload(Referee.country))
        .where(Referee.id == referee_id, Referee.user_id == user_id)
    )
    referee = result.scalar_one_or_none()
    if referee is None:
        raise RefereeAssignmentError(f"Referee {referee_id} not found", status_code=404)

    if not role_accepts_type(role, referee.referee_type):
        accepted = ", ".join(OFFICIAL_ROLE_TYPES.get(role, ()))
        raise RefereeAssignmentError(
            f"A {referee.referee_type.replace('_', ' ')} cannot be appointed as "
            f"{role.replace('_', ' ')}. That role is filled by: {accepted}.",
            status_code=400,
        )

    if referee.status != RefereeStatus.ACTIVE.value:
        raise RefereeAssignmentError(
            f"This official is {referee.status} and cannot be appointed to a match.",
            status_code=409,
        )

    existing = list(
        (
            await db.execute(
                select(MatchRefereeAssignment).where(MatchRefereeAssignment.match_id == match.id)
            )
        )
        .scalars()
        .all()
    )
    for assignment in existing:
        if assignment.role == role:
            raise RefereeAssignmentError(
                f"This match already has an official in the {role} role.", status_code=409
            )
        if assignment.referee_id == referee_id:
            raise RefereeAssignmentError(
                "This official is already appointed to this match.", status_code=409
            )

    warnings: list[str] = []
    if referee.availability_status == AvailabilityStatus.UNAVAILABLE.value:
        warnings.append("This official is marked unavailable and may need to be asked first.")
    if referee.certification_expiry_date is not None:
        warnings.append(
            "This official's certification has lapsed or lapses soon; check it before "
            "the appointment goes out."
        )
    warnings.extend(await _schedule_conflicts(db, match, referee_id))

    assignment = MatchRefereeAssignment(
        match_id=match.id,
        referee_id=referee.id,
        role=role,
        assigned_by_id=user_id,
    )
    db.add(assignment)

    # A role the panel has a seat for also fills that seat, so the scorecard
    # and the appointment book say the same thing.
    slot = ROLE_SLOTS.get(role)
    if slot:
        _mirror_slot(match, slot, referee)

    await db.commit()
    await db.refresh(assignment)
    await db.refresh(match)
    return assignment, warnings


async def unassign_referee_from_match(
    db: AsyncSession, match, assignment_id: int, user_id: int
) -> bool:
    """Withdraw an appointment, and empty the seat it held.

    The appointment row goes; the match stays. An official who is taken off a
    fixture does not take the fixture with them, and nothing that has already
    been played is touched.
    """
    if match is None or match.user_id != user_id:
        return False

    result = await db.execute(
        select(MatchRefereeAssignment).where(
            MatchRefereeAssignment.id == assignment_id,
            MatchRefereeAssignment.match_id == match.id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        return False

    slot = ROLE_SLOTS.get(assignment.role)
    if slot:
        _mirror_slot(match, slot, None)

    await db.delete(assignment)
    await db.commit()
    await db.refresh(match)
    return True


async def get_referee_matches(
    db: AsyncSession, referee_id: int, user_id: int
) -> list[RefereeMatchInfo]:
    """The matches an official has been appointed to."""
    from app.matches.models import Match

    assignments = list(
        (
            await db.execute(
                select(MatchRefereeAssignment)
                .where(MatchRefereeAssignment.referee_id == referee_id)
                .order_by(MatchRefereeAssignment.id)
            )
        )
        .scalars()
        .all()
    )
    if not assignments:
        return []

    match_ids = [assignment.match_id for assignment in assignments]
    result = await db.execute(
        select(Match)
        .options(selectinload(Match.team_a), selectinload(Match.team_b))
        .where(Match.id.in_(match_ids), Match.user_id == user_id)
    )
    matches = {match.id: match for match in result.scalars().all()}

    infos: list[RefereeMatchInfo] = []
    for assignment in assignments:
        match = matches.get(assignment.match_id)
        if match is None:
            continue
        infos.append(
            RefereeMatchInfo(
                assignment_id=assignment.id,
                match_id=match.id,
                role=assignment.role,
                match_date=match.match_date,
                match_time=match.match_time,
                venue=match.venue,
                match_type=match.match_type,
                status=match.status,
                team_a=match.team_a.name if match.team_a else None,
                team_b=match.team_b.name if match.team_b else None,
                team_a_id=match.team_a_id,
                team_b_id=match.team_b_id,
                assigned_at=assignment.assigned_at,
            )
        )
    infos.sort(key=lambda info: (info.match_date, info.match_time, info.match_id))
    return infos


def _official_info(
    slot: str,
    label: str,
    referee: Referee | None,
    fallback_name: str | None,
    *,
    reveal_contact: bool,
) -> MatchOfficialInfo:
    """Build one slot of the officials panel.

    ``fallback_name`` is the free-text name the match carries. It is used when
    no official is linked, and it is also what the slot shows when there is one
    - the linked record is the source of truth for the details, but the name on
    the slot stays the name of the match.
    """
    if referee is None:
        return MatchOfficialInfo(
            slot=slot,
            label=label,
            name=fallback_name,
        )

    name = full_name(referee.first_name, referee.last_name)
    return MatchOfficialInfo(
        slot=slot,
        label=label,
        name=name or fallback_name,
        referee_id=referee.id,
        referee_code=referee.referee_code,
        first_name=referee.first_name,
        last_name=referee.last_name,
        full_name=name,
        date_of_birth=referee.date_of_birth,
        nationality=referee.country.name if referee.country else None,
        profile_image=referee.profile_image,
        referee_type=referee.referee_type,
        experience_years=referee.experience_years,
        # The number is masked unless the caller may see it in full. The
        # officials panel is read by the account that owns the match, so in
        # practice this is the owner or an admin; a scorer browsing a list
        # still gets a masked number.
        mobile_number=(
            referee.mobile_number
            if reveal_contact
            else mask_mobile(referee.country_code, referee.mobile_number)
        ),
        email=referee.email,
        status=referee.status,
        availability_status=referee.availability_status,
    )


def assignment_info(assignment: MatchRefereeAssignment) -> MatchRefereeAssignmentInfo:
    """One row of a match's appointment list."""
    referee = assignment.referee
    return MatchRefereeAssignmentInfo(
        id=assignment.id,
        match_id=assignment.match_id,
        role=assignment.role,
        referee_id=assignment.referee_id,
        referee_code=referee.referee_code if referee else None,
        full_name=full_name(referee.first_name, referee.last_name) if referee else None,
        profile_image=referee.profile_image if referee else None,
        referee_type=referee.referee_type if referee else None,
        status=referee.status if referee else None,
        assigned_at=assignment.assigned_at,
    )


async def build_match_officials(
    db: AsyncSession,
    matches: list,
    *,
    reveal_contact: bool,
) -> list[MatchOfficials]:
    """Build the officials panel for each of the given matches.

    The appointment book is the source of truth. The slot columns on the match
    are consulted as well, so a match whose officials were named before the
    book existed still shows them.
    """
    if not matches:
        return []

    match_ids = [match.id for match in matches]
    assignments_by_match = await get_match_assignments(db, match_ids)

    # Every official the panels refer to, in one query.
    wanted_ids: set[int] = set()
    for match in matches:
        for _, id_column, _, _ in OFFICIAL_SLOTS:
            referee_id = getattr(match, id_column, None)
            if referee_id is not None:
                wanted_ids.add(referee_id)
        for assignment in assignments_by_match.get(match.id, []):
            wanted_ids.add(assignment.referee_id)

    referees: dict[int, Referee] = {}
    if wanted_ids:
        result = await db.execute(
            select(Referee).options(selectinload(Referee.country)).where(Referee.id.in_(wanted_ids))
        )
        referees = {r.id: r for r in result.scalars().all()}

    panels: list[MatchOfficials] = []
    for match in matches:
        assignments = assignments_by_match.get(match.id, [])
        by_role = {assignment.role: assignment for assignment in assignments}

        slots = {}
        for slot, id_column, name_column, label in OFFICIAL_SLOTS:
            role = SLOT_ROLES[slot]
            assignment = by_role.get(role)
            referee = referees.get(assignment.referee_id) if assignment else None
            if referee is None:
                # No appointment for this seat: fall back to the slot column,
                # which is how a match names an official in plain text.
                referee_id = getattr(match, id_column, None)
                referee = referees.get(referee_id) if referee_id is not None else None
            slots[slot] = _official_info(
                slot,
                label,
                referee,
                getattr(match, name_column, None),
                reveal_contact=reveal_contact,
            )
        panels.append(
            MatchOfficials(
                referee_1=slots["referee_1"],
                referee_2=slots["referee_2"],
                match_referee=slots["match_referee"],
                assignments=[assignment_info(a) for a in assignments],
            )
        )
    return panels
