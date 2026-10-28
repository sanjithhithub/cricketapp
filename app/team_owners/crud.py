"""Reading and writing the owners register, and the stakes they hold.

Two things live here. The first is the register: this account's owners,
searchable, filterable and pageable, each one owned by the account that
entered them. The second is the stake book: which owner holds which team, on
what terms, and whether that stake is still live.

Ownership is the same rule the rest of the app uses: a record belongs to the
account that created it, and another account's record is reported as missing
rather than as forbidden, so the response cannot be used to probe which ids
exist.

The rules the stake book enforces, and the order they are checked in:

* the team and the owner must both belong to the account;
* a person cannot hold the same team twice at the same time - the live stakes
  are unique on (team, owner), and the database says so as well;
* the live percentages of a team add up to no more than a hundred. They are
  not required to add up to a hundred: a club that does not split its equity
  does not have to pretend to;
* ending a stake is not deleting it. The row stays, with an end date and an
  inactive status, because "this club was owned by that family until 2019" is
  a fact about the club's past.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import OwnershipStatus, OwnershipType
from app.models import Country
from app.players.identity import full_name, mask_mobile
from app.team_owners.models import TeamOwner, TeamOwnership
from app.team_owners.schemas import (
    TeamOwnerCreate,
    TeamOwnerDeleteCheck,
    TeamOwnerOption,
    TeamOwnerResponse,
    TeamOwnershipInput,
    TeamOwnershipResponse,
    TeamOwnerStats,
    TeamOwnerTeamSummary,
    TeamOwnerUpdate,
)

# The columns the owners list may be sorted on. A whitelist rather than a
# string interpolated into an ORDER BY: the column name comes from the query
# string, and anything not listed here is not a sort the register offers.
SORT_COLUMNS: dict[str, Any] = {
    "id": TeamOwner.id,
    "first_name": TeamOwner.first_name,
    "last_name": TeamOwner.last_name,
    # "name" is the whole name, which is the surname first for most people.
    "name": TeamOwner.last_name,
    "email": TeamOwner.email,
    "ownership_type": TeamOwner.ownership_type,
    "ownership_status": TeamOwner.ownership_status,
    "created_at": TeamOwner.created_at,
    "updated_at": TeamOwner.updated_at,
}

DEFAULT_SORT = "id"


class DuplicateOwnerEmailError(ValueError):
    """Raised when the address is already on this account's register.

    The email is unique within an account and not across accounts: two clubs
    may both file a fixture desk at the same shared address, but one club may
    not register the same owner twice under one address.
    """

    def __init__(self, email: str, existing_id: int | None = None) -> None:
        super().__init__(f"A team owner with the email address {email} is already on the register.")
        self.email = email
        self.existing_id = existing_id


class OwnerProfileExistsError(ValueError):
    """Raised when the account already has an owner profile of its own.

    The self-service form fills in one profile and links it to the signed-in
    account. A second one would give the same person two records, so the first
    is kept and the request is answered with 409.
    """

    def __init__(self, owner_id: int) -> None:
        super().__init__("This account already has a team owner profile.")
        self.owner_id = owner_id


class DuplicateActiveOwnershipError(ValueError):
    """Raised when the owner already holds this team and the stake is live.

    The stake that is already there is a live claim on the team. A second live
    claim by the same person is a mistake in the data, and it is refused here
    rather than left to be noticed twice over on the team panel.
    """

    def __init__(self, team_id: int, owner_id: int) -> None:
        super().__init__(
            "This owner already holds this team. End the existing ownership before "
            "taking the team on again."
        )
        self.team_id = team_id
        self.owner_id = owner_id


class OwnershipPercentageError(ValueError):
    """Raised when the live stakes of a team would add up to more than a hundred.

    ``status_code`` is the answer the request should get: 409, because the
    request is well formed but conflicts with the state the team is already in.
    """

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class OwnershipError(ValueError):
    """Raised when an ownership is refused.

    ``status_code`` is the answer the request should get: 404 when the team or
    the owner is not there, 400 when the request contradicts itself, and 409
    when it conflicts with what the team already has.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _search_filter(q: str | None):
    """A case-insensitive "contains" over the fields a search would look at.

    Name parts, the whole name, the email address and the phone number: the
    four things a person typing into the search box knows about the owner they
    want.
    """
    if not q:
        return None
    pattern = f"%{q.strip().lower()}%"
    return or_(
        func.lower(TeamOwner.first_name).like(pattern),
        func.lower(TeamOwner.last_name).like(pattern),
        func.lower(TeamOwner.email).like(pattern),
        func.lower(TeamOwner.mobile_number).like(pattern),
        func.lower(TeamOwner.organization_name).like(pattern),
    )


def _nationality_filter(nationality: str | None):
    """Filter on the country the owner is from.

    The register offers a country dropdown, so the value is normally an id. A
    name is accepted as well - the reference data is called by name in half
    the places it is used, and a search box that refuses "India" because it
    wanted 12 is a nuisance rather than a safety feature.
    """
    if not nationality:
        return None
    text = str(nationality).strip()
    if not text:
        return None
    if text.isdigit():
        return TeamOwner.country_id == int(text)
    pattern = f"%{text.lower()}%"
    matching = select(Country.id).where(func.lower(Country.name).like(pattern))
    return TeamOwner.country_id.in_(matching)


def _order_by(sort_by: str | None, sort_order: str | None):
    """The ORDER BY for the owners list, from a whitelist.

    Unknown column names fall back to the id, and an unknown direction falls
    back to ascending: a client that asks for a sort the register does not
    have gets a sensible list rather than an error.
    """
    column = SORT_COLUMNS.get((sort_by or "").strip().lower(), SORT_COLUMNS[DEFAULT_SORT])
    descending = (sort_order or "").strip().lower() in {"desc", "descending", "-1"}
    direction = column.desc() if descending else column.asc()
    # A stable order: two owners with the same name still come back in the
    # same order every time, which is what a paginated list needs.
    return [direction, TeamOwner.first_name.asc(), TeamOwner.id.asc()]


async def get_team_owners(
    db: AsyncSession,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    q: str | None = None,
    team_id: int | None = None,
    ownership_type: str | None = None,
    ownership_status: str | None = None,
    nationality: str | None = None,
    gender: str | None = None,
    sort_by: str | None = DEFAULT_SORT,
    sort_order: str | None = "asc",
) -> tuple[list[TeamOwner], int]:
    """One page of this account's owners, and the total that page is drawn from."""
    where = [TeamOwner.user_id == user_id]

    search_filter = _search_filter(q)
    if search_filter is not None:
        where.append(search_filter)
    if ownership_type:
        where.append(TeamOwner.ownership_type == ownership_type)
    if ownership_status:
        where.append(TeamOwner.ownership_status == ownership_status)
    if gender:
        where.append(TeamOwner.gender == gender)
    nationality_filter = _nationality_filter(nationality)
    if nationality_filter is not None:
        where.append(nationality_filter)
    if team_id is not None:
        # Any stake at all, live or historical: an owner who has handed a team
        # over is still an owner of it, and the register should be able to say
        # so.
        where.append(
            TeamOwner.id.in_(select(TeamOwnership.owner_id).where(TeamOwnership.team_id == team_id))
        )

    total = int((await db.execute(select(func.count(TeamOwner.id)).where(*where))).scalar() or 0)

    result = await db.execute(
        select(TeamOwner)
        .options(
            selectinload(TeamOwner.country),
            selectinload(TeamOwner.created_by),
            selectinload(TeamOwner.updated_by),
            selectinload(TeamOwner.ownerships).selectinload(TeamOwnership.team),
        )
        .where(*where)
        .order_by(*_order_by(sort_by, sort_order))
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_team_owner(db: AsyncSession, owner_id: int, user_id: int) -> TeamOwner | None:
    """One owner of this account, with its nationality and its stakes loaded."""
    result = await db.execute(
        select(TeamOwner)
        .options(
            selectinload(TeamOwner.country),
            selectinload(TeamOwner.created_by),
            selectinload(TeamOwner.updated_by),
            selectinload(TeamOwner.ownerships).selectinload(TeamOwnership.team),
        )
        .where(TeamOwner.id == owner_id, TeamOwner.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_team_owner_by_linked_user(db: AsyncSession, linked_user_id: int) -> TeamOwner | None:
    """The owner record a signed-in user has completed for themselves.

    This is the link that lets a person who signed in with Google finish their
    own owner profile: the row is theirs, so they may read and change it, and
    nobody else may.
    """
    result = await db.execute(
        select(TeamOwner)
        .options(
            selectinload(TeamOwner.country),
            selectinload(TeamOwner.ownerships).selectinload(TeamOwnership.team),
        )
        .where(TeamOwner.linked_user_id == linked_user_id)
        .order_by(TeamOwner.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_team_owner_options(
    db: AsyncSession,
    user_id: int,
    q: str | None = None,
    ownership_status: str | None = None,
) -> list[TeamOwner]:
    """The owner picker: this account's owners, unpageinated and light."""
    where = [TeamOwner.user_id == user_id]
    search_filter = _search_filter(q)
    if search_filter is not None:
        where.append(search_filter)
    if ownership_status:
        where.append(TeamOwner.ownership_status == ownership_status)

    result = await db.execute(
        select(TeamOwner)
        .options(selectinload(TeamOwner.country))
        .where(*where)
        .order_by(TeamOwner.first_name, TeamOwner.last_name)
    )
    return list(result.scalars().all())


def team_owner_option(owner: TeamOwner) -> TeamOwnerOption:
    return TeamOwnerOption(
        id=owner.id,
        full_name=full_name(owner.first_name, owner.last_name),
        profile_image=owner.profile_image,
        ownership_type=owner.ownership_type,
        ownership_status=owner.ownership_status or OwnershipStatus.ACTIVE.value,
    )


async def get_team_owner_stats(db: AsyncSession, user_id: int) -> TeamOwnerStats:
    """The counts the owners register's header shows.

    One grouped query per dimension rather than a scan of the whole table in
    Python: the numbers are read on every load of the page.
    """
    from app.teams.models import Team

    status_rows = (
        await db.execute(
            select(TeamOwner.ownership_status, func.count(TeamOwner.id))
            .where(TeamOwner.user_id == user_id)
            .group_by(TeamOwner.ownership_status)
        )
    ).all()
    by_status = {status: int(count) for status, count in status_rows}

    with_teams = int(
        (
            await db.execute(
                select(func.count(func.distinct(TeamOwnership.owner_id)))
                .join(TeamOwner, TeamOwner.id == TeamOwnership.owner_id)
                .where(
                    TeamOwner.user_id == user_id,
                    TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value,
                )
            )
        ).scalar()
        or 0
    )

    owned_teams = int(
        (
            await db.execute(
                select(func.count(func.distinct(TeamOwnership.team_id)))
                .join(TeamOwner, TeamOwner.id == TeamOwnership.owner_id)
                .where(
                    TeamOwner.user_id == user_id,
                    TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value,
                )
            )
        ).scalar()
        or 0
    )

    total_teams = int(
        (await db.execute(select(func.count(Team.id)).where(Team.user_id == user_id))).scalar() or 0
    )

    total = sum(by_status.values())
    return TeamOwnerStats(
        total=total,
        active=by_status.get(OwnershipStatus.ACTIVE.value, 0),
        inactive=by_status.get(OwnershipStatus.INACTIVE.value, 0),
        with_teams=with_teams,
        teams_without_owners=max(total_teams - owned_teams, 0),
    )


async def email_taken(
    db: AsyncSession, email: str, user_id: int, exclude_owner_id: int | None = None
) -> bool:
    """Whether this account already has an owner on this address.

    Case-insensitive, and scoped to the account: the same address may be filed
    by two clubs without either of them being wrong.
    """
    needle = (email or "").strip().lower()
    if not needle:
        return False
    query = select(TeamOwner.id).where(
        TeamOwner.user_id == user_id,
        func.lower(TeamOwner.email) == needle,
    )
    if exclude_owner_id is not None:
        query = query.where(TeamOwner.id != exclude_owner_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none() is not None


# --- the stakes ---------------------------------------------------------------


async def get_team_ownerships(
    db: AsyncSession, team_id: int, user_id: int, live_only: bool = False
) -> list[TeamOwnership]:
    """The stakes on one team of this account.

    The live ones come first, then the historical ones, and within each group
    the oldest first - so the panel reads as "who runs this club now", followed
    by "who ran it before".
    """
    from app.teams.models import Team

    where = [TeamOwnership.team_id == team_id]
    if live_only:
        where.append(TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value)
    # The team has to belong to this account.
    where.append(TeamOwnership.team_id.in_(select(Team.id).where(Team.user_id == user_id)))

    result = await db.execute(
        select(TeamOwnership)
        .options(selectinload(TeamOwnership.owner), selectinload(TeamOwnership.team))
        .where(*where)
    )
    stakes = list(result.scalars().all())
    stakes.sort(
        key=lambda stake: (
            0 if stake.ownership_status == OwnershipStatus.ACTIVE.value else 1,
            stake.ownership_start_date or date.max,
            stake.id,
        )
    )
    return stakes


async def get_team_ownership(db: AsyncSession, team_id: int, owner_id: int) -> TeamOwnership | None:
    """The live stake one owner holds on one team, if there is one."""
    result = await db.execute(
        select(TeamOwnership)
        .options(selectinload(TeamOwnership.owner), selectinload(TeamOwnership.team))
        .where(
            TeamOwnership.team_id == team_id,
            TeamOwnership.owner_id == owner_id,
            TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value,
        )
    )
    return result.scalar_one_or_none()


async def _live_percentage_total(
    db: AsyncSession, team_id: int, exclude_stake_id: int | None = None
) -> float:
    """The sum of the live stakes' percentages on a team.

    The stakes with no percentage are not counted: a club that records one
    owner outright and one owner without a share has said that the second
    stake is not a number, and inventing a share for it would make the total
    mean something the club never said.
    """
    query = select(func.coalesce(func.sum(TeamOwnership.ownership_percentage), 0)).where(
        TeamOwnership.team_id == team_id,
        TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value,
        TeamOwnership.ownership_percentage.isnot(None),
    )
    if exclude_stake_id is not None:
        query = query.where(TeamOwnership.id != exclude_stake_id)
    return float((await db.execute(query)).scalar() or 0)


async def _assert_percentage_fits(
    db: AsyncSession,
    team_id: int,
    percentage: float | None,
    exclude_stake_id: int | None = None,
) -> None:
    """Refuse a stake that would take the team over a hundred percent.

    The live stakes are allowed to add up to less than a hundred - a club may
    leave part of its equity unallocated - but not more.
    """
    if percentage is None:
        return
    current = await _live_percentage_total(db, team_id, exclude_stake_id)
    if current + percentage > 100.0001:
        raise OwnershipPercentageError(
            f"The live ownership percentages of this team already add up to "
            f"{current:g}%. Adding {percentage:g}% would take them over 100%."
        )


async def _sync_team_owner_mirror(db: AsyncSession, team) -> None:
    """Keep the team's free-text owner column in step with the stake book.

    ``teams.owner`` is the app's original way of writing down who runs a club -
    a name in a column. The stake book is the truth now, and the column is a
    display mirror of it, the same way a match's official slots mirror the
    appointment book.

    The column is only written when there is a live stake to write, so a club
    that has handed a team over keeps the name it had rather than having it
    blanked.
    """
    stakes = await get_team_ownerships(db, team.id, team.user_id, live_only=True)
    if not stakes:
        return
    names = [
        full_name(stake.owner.first_name, stake.owner.last_name)
        for stake in stakes
        if stake.owner is not None
    ]
    names = [name for name in names if name]
    if not names:
        return
    # The column is 200 characters wide; a club with a long list of owners is
    # truncated rather than refused.
    team.owner = ", ".join(names)[:200]


async def assign_owner_to_team(
    db: AsyncSession,
    team,
    owner: TeamOwner,
    data: TeamOwnershipInput,
    user_id: int,
) -> TeamOwnership:
    """Give an owner a stake in a team.

    The rules, in the order they are checked:

    * the owner may not hold the same team twice at the same time - a live
      stake is already there, and the answer is 409 rather than a second row;
    * the live stakes of the team may not add up to more than a hundred;
    * a stake with no stated type takes the owner's own type.

    Changing the terms of a stake the owner already holds is a different
    job - that is what ``PATCH /teams/{team_id}/owners/{owner_id}`` is for.

    The write is one transaction: the stake and the team's display mirror are
    committed together or not at all.
    """
    existing = await get_team_ownership(db, team.id, owner.id)
    if existing is not None:
        # A live stake is already there. A second one would mean the owner
        # holds the same club twice, which the partial unique index on the
        # live stakes would refuse anyway - so it is refused here, with a
        # message that says what to do instead.
        raise DuplicateActiveOwnershipError(team.id, owner.id)

    # The live stakes may add up to less than a hundred - a club may leave part
    # of its equity unallocated - but never more.
    await _assert_percentage_fits(db, team.id, data.ownership_percentage)

    stake = TeamOwnership(
        team_id=team.id,
        owner_id=owner.id,
        ownership_type=data.ownership_type or owner.ownership_type,
        ownership_percentage=data.ownership_percentage,
        ownership_start_date=data.ownership_start_date,
        ownership_end_date=data.ownership_end_date,
        ownership_status=data.ownership_status or OwnershipStatus.ACTIVE.value,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(stake)

    try:
        await db.flush()
    except IntegrityError as exc:
        # The partial unique index on the live stakes fired: two requests tried
        # to give the same owner the same team at the same time.
        await db.rollback()
        raise DuplicateActiveOwnershipError(team.id, owner.id) from exc

    await _sync_team_owner_mirror(db, team)
    await db.commit()
    await db.refresh(stake)
    return stake


async def update_team_ownership(
    db: AsyncSession,
    team,
    owner_id: int,
    data: TeamOwnershipInput | Any,
    user_id: int,
) -> TeamOwnership:
    """Change the terms of the live stake one owner holds on one team."""
    stake = await get_team_ownership(db, team.id, owner_id)
    if stake is None:
        raise OwnershipError("This owner does not hold this team.", status_code=404)

    payload = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    payload.pop("team_id", None)
    payload.pop("owner_id", None)

    percentage = payload.get("ownership_percentage")
    if percentage is not None:
        await _assert_percentage_fits(db, team.id, float(percentage), exclude_stake_id=stake.id)

    for key, value in payload.items():
        if value is None and key in {"ownership_percentage", "ownership_start_date"}:
            # Clearing a percentage or a start date is allowed; clearing the
            # other fields is not, and the caller has to say so explicitly.
            setattr(stake, key, None)
            continue
        if value is None:
            continue
        setattr(stake, key, value)

    stake.updated_by_id = user_id
    await db.commit()
    await db.refresh(stake)
    return stake


async def end_team_ownership(db: AsyncSession, team, owner_id: int, user_id: int) -> bool:
    """End the live stake one owner holds on one team.

    The row is kept: the end date and the inactive status are what turn it
    into history, and deleting it would leave the club's past with a hole in
    it. A later stake on the same team is a new row, not this one reopened.
    """
    stake = await get_team_ownership(db, team.id, owner_id)
    if stake is None:
        return False

    stake.ownership_status = OwnershipStatus.INACTIVE.value
    if stake.ownership_end_date is None:
        stake.ownership_end_date = date.today()
    stake.updated_by_id = user_id

    await _sync_team_owner_mirror(db, team)
    await db.commit()
    await db.refresh(stake)
    return True


async def sync_owner_stakes(
    db: AsyncSession,
    owner: TeamOwner,
    stakes: list[TeamOwnershipInput],
    user_id: int,
) -> None:
    """Make the owner's live stakes be exactly the ones the caller described.

    A stake that is already live is changed, a stake that is new is created,
    and a live stake the caller left out is ended rather than deleted. The
    history is preserved: an ended stake keeps its row and gains an end date.
    """
    from app.teams.models import Team

    wanted: dict[int, TeamOwnershipInput] = {}
    for item in stakes:
        if item.team_id in wanted:
            raise OwnershipError(
                "The same team was named twice in the list of ownerships.", status_code=400
            )
        wanted[item.team_id] = item

    team_ids = list(wanted)
    teams: dict[int, Any] = {}
    if team_ids:
        result = await db.execute(
            select(Team).where(Team.id.in_(team_ids), Team.user_id == user_id)
        )
        teams = {team.id: team for team in result.scalars().all()}
    missing = [team_id for team_id in team_ids if team_id not in teams]
    if missing:
        raise OwnershipError(f"Team {missing[0]} not found", status_code=404)

    # The stakes that are live now, so the ones the caller left out can be
    # ended.
    existing = (
        (
            await db.execute(
                select(TeamOwnership).where(
                    TeamOwnership.owner_id == owner.id,
                    TeamOwnership.ownership_status == OwnershipStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    by_team = {stake.team_id: stake for stake in existing}

    # The percentages are checked before anything is written, so a request
    # that would take a team over a hundred percent changes nothing at all.
    for team_id, item in wanted.items():
        if item.ownership_percentage is None:
            continue
        stake = by_team.get(team_id)
        await _assert_percentage_fits(
            db,
            team_id,
            float(item.ownership_percentage),
            exclude_stake_id=stake.id if stake else None,
        )

    for team_id, item in wanted.items():
        stake = by_team.get(team_id)
        if stake is None:
            db.add(
                TeamOwnership(
                    team_id=team_id,
                    owner_id=owner.id,
                    ownership_type=item.ownership_type or owner.ownership_type,
                    ownership_percentage=item.ownership_percentage,
                    ownership_start_date=item.ownership_start_date,
                    ownership_end_date=item.ownership_end_date,
                    ownership_status=item.ownership_status or OwnershipStatus.ACTIVE.value,
                    created_by_id=user_id,
                    updated_by_id=user_id,
                )
            )
            continue
        if item.ownership_type is not None:
            stake.ownership_type = item.ownership_type
        if item.ownership_percentage is not None:
            stake.ownership_percentage = float(item.ownership_percentage)
        if item.ownership_start_date is not None:
            stake.ownership_start_date = item.ownership_start_date
        if item.ownership_end_date is not None:
            stake.ownership_end_date = item.ownership_end_date
        if item.ownership_status is not None:
            stake.ownership_status = item.ownership_status
        stake.updated_by_id = user_id

    # A live stake the caller left out is ended, not deleted.
    for team_id, stake in by_team.items():
        if team_id in wanted:
            continue
        stake.ownership_status = OwnershipStatus.INACTIVE.value
        if stake.ownership_end_date is None:
            stake.ownership_end_date = date.today()
        stake.updated_by_id = user_id

    for team in teams.values():
        await _sync_team_owner_mirror(db, team)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateActiveOwnershipError(0, owner.id) from exc


# --- the register -------------------------------------------------------------


async def create_team_owner(
    db: AsyncSession,
    data: TeamOwnerCreate,
    user_id: int,
    *,
    linked_user_id: int | None = None,
) -> TeamOwner:
    """Register an owner for this account.

    The owner and the stakes they take on are written in one transaction: an
    owner whose stake could not be created is not an owner at all.

    ``linked_user_id`` links the record to the account that signed in and is
    filling in their own profile. It is only ever passed by the self-service
    route, which takes the id from the token rather than the request body.
    """
    if linked_user_id is not None:
        existing = await get_team_owner_by_linked_user(db, linked_user_id)
        if existing is not None:
            raise OwnerProfileExistsError(existing.id)

    if await email_taken(db, data.email, user_id):
        raise DuplicateOwnerEmailError(data.email)

    owner = TeamOwner(
        **data.model_dump(exclude={"ownerships"}),
        user_id=user_id,
        linked_user_id=linked_user_id,
    )
    db.add(owner)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateOwnerEmailError(data.email) from exc

    if data.ownerships:
        await sync_owner_stakes(db, owner, data.ownerships, user_id)
    else:
        await db.commit()

    await db.refresh(owner)
    return await get_team_owner(db, owner.id, user_id)


# The columns a row cannot do without. A PATCH that sends one of them as null
# is asking to clear a field that has to be there, and the database would
# refuse the write with an error the client cannot act on. The field is left
# as it is instead, so a partial update that names an optional field it does
# not want to change does not destroy the record.
_REQUIRED_FIELDS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "gender",
    "country_id",
    "email",
    "ownership_type",
    "ownership_status",
)


async def update_team_owner(
    db: AsyncSession,
    owner_id: int,
    data: TeamOwnerUpdate,
    user_id: int,
    *,
    allow_linked: bool = False,
) -> TeamOwner | None:
    """Apply a partial update to one owner.

    ``allow_linked`` opens the second door: the owner of a record may change
    their own profile, and the record is found through the link to their
    sign-in rather than through the register's own account. The email is still
    checked against the account the record belongs to, so a person editing
    their own profile cannot take an address that is already on the register.
    """
    owner = await get_team_owner(db, owner_id, user_id)
    if owner is None and allow_linked:
        linked = await get_team_owner_by_linked_user(db, user_id)
        owner = linked if linked is not None and linked.id == owner_id else None
    if not owner:
        return None
    payload = data.model_dump(exclude_unset=True)
    stakes = payload.pop("ownerships", None)

    # The account the record belongs to. For a self-service edit this is the
    # register that holds the record, not the sign-in that is asking to
    # change it.
    scope_id = owner.user_id if owner.user_id is not None else user_id

    for field in _REQUIRED_FIELDS:
        if field in payload and payload[field] is None:
            payload.pop(field)
    if payload.get("email") and await email_taken(
        db, payload["email"], scope_id, exclude_owner_id=owner_id
    ):
        raise DuplicateOwnerEmailError(payload["email"], existing_id=owner_id)

    # The organisation details are only compulsory for a company or a
    # franchise, so the check is made against the type the record will have
    # after the update rather than the one it has now.
    resulting_type = payload.get("ownership_type") or owner.ownership_type
    organization_name = payload.get("organization_name", owner.organization_name)
    registration_number = payload.get("registration_number", owner.registration_number)
    if resulting_type in (
        OwnershipType.COMPANY_ORGANIZATION.value,
        OwnershipType.FRANCHISE_OWNER.value,
    ):
        if not organization_name:
            raise OwnershipError(
                "organization_name is required for a company or franchise ownership",
                status_code=400,
            )
        if not registration_number:
            raise OwnershipError(
                "registration_number is required for a company or franchise ownership",
                status_code=400,
            )

    for key, val in payload.items():
        setattr(owner, key, val)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateOwnerEmailError(payload.get("email", owner.email)) from exc
    await db.refresh(owner)

    if stakes is not None:
        await sync_owner_stakes(db, owner, stakes, scope_id)

    return await get_team_owner(db, owner_id, scope_id)


async def replace_team_owner(
    db: AsyncSession, owner_id: int, data: TeamOwnerCreate, user_id: int
) -> TeamOwner | None:
    """Replace every mutable field of one owner of this account."""
    owner = await get_team_owner(db, owner_id, user_id)
    if not owner:
        return None
    if await email_taken(db, data.email, user_id, exclude_owner_id=owner_id):
        raise DuplicateOwnerEmailError(data.email, existing_id=owner_id)

    payload = data.model_dump(exclude={"ownerships"})
    for key, val in payload.items():
        setattr(owner, key, val)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateOwnerEmailError(data.email) from exc
    await db.refresh(owner)

    if data.ownerships:
        await sync_owner_stakes(db, owner, data.ownerships, user_id)

    return await get_team_owner(db, owner_id, user_id)


async def get_team_owner_delete_check(
    db: AsyncSession, owner_id: int, user_id: int
) -> TeamOwnerDeleteCheck | None:
    """What deleting an owner would do.

    Answered before the delete is attempted, so the confirmation dialog can say
    what is going to happen instead of discovering it afterwards.
    """
    owner = await get_team_owner(db, owner_id, user_id)
    if not owner:
        return None

    rows = (
        await db.execute(
            select(TeamOwnership.ownership_status, func.count(TeamOwnership.id))
            .where(TeamOwnership.owner_id == owner_id)
            .group_by(TeamOwnership.ownership_status)
        )
    ).all()
    by_status = {status: int(count) for status, count in rows}

    active = by_status.get(OwnershipStatus.ACTIVE.value, 0)
    historical = sum(
        count for status, count in by_status.items() if status != OwnershipStatus.ACTIVE.value
    )
    total = active + historical

    can_delete = total == 0
    reason = None
    if not can_delete:
        reason = (
            f"This owner holds {total} team ownership"
            f"{'s' if total != 1 else ''}. End the ownership before deleting the owner, "
            "so the teams' history is not lost."
        )
    return TeamOwnerDeleteCheck(
        owner_id=owner_id,
        can_delete=can_delete,
        active_ownerships=active,
        historical_ownerships=historical,
        reason=reason,
    )


async def delete_team_owner(db: AsyncSession, owner_id: int, user_id: int) -> bool | None:
    """Delete an owner who holds no team.

    Returns ``None`` when the owner does not belong to this account and
    ``False`` when they still hold a team, so the caller can answer 404 and
    409 respectively. An owner who has owned a team is part of that team's
    record: deleting the row would leave a team pointing at an owner who no
    longer exists, and would take the club's history with it.
    """
    owner = await get_team_owner(db, owner_id, user_id)
    if not owner:
        return None

    holdings = (
        await db.execute(
            select(func.count(TeamOwnership.id)).where(TeamOwnership.owner_id == owner_id)
        )
    ).scalar()
    if holdings:
        return False

    await db.delete(owner)
    await db.commit()
    return True


async def update_team_owner_profile_image(
    db: AsyncSession, owner_id: int, profile_image: str, user_id: int
) -> TeamOwner | None:
    owner = await get_team_owner(db, owner_id, user_id)
    if not owner:
        return None
    owner.profile_image = profile_image
    await db.commit()
    await db.refresh(owner)
    return owner


async def clear_team_owner_profile_image(
    db: AsyncSession, owner_id: int, user_id: int
) -> TeamOwner | None:
    """Remove the photo from an owner's record."""
    owner = await get_team_owner(db, owner_id, user_id)
    if not owner:
        return None
    owner.profile_image = None
    await db.commit()
    await db.refresh(owner)
    return owner


# --- building the responses ---------------------------------------------------


def _actor_name(user) -> str | None:
    """The name of a user, for the audit columns."""
    if user is None:
        return None
    return user.full_name or user.email


def stake_response(stake: TeamOwnership) -> TeamOwnershipResponse:
    """Build the response for one stake.

    The team's name and logo travel with it so a client does not have to join
    the teams table itself to label a stake.
    """
    team = stake.team
    return TeamOwnershipResponse(
        id=stake.id,
        team_id=stake.team_id,
        owner_id=stake.owner_id,
        team_name=team.name if team else "",
        team_short_name=team.short_name if team else "",
        team_logo=team.logo if team else None,
        ownership_type=stake.ownership_type,
        ownership_percentage=stake.ownership_percentage,
        ownership_start_date=stake.ownership_start_date,
        ownership_end_date=stake.ownership_end_date,
        ownership_status=stake.ownership_status,
        created_at=stake.created_at,
        updated_at=stake.updated_at,
    )


def team_owner_response(
    owner: TeamOwner,
    *,
    reveal_phone: bool,
    reveal_notes: bool,
) -> TeamOwnerResponse:
    """Build a response for one owner.

    ``reveal_phone`` and ``reveal_notes`` are the authorisation decisions: a
    list masks the number and hides the internal notes, while an admin or the
    account that owns the record sees both in full.
    """
    stakes = sorted(
        owner.ownerships or [],
        key=lambda stake: (
            0 if stake.ownership_status == OwnershipStatus.ACTIVE.value else 1,
            stake.ownership_start_date or date.max,
            stake.id,
        ),
    )
    responses = [stake_response(stake) for stake in stakes]

    teams = [
        TeamOwnerTeamSummary(
            id=stake.team_id,
            name=stake.team.name if stake.team else "",
            short_name=stake.team.short_name if stake.team else "",
            logo=stake.team.logo if stake.team else None,
            ownership_type=stake.ownership_type,
            ownership_status=stake.ownership_status,
        )
        for stake in stakes
    ]

    # The owner's primary stake: the live one they held longest. It is what
    # the list shows in the "percentage" column, so a row can say what the
    # owner is worth to their club without walking the stakes itself.
    primary = next(
        (stake for stake in stakes if stake.ownership_status == OwnershipStatus.ACTIVE.value),
        stakes[0] if stakes else None,
    )

    return TeamOwnerResponse(
        id=owner.id,
        first_name=owner.first_name,
        last_name=owner.last_name,
        full_name=full_name(owner.first_name, owner.last_name),
        date_of_birth=owner.date_of_birth,
        gender=owner.gender,
        country_id=owner.country_id,
        nationality=owner.country.name if owner.country else None,
        profile_image=owner.profile_image,
        email=owner.email,
        country_code=owner.country_code,
        mobile_number=(
            owner.mobile_number
            if reveal_phone
            else mask_mobile(owner.country_code, owner.mobile_number)
        ),
        phone_display=mask_mobile(owner.country_code, owner.mobile_number),
        ownership_type=owner.ownership_type,
        ownership_status=owner.ownership_status,
        organization_name=owner.organization_name,
        registration_number=owner.registration_number,
        notes=owner.notes if reveal_notes else None,
        ownerships=responses,
        teams=teams,
        ownership_percentage=primary.ownership_percentage if primary else None,
        ownership_start_date=primary.ownership_start_date if primary else None,
        ownership_end_date=primary.ownership_end_date if primary else None,
        created_at=owner.created_at,
        updated_at=owner.updated_at,
        created_by=_actor_name(owner.created_by),
        updated_by=_actor_name(owner.updated_by),
    )
