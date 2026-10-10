"""Writes for tournaments, membership and fixtures.

The read-only views (points table, leaderboards, fixture rows) live in
:mod:`app.tournaments.analytics`; this module owns creation, editing and the
generators that turn a list of entered teams into a schedule. It deliberately
imports *from* analytics rather than the reverse, so the derived layer stays
free of write concerns.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.matches.models import Match
from app.teams.models import Team
from app.tournaments.analytics import ranked_team_ids
from app.tournaments.enums import FixtureStage
from app.tournaments.models import Tournament, TournamentFixture, TournamentTeam
from app.tournaments.schemas import FixtureCreate, FixtureUpdate, TournamentCreate, TournamentUpdate

# The knockout draw sizes a competition may seed, and the stage each produces.
_KNOCKOUT_STAGES = {8: FixtureStage.QUALIFIER, 4: FixtureStage.SEMIFINAL, 2: FixtureStage.FINAL}


def _tournament_row(tournament: Tournament, team_count: int, fixture_count: int) -> dict:
    return {
        "id": tournament.id,
        "name": tournament.name,
        "description": tournament.description,
        "level_id": tournament.level_id,
        "format": tournament.format,
        "status": tournament.status,
        "start_date": tournament.start_date,
        "end_date": tournament.end_date,
        "points_win": tournament.points_win,
        "points_tie": tournament.points_tie,
        "points_loss": tournament.points_loss,
        "points_no_result": tournament.points_no_result,
        "team_count": team_count,
        "fixture_count": fixture_count,
    }


async def _counts(db: AsyncSession, tournament_id: int) -> tuple[int, int]:
    team_count = int(
        (
            await db.execute(
                select(func.count(TournamentTeam.id)).where(
                    TournamentTeam.tournament_id == tournament_id
                )
            )
        ).scalar()
        or 0
    )
    fixture_count = int(
        (
            await db.execute(
                select(func.count(TournamentFixture.id)).where(
                    TournamentFixture.tournament_id == tournament_id
                )
            )
        ).scalar()
        or 0
    )
    return team_count, fixture_count


async def get_tournaments(
    db: AsyncSession, user_id: int, skip: int = 0, limit: int = 100
) -> tuple[list[dict], int]:
    total = int(
        (
            await db.execute(select(func.count(Tournament.id)).where(Tournament.user_id == user_id))
        ).scalar()
        or 0
    )
    team_count = (
        select(func.count(TournamentTeam.id))
        .where(TournamentTeam.tournament_id == Tournament.id)
        .scalar_subquery()
    )
    fixture_count = (
        select(func.count(TournamentFixture.id))
        .where(TournamentFixture.tournament_id == Tournament.id)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Tournament, team_count, fixture_count)
        .where(Tournament.user_id == user_id)
        .order_by(Tournament.id)
        .offset(skip)
        .limit(limit)
    )
    rows = [_tournament_row(t, int(tc or 0), int(fc or 0)) for t, tc, fc in result.all()]
    return rows, total


async def tournament_response(db: AsyncSession, tournament: Tournament) -> dict:
    team_count, fixture_count = await _counts(db, tournament.id)
    return _tournament_row(tournament, team_count, fixture_count)


async def get_tournament(db: AsyncSession, tournament_id: int, user_id: int):
    result = await db.execute(
        select(Tournament).where(Tournament.id == tournament_id, Tournament.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_tournament(db: AsyncSession, data: TournamentCreate, user_id: int) -> Tournament:
    tournament = Tournament(**data.model_dump(), user_id=user_id)
    db.add(tournament)
    await db.commit()
    await db.refresh(tournament)
    return tournament


async def update_tournament(
    db: AsyncSession, tournament: Tournament, data: TournamentUpdate
) -> Tournament:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(tournament, key, value)
    await db.commit()
    await db.refresh(tournament)
    return tournament


async def delete_tournament(db: AsyncSession, tournament: Tournament) -> None:
    await db.delete(tournament)
    await db.commit()


async def list_tournament_teams(db: AsyncSession, tournament_id: int) -> list[dict]:
    result = await db.execute(
        select(TournamentTeam)
        .options(selectinload(TournamentTeam.team))
        .where(TournamentTeam.tournament_id == tournament_id)
        .order_by(TournamentTeam.id)
    )
    rows = []
    for entry in result.scalars().all():
        team = entry.team
        rows.append(
            {
                "id": entry.id,
                "team_id": entry.team_id,
                "team_name": team.name,
                "team_short_name": team.short_name,
                "logo": team.logo,
            }
        )
    return rows


async def add_team(
    db: AsyncSession, tournament: Tournament, team_id: int, user_id: int
) -> tuple[dict | None, str | None]:
    team = (
        await db.execute(select(Team).where(Team.id == team_id, Team.user_id == user_id))
    ).scalar_one_or_none()
    if team is None:
        return None, "Team not found"

    existing = (
        await db.execute(
            select(TournamentTeam).where(
                TournamentTeam.tournament_id == tournament.id,
                TournamentTeam.team_id == team_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None, "Team is already in this tournament"

    entry = TournamentTeam(tournament_id=tournament.id, team_id=team_id)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return (
        {
            "id": entry.id,
            "team_id": team.id,
            "team_name": team.name,
            "team_short_name": team.short_name,
            "logo": team.logo,
        },
        None,
    )


async def remove_team(
    db: AsyncSession, tournament: Tournament, team_id: int
) -> tuple[bool, str | None]:
    entry = (
        await db.execute(
            select(TournamentTeam).where(
                TournamentTeam.tournament_id == tournament.id,
                TournamentTeam.team_id == team_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        return False, "Team is not in this tournament"

    fixture_count = int(
        (
            await db.execute(
                select(func.count(TournamentFixture.id)).where(
                    TournamentFixture.tournament_id == tournament.id,
                    (TournamentFixture.team_a_id == team_id)
                    | (TournamentFixture.team_b_id == team_id),
                )
            )
        ).scalar()
        or 0
    )
    if fixture_count:
        return False, "Remove this team's fixtures before removing it from the tournament."

    await db.delete(entry)
    await db.commit()
    return True, None


async def _validate_fixture_teams(
    db: AsyncSession, tournament: Tournament, team_a_id: int, team_b_id: int
) -> str | None:
    if team_a_id == team_b_id:
        return "A fixture needs two different teams."
    entered = set(
        (
            await db.execute(
                select(TournamentTeam.team_id).where(
                    TournamentTeam.tournament_id == tournament.id,
                    TournamentTeam.team_id.in_([team_a_id, team_b_id]),
                )
            )
        )
        .scalars()
        .all()
    )
    if team_a_id not in entered:
        return f"Team {team_a_id} is not entered in this tournament."
    if team_b_id not in entered:
        return f"Team {team_b_id} is not entered in this tournament."
    return None


async def create_fixture(
    db: AsyncSession, tournament: Tournament, data: FixtureCreate
) -> tuple[TournamentFixture | None, str | None]:
    error = await _validate_fixture_teams(db, tournament, data.team_a_id, data.team_b_id)
    if error:
        return None, error
    fixture = TournamentFixture(tournament_id=tournament.id, **data.model_dump())
    db.add(fixture)
    await db.commit()
    await db.refresh(fixture)
    return fixture, None


def _round_robin_pairs(team_ids: list[int], legs: int) -> list[tuple[int, int, int]]:
    """A round-robin schedule as ``(round_number, team_a_id, team_b_id)``.

    The circle method: one side is fixed and the rest rotate, so every pair meets
    exactly once across ``n - 1`` rounds. An odd count gets a bye (a ``None``
    slot) that is dropped when pairing, giving each side one rest round. ``legs``
    of 2 appends the reverse fixtures as extra rounds.
    """
    slots: list[int | None] = list(team_ids)
    if len(slots) % 2:
        slots.append(None)
    n = len(slots)
    schedule: list[tuple[int, int, int]] = []
    for round_index in range(n - 1):
        for i in range(n // 2):
            a, b = slots[i], slots[n - 1 - i]
            if a is None or b is None:
                continue
            pair = (a, b) if round_index % 2 == 0 else (b, a)
            schedule.append((round_index + 1, pair[0], pair[1]))
        slots = [slots[0], slots[-1], *slots[1:-1]]
    if legs == 2 and team_ids:
        offset = n - 1
        schedule += [(r + offset, b, a) for r, a, b in schedule]
    return schedule


async def generate_fixtures(
    db: AsyncSession, tournament: Tournament, legs: int, replace: bool
) -> tuple[int, str | None]:
    existing = (
        (
            await db.execute(
                select(TournamentFixture).where(TournamentFixture.tournament_id == tournament.id)
            )
        )
        .scalars()
        .all()
    )
    if existing:
        if not replace:
            return 0, "This tournament already has fixtures. Pass replace=true to regenerate."
        if any(f.match_id is not None for f in existing):
            return 0, "Cannot regenerate: some fixtures already have a linked match."
        await db.execute(
            delete(TournamentFixture).where(TournamentFixture.tournament_id == tournament.id)
        )

    team_ids = list(
        (
            await db.execute(
                select(TournamentTeam.team_id)
                .where(TournamentTeam.tournament_id == tournament.id)
                .order_by(TournamentTeam.id)
            )
        )
        .scalars()
        .all()
    )
    if len(team_ids) < 2:
        return 0, "Add at least two teams before generating fixtures."

    schedule = _round_robin_pairs(team_ids, legs)
    for round_number, team_a_id, team_b_id in schedule:
        db.add(
            TournamentFixture(
                tournament_id=tournament.id,
                team_a_id=team_a_id,
                team_b_id=team_b_id,
                stage=FixtureStage.LEAGUE.value,
                round_number=round_number,
            )
        )
    await db.commit()
    return len(schedule), None


async def get_fixture(
    db: AsyncSession, tournament: Tournament, fixture_id: int
) -> TournamentFixture | None:
    return (
        await db.execute(
            select(TournamentFixture).where(
                TournamentFixture.id == fixture_id,
                TournamentFixture.tournament_id == tournament.id,
            )
        )
    ).scalar_one_or_none()


async def update_fixture(
    db: AsyncSession, tournament: Tournament, fixture_id: int, data: FixtureUpdate
) -> tuple[TournamentFixture | None, str | None]:
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, "Fixture not found"

    updates = data.model_dump(exclude_unset=True)
    if updates.keys() & {"team_a_id", "team_b_id"}:
        error = await _validate_fixture_teams(
            db,
            tournament,
            updates.get("team_a_id", fixture.team_a_id),
            updates.get("team_b_id", fixture.team_b_id),
        )
        if error:
            return None, error

    for key, value in updates.items():
        setattr(fixture, key, value)
    await db.commit()
    await db.refresh(fixture)
    return fixture, None


async def delete_fixture(db: AsyncSession, tournament: Tournament, fixture_id: int) -> bool:
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return False
    await db.delete(fixture)
    await db.commit()
    return True


async def set_fixture_result(
    db: AsyncSession, tournament: Tournament, fixture_id: int, match_id: int, user_id: int
) -> tuple[TournamentFixture | None, str | None]:
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, "Fixture not found"

    match = (
        await db.execute(select(Match).where(Match.id == match_id, Match.user_id == user_id))
    ).scalar_one_or_none()
    if match is None:
        return None, "Match not found"
    if {match.team_a_id, match.team_b_id} != {fixture.team_a_id, fixture.team_b_id}:
        return None, "The match's teams do not match this fixture's teams."

    fixture.match_id = match_id
    await db.commit()
    await db.refresh(fixture)
    return fixture, None


async def generate_knockout(
    db: AsyncSession, tournament: Tournament, team_count: int
) -> tuple[list[TournamentFixture], str | None]:
    stage = _KNOCKOUT_STAGES[team_count]
    ranked = await ranked_team_ids(db, tournament)
    if len(ranked) < team_count:
        return [], f"This tournament needs at least {team_count} teams for that draw."

    existing = (
        (
            await db.execute(
                select(TournamentFixture).where(
                    TournamentFixture.tournament_id == tournament.id,
                    TournamentFixture.stage == stage.value,
                )
            )
        )
        .scalars()
        .all()
    )
    if any(f.match_id is not None for f in existing):
        return [], f"A {stage.value} fixture already has a linked match."
    if existing:
        await db.execute(
            delete(TournamentFixture).where(
                TournamentFixture.tournament_id == tournament.id,
                TournamentFixture.stage == stage.value,
            )
        )

    seeds = ranked[:team_count]
    created = []
    for i in range(team_count // 2):
        fixture = TournamentFixture(
            tournament_id=tournament.id,
            team_a_id=seeds[i],
            team_b_id=seeds[team_count - 1 - i],
            stage=stage.value,
            round_number=1,
        )
        db.add(fixture)
        created.append(fixture)
    await db.commit()
    for fixture in created:
        await db.refresh(fixture)
    return created, None
