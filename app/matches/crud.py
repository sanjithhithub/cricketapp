from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.matches.models import Match
from app.matches.schemas import MatchCreate, MatchUpdate
from app.players.identity import full_name
from app.referees.crud import OFFICIAL_SLOTS, resolve_match_referees, sync_match_assignments
from app.referees.models import MatchRefereeAssignment
from app.scoring.crud import sync_match_result
from app.scoring.enums import ExtraType, MatchStatus
from app.scoring.models import Delivery, Innings
from app.teams.models import PlayerTeamAssignment, Team


async def _validate_teams(
    db: AsyncSession, team_a_id: int, team_b_id: int, toss_winner_id: int, user_id: int
):
    result = await db.execute(
        select(Team).where(
            Team.id.in_([team_a_id, team_b_id, toss_winner_id]), Team.user_id == user_id
        )
    )
    teams = {t.id: t for t in result.scalars().all()}
    errors = []
    if team_a_id not in teams:
        errors.append(f"Team A (id={team_a_id}) not found")
    if team_b_id not in teams:
        errors.append(f"Team B (id={team_b_id}) not found")
    if toss_winner_id not in teams:
        errors.append(f"Toss winner (id={toss_winner_id}) not found")
    if team_a_id == team_b_id:
        errors.append("Team A and Team B must be different teams")
    if toss_winner_id not in (team_a_id, team_b_id):
        errors.append("Toss winner must be Team A or Team B")
    return errors


async def _validate_player_of_match(
    db: AsyncSession, team_a_id: int, team_b_id: int, player_id: int | None
) -> str | None:
    """The player-of-the-match award must go to someone in one of the two sides.

    It may be a player from the losing team - that is exactly the case the award
    exists for - so only a player outside both squads is rejected. ``None`` (no
    award yet) is always fine.
    """
    if player_id is None:
        return None
    result = await db.execute(
        select(PlayerTeamAssignment.player_id).where(
            PlayerTeamAssignment.team_id.in_([team_a_id, team_b_id]),
            PlayerTeamAssignment.player_id == player_id,
        )
    )
    if result.scalar_one_or_none() is None:
        return "Player of the match must be a player in Team A or Team B"
    return None


async def _resolve_officials(
    db: AsyncSession, referee_ids: dict[str, int | None], user_id: int
) -> tuple[dict[str, object], list[str]]:
    """Check the officials a match names and return them by slot.

    The rules live in ``app.referees.crud``: an official must belong to this
    account, and the same person cannot fill two slots of one match.
    """
    return await resolve_match_referees(db, referee_ids, user_id)


def _officials_from_match(match: Match) -> dict[str, int | None]:
    """The official ids a match currently carries, by slot."""
    return {slot: getattr(match, id_column) for slot, id_column, _, _ in OFFICIAL_SLOTS}


def _apply_official_names(
    match: Match,
    resolved: dict[str, object],
    changed_slots: set[str],
    changed_names: set[str] | None = None,
) -> None:
    """Keep the free-text name columns in step with the linked officials.

    A linked official is the source of truth for their name, so the mirror
    beside it is rewritten from the record rather than taken from the request:
    the two can then never disagree on a scorecard. A slot with no linked
    official keeps the plain name the match has always been able to carry.

    ``changed_slots`` holds the slots whose id the request touched. A slot that
    was explicitly unlinked loses the name that was mirrored from the official -
    unless the request named a replacement in the same breath, which the
    request's own name wins over. ``changed_names`` holds the name columns the
    request set explicitly, and is what tells the two apart: a slot and a name
    column are different fields, and only the request says which one it meant.
    """
    changed_names = changed_names or set()
    for slot, _id_column, name_column, _ in OFFICIAL_SLOTS:
        referee = resolved.get(slot)
        if referee is not None:
            setattr(
                match,
                name_column,
                full_name(referee.first_name, referee.last_name),
            )
        elif slot in changed_slots and name_column not in changed_names:
            setattr(match, name_column, None)


async def _max_legal_balls_bowled(db: AsyncSession, match_id: int) -> int:
    """The most legal balls any non-Super-Over innings of this match has bowled.

    A Super Over is one over by rule and never constrains the innings over count,
    so it is excluded. Wides and no-balls are not balls of the over, so they do
    not count either - the number returned is the overs the innings has actually
    delivered.
    """
    result = await db.execute(
        select(Delivery.innings_id, Delivery.extra_type)
        .join(Innings, Innings.id == Delivery.innings_id)
        .where(Innings.match_id == match_id, Innings.is_super_over.is_(False))
    )
    counts: dict[int, int] = {}
    for innings_id, extra in result.all():
        if extra not in (ExtraType.WIDE.value, ExtraType.NO_BALL.value):
            counts[innings_id] = counts.get(innings_id, 0) + 1
    return max(counts.values(), default=0)


async def _validate_total_overs(db: AsyncSession, match_id: int, total_overs: int | None):
    """Refuse an over count lower than cricket already played.

    Reducing the overs is allowed, but not below what an innings has bowled: the
    scorecard would then claim the side faced more overs than the match permitted.
    Raising the count, or clearing it back to the format default (``None``), is
    always allowed.
    """
    if total_overs is None:
        return None
    bowled_balls = await _max_legal_balls_bowled(db, match_id)
    if total_overs * 6 < bowled_balls:
        bowled_overs = f"{bowled_balls // 6}.{bowled_balls % 6}"
        return f"total_overs cannot be lower than the overs already bowled ({bowled_overs})"
    return None


async def _heal_results(db: AsyncSession, matches: Match | list[Match]) -> None:
    """Recompute-and-persist the result of every completed match so a stale or
    manually entered result (e.g. a leftover 'tie' from the create-match form)
    is replaced by the official result derived from the innings scorecards."""
    if matches is None:
        return
    items = [matches] if isinstance(matches, Match) else matches
    for match in items:
        if match.status == MatchStatus.COMPLETED.value:
            await sync_match_result(db, match)


async def get_matches(
    db: AsyncSession, user_id: int, skip: int = 0, limit: int = 100
) -> tuple[list[Match], int]:
    """One page of this account's matches, and the total that page is drawn from."""
    total = int(
        (await db.execute(select(func.count(Match.id)).where(Match.user_id == user_id))).scalar()
        or 0
    )
    result = await db.execute(
        select(Match)
        .options(
            selectinload(Match.team_a), selectinload(Match.team_b), selectinload(Match.toss_winner)
        )
        .where(Match.user_id == user_id)
        .order_by(Match.id)
        .offset(skip)
        .limit(limit)
    )
    matches = list(result.scalars().all())
    await _heal_results(db, matches)
    return matches, total


async def get_match(db: AsyncSession, match_id: int, user_id: int):
    result = await db.execute(
        select(Match)
        .options(
            selectinload(Match.team_a), selectinload(Match.team_b), selectinload(Match.toss_winner)
        )
        .where(Match.id == match_id, Match.user_id == user_id)
    )
    match = result.scalar_one_or_none()
    if match is not None:
        await _heal_results(db, match)
    return match


async def create_match(db: AsyncSession, data: MatchCreate, user_id: int, *, commit: bool = True):
    errors = await _validate_teams(db, data.team_a_id, data.team_b_id, data.toss_winner_id, user_id)
    errors += [
        e
        for e in [
            await _validate_player_of_match(
                db, data.team_a_id, data.team_b_id, data.player_of_match_id
            )
        ]
        if e
    ]

    # The officials are checked before the match is written, so a match never
    # exists naming an official this account does not have on file.
    resolved, official_errors = await _resolve_officials(
        db,
        {
            "referee_1": data.referee_1_id,
            "referee_2": data.referee_2_id,
            "match_referee": data.match_referee_id,
        },
        user_id,
    )
    errors += official_errors
    if errors:
        return None, "; ".join(errors)

    match = Match(**data.model_dump(), user_id=user_id)
    _apply_official_names(match, resolved, changed_slots=set())
    db.add(match)
    # The appointment rows are written before the commit, so a match and its
    # appointments are saved together: a match that names an official it has no
    # appointment for would be a half-written fixture.
    await db.flush()
    await sync_match_assignments(db, match, resolved, user_id=user_id)
    if not commit:
        return match, None
    await db.commit()
    await db.refresh(match)

    match = await get_match(db, match.id, user_id)
    return match, None


async def update_match(db: AsyncSession, match_id: int, data: MatchUpdate, user_id: int):
    match = await get_match(db, match_id, user_id)
    if not match:
        return None, "Match not found"

    update_data = data.model_dump(exclude_unset=True)

    if update_data.keys() & {"team_a_id", "team_b_id", "toss_winner_id"}:
        new_team_a = update_data.get("team_a_id", match.team_a_id)
        new_team_b = update_data.get("team_b_id", match.team_b_id)
        new_toss_winner = update_data.get("toss_winner_id", match.toss_winner_id)
        errors = await _validate_teams(db, new_team_a, new_team_b, new_toss_winner, user_id)
        if errors:
            return None, "; ".join(errors)

    if "player_of_match_id" in update_data:
        new_team_a = update_data.get("team_a_id", match.team_a_id)
        new_team_b = update_data.get("team_b_id", match.team_b_id)
        error = await _validate_player_of_match(
            db, new_team_a, new_team_b, update_data["player_of_match_id"]
        )
        if error:
            return None, error

    if "total_overs" in update_data:
        error = await _validate_total_overs(db, match.id, update_data["total_overs"])
        if error:
            return None, error

    # The officials are resolved against the state the match will be in after
    # the update, not against the request alone: a PATCH that only changes one
    # slot still has to agree with the two it does not mention.
    effective = _officials_from_match(match)
    for slot, id_column, _, _ in OFFICIAL_SLOTS:
        if id_column in update_data:
            effective[slot] = update_data[id_column]

    resolved, official_errors = await _resolve_officials(db, effective, user_id)
    if official_errors:
        return None, "; ".join(official_errors)

    for key, val in update_data.items():
        setattr(match, key, val)

    changed_slots = {slot for slot, id_column, _, _ in OFFICIAL_SLOTS if id_column in update_data}
    changed_names = {
        name_column for _, _, name_column, _ in OFFICIAL_SLOTS if name_column in update_data
    }
    _apply_official_names(match, resolved, changed_slots, changed_names)
    await sync_match_assignments(db, match, resolved, user_id=user_id)

    await db.commit()
    match = await get_match(db, match.id, user_id)
    return match, None


async def replace_match(db: AsyncSession, match_id: int, data: MatchCreate, user_id: int):
    match = await get_match(db, match_id, user_id)
    if not match:
        return None, "Match not found"

    errors = await _validate_teams(db, data.team_a_id, data.team_b_id, data.toss_winner_id, user_id)
    errors += [
        e
        for e in [
            await _validate_player_of_match(
                db, data.team_a_id, data.team_b_id, data.player_of_match_id
            )
        ]
        if e
    ]
    if errors:
        return None, "; ".join(errors)

    error = await _validate_total_overs(db, match.id, data.total_overs)
    if error:
        return None, error

    resolved, official_errors = await _resolve_officials(
        db,
        {
            "referee_1": data.referee_1_id,
            "referee_2": data.referee_2_id,
            "match_referee": data.match_referee_id,
        },
        user_id,
    )
    if official_errors:
        return None, "; ".join(official_errors)

    for key, val in data.model_dump().items():
        setattr(match, key, val)

    _apply_official_names(match, resolved, changed_slots=set())
    await sync_match_assignments(db, match, resolved, user_id=user_id)

    await db.commit()
    match = await get_match(db, match.id, user_id)
    return match, None


async def delete_match(db: AsyncSession, match_id: int, user_id: int):
    """Delete a match together with everything derived from it."""
    from sqlalchemy import text

    from app.scoring.models import Delivery, InningsBatsman
    from app.tournaments.models import TournamentFixture

    try:
        await db.execute(text("PRAGMA foreign_keys=OFF"))
        result = await db.execute(
            select(Match).where(Match.id == match_id, Match.user_id == user_id)
        )
        match = result.scalar_one_or_none()
        if not match:
            await db.execute(text("PRAGMA foreign_keys=ON"))
            return False

        # Unlink from any tournament fixture
        await db.execute(
            update(TournamentFixture)
            .where(TournamentFixture.match_id == match_id)
            .values(match_id=None)
        )

        innings_ids = list(
            (await db.execute(select(Innings.id).where(Innings.match_id == match_id)))
            .scalars()
            .all()
        )
        if innings_ids:
            # Wickets and extras are columns of the delivery row, so removing
            # the deliveries is what removes them.
            await db.execute(
                delete(InningsBatsman).where(InningsBatsman.innings_id.in_(innings_ids))
            )
            await db.execute(delete(Delivery).where(Delivery.innings_id.in_(innings_ids)))
            await db.execute(delete(Innings).where(Innings.id.in_(innings_ids)))

        await db.execute(
            delete(MatchRefereeAssignment).where(MatchRefereeAssignment.match_id == match_id)
        )

        await db.execute(delete(Match).where(Match.id == match_id))
        await db.commit()
        await db.execute(text("PRAGMA foreign_keys=ON"))
        return True
    except Exception:
        await db.rollback()
        try:
            await db.execute(text("PRAGMA foreign_keys=ON"))
        except Exception:
            pass
        raise
