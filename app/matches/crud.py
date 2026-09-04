from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.matches.models import Match
from app.matches.schemas import MatchCreate, MatchUpdate
from app.teams.models import Team


async def _validate_teams(db: AsyncSession, team_a_id: int, team_b_id: int, toss_winner_id: int):
    result = await db.execute(select(Team).where(Team.id.in_([team_a_id, team_b_id, toss_winner_id])))
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


async def get_matches(db: AsyncSession, skip: int = 0, limit: int = 100):
    result = await db.execute(
        select(Match)
        .options(selectinload(Match.team_a), selectinload(Match.team_b), selectinload(Match.toss_winner))
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


async def get_match(db: AsyncSession, match_id: int):
    result = await db.execute(
        select(Match)
        .options(selectinload(Match.team_a), selectinload(Match.team_b), selectinload(Match.toss_winner))
        .where(Match.id == match_id)
    )
    return result.scalar_one_or_none()


async def create_match(db: AsyncSession, data: MatchCreate):
    errors = await _validate_teams(db, data.team_a_id, data.team_b_id, data.toss_winner_id)
    if errors:
        return None, "; ".join(errors)

    match = Match(**data.model_dump())
    db.add(match)
    await db.commit()
    await db.refresh(match)

    match = await get_match(db, match.id)
    return match, None


async def update_match(db: AsyncSession, match_id: int, data: MatchUpdate):
    match = await get_match(db, match_id)
    if not match:
        return None, "Match not found"

    update_data = data.model_dump(exclude_unset=True)

    if update_data.keys() & {"team_a_id", "team_b_id", "toss_winner_id"}:
        new_team_a = update_data.get("team_a_id", match.team_a_id)
        new_team_b = update_data.get("team_b_id", match.team_b_id)
        new_toss_winner = update_data.get("toss_winner_id", match.toss_winner_id)
        errors = await _validate_teams(db, new_team_a, new_team_b, new_toss_winner)
        if errors:
            return None, "; ".join(errors)

    for key, val in update_data.items():
        setattr(match, key, val)

    await db.commit()
    match = await get_match(db, match.id)
    return match, None


async def replace_match(db: AsyncSession, match_id: int, data: MatchCreate):
    match = await get_match(db, match_id)
    if not match:
        return None, "Match not found"

    errors = await _validate_teams(db, data.team_a_id, data.team_b_id, data.toss_winner_id)
    if errors:
        return None, "; ".join(errors)

    for key, val in data.model_dump().items():
        setattr(match, key, val)

    await db.commit()
    match = await get_match(db, match.id)
    return match, None


async def delete_match(db: AsyncSession, match_id: int):
    result = await db.execute(select(Match).where(Match.id == match_id))
    match = result.scalar_one_or_none()
    if not match:
        return False
    await db.delete(match)
    await db.commit()
    return True
