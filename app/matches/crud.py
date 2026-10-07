from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.matches.models import Match
from app.matches.schemas import MatchCreate, MatchUpdate
from app.scoring.crud import sync_match_result
from app.scoring.enums import MatchStatus
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


async def create_match(db: AsyncSession, data: MatchCreate, user_id: int):
    errors = await _validate_teams(db, data.team_a_id, data.team_b_id, data.toss_winner_id, user_id)
    errors += [
        e
        for e in [
            await _validate_player_of_match(db, data.team_a_id, data.team_b_id, data.player_of_match_id)
        ]
        if e
    ]
    if errors:
        return None, "; ".join(errors)

    match = Match(**data.model_dump(), user_id=user_id)
    db.add(match)
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

    for key, val in update_data.items():
        setattr(match, key, val)

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
            await _validate_player_of_match(db, data.team_a_id, data.team_b_id, data.player_of_match_id)
        ]
        if e
    ]
    if errors:
        return None, "; ".join(errors)

    for key, val in data.model_dump().items():
        setattr(match, key, val)

    await db.commit()
    match = await get_match(db, match.id, user_id)
    return match, None


async def delete_match(db: AsyncSession, match_id: int, user_id: int):
    result = await db.execute(select(Match).where(Match.id == match_id, Match.user_id == user_id))
    match = result.scalar_one_or_none()
    if not match:
        return False
    await db.delete(match)
    await db.commit()
    return True
