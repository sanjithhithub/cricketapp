"""Writes for tournaments, membership and fixtures.

The read-only views (points table, leaderboards, fixture rows) live in
:mod:`app.tournaments.analytics`; this module owns creation, editing and the
generators that turn a list of entered teams into a schedule. It deliberately
imports *from* analytics rather than the reverse, so the derived layer stays
free of write concerns.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import MatchStatus
from app.matches.crud import create_match, get_match
from app.matches.models import Match
from app.matches.schemas import MatchCreate
from app.teams.models import Team
from app.tournaments.analytics import (
    _fixture_outcome,
    _match_innings,
    _scorecards,
    ranked_team_ids,
)
from app.tournaments.enums import KNOCKOUT_STAGES, FixtureStage
from app.tournaments.models import Tournament, TournamentFixture, TournamentTeam
from app.tournaments.schemas import (
    FixtureCreate,
    FixtureMatchStart,
    FixtureUpdate,
    TournamentCreate,
    TournamentUpdate,
)

# Sizes supported by the bracket generator. Non-power-of-two counts are padded
# to the next power of two and receive byes; the stage vocabulary currently tops
# out at the quarter-final, so eight is the largest draw this schema can label.
_KNOCKOUT_MAX_SIZE = 8
_KNOCKOUT_FORMATS = {"knockout", "league_knockout"}
_LEAGUE_FORMATS = {"league", "league_knockout"}
_KNOCKOUT_STAGE_VALUES = {stage.value for stage in KNOCKOUT_STAGES}


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


def _validate_schedule(
    tournament: Tournament, scheduled_date: date | None, scheduled_time: str | None
) -> str | None:
    """Keep a fixture inside the tournament's own date window.

    A fixture on a date the competition does not cover is a data error rather
    than a preference, so it is refused at the edge instead of being stored and
    discovered when the schedule is read. An unscheduled fixture is valid: the
    organiser may know the pairings before the dates are fixed.
    """
    if scheduled_time and not scheduled_date:
        return "Add a scheduled date before setting a time."

    if scheduled_date is not None:
        if tournament.start_date and scheduled_date < tournament.start_date:
            return "scheduled_date is before the tournament start date."
        if tournament.end_date and scheduled_date > tournament.end_date:
            return "scheduled_date is after the tournament end date."

    if scheduled_time:
        parts = scheduled_time.split(":")
        if len(parts) != 2:
            return "Invalid scheduled_time format. Use HH:MM."
        try:
            hours, minutes = int(parts[0]), int(parts[1])
        except ValueError:
            return "Invalid scheduled_time format. Use HH:MM."
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return "Invalid scheduled_time format. Use HH:MM."
    return None


async def _validate_fixture_dates(
    tournament: Tournament, data: FixtureCreate | FixtureUpdate
) -> str | None:
    return _validate_schedule(
        tournament,
        getattr(data, "scheduled_date", None),
        getattr(data, "scheduled_time", None),
    )


async def _counts(db: AsyncSession, tournament_id: int) -> tuple[int, int]:
    """How many teams and fixtures this tournament holds."""
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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None, "Team is already in this tournament"
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


async def _team_fixtures(
    db: AsyncSession, tournament_id: int, team_id: int
) -> list[TournamentFixture]:
    result = await db.execute(
        select(TournamentFixture)
        .where(
            TournamentFixture.tournament_id == tournament_id,
            or_(
                TournamentFixture.team_a_id == team_id,
                TournamentFixture.team_b_id == team_id,
            ),
        )
        .order_by(TournamentFixture.id)
    )
    return list(result.scalars().all())


async def remove_team(
    db: AsyncSession, tournament: Tournament, team_id: int, confirm: bool = False
) -> tuple[bool, str | None]:
    """Remove a team from a tournament without touching the team or its players.

    Membership is the only row this deletes; ``Team`` and ``Player`` history is
    never touched. A team referenced by a played fixture cannot be removed at
    all, because deleting that fixture would orphan the result. A team referenced
    only by unplayed fixtures may be removed after explicit confirmation, and
    those fixtures are deleted with it (otherwise they would point at a team no
    longer in the competition).
    """
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

    fixtures = await _team_fixtures(db, tournament.id, team_id)
    if fixtures:
        if any(fixture.match_id is not None for fixture in fixtures):
            return False, (
                "This team has a fixture linked to a match. "
                "Keep the team so the recorded result and scorecard stay intact."
            )
        # A bracket fixture is an edge in a graph; deleting it would change the
        # route to the final. Regenerating the draw is the safe operation.
        if any([await _fixture_in_bracket(db, fixture) for fixture in fixtures]):
            return False, (
                "This team is part of the knockout bracket. Regenerate the draw before removing it."
            )
        if not confirm:
            return False, (
                f"This team appears in {len(fixtures)} unplayed fixture(s). "
                "Confirm removal to delete those fixtures and remove the team."
            )

    fixture_ids = [fixture.id for fixture in fixtures]
    try:
        if fixture_ids:
            await db.execute(
                update(TournamentFixture)
                .where(TournamentFixture.next_fixture_id.in_(fixture_ids))
                .values(next_fixture_id=None, next_slot=None)
            )
            await db.execute(delete(TournamentFixture).where(TournamentFixture.id.in_(fixture_ids)))
        await db.delete(entry)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return True, None


async def _validate_fixture_teams(
    db: AsyncSession, tournament: Tournament, team_a_id: int | None, team_b_id: int | None
) -> str | None:
    """Check the two participants are distinct and entered in the tournament.

    Both slots may be empty only for a generated knockout row whose participants
    are not decided yet. A manually created or edited fixture must name both
    sides; a half-filled fixture is a plan nobody can play.
    """
    if team_a_id is None or team_b_id is None:
        if team_a_id is None and team_b_id is None:
            return "A fixture needs two teams."
        return "A fixture needs both teams, not just one."
    if team_a_id == team_b_id:
        return "A fixture needs two different teams."
    wanted = [team_a_id, team_b_id]
    entered = set(
        (
            await db.execute(
                select(TournamentTeam.team_id).where(
                    TournamentTeam.tournament_id == tournament.id,
                    TournamentTeam.team_id.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )
    for team_id in wanted:
        if team_id not in entered:
            return f"Team {team_id} is not entered in this tournament."
    return None


async def create_fixture(
    db: AsyncSession, tournament: Tournament, data: FixtureCreate
) -> tuple[TournamentFixture | None, str | None]:
    error = await _validate_fixture_teams(db, tournament, data.team_a_id, data.team_b_id)
    if error:
        return None, error
    error = _validate_schedule(tournament, data.scheduled_date, data.scheduled_time)
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


async def _tournament_team_ids(db: AsyncSession, tournament_id: int) -> list[int]:
    return list(
        (
            await db.execute(
                select(TournamentTeam.team_id)
                .where(TournamentTeam.tournament_id == tournament_id)
                .order_by(TournamentTeam.id)
            )
        )
        .scalars()
        .all()
    )


async def generate_fixtures(
    db: AsyncSession,
    tournament: Tournament,
    legs: int,
    replace: bool,
    confirm: bool = False,
) -> tuple[int, str | None]:
    """Generate the round-robin stage of a league or league-plus-knockout event.

    This generator owns the ``league`` stage only. Regenerating a
    league-plus-knockout schedule leaves the knockout bracket untouched, so a
    league result and its semi-final links survive an accidental regenerate.

    Replacement is refused if any league fixture already has a linked match. A
    played result and its scorecard are not a schedule to be thrown away; the
    operator must unlink or keep those fixtures. ``replace`` asks for the
    replacement and ``confirm`` acknowledges the destructive part, which is the
    API-level equivalent of the dialog the UI shows.
    """
    if tournament.format not in _LEAGUE_FORMATS:
        return 0, "Round-robin fixtures apply to league or league-plus-knockout tournaments."

    existing = list(
        (
            await db.execute(
                select(TournamentFixture).where(
                    TournamentFixture.tournament_id == tournament.id,
                    TournamentFixture.stage == FixtureStage.LEAGUE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        if not replace:
            return (
                0,
                "This tournament already has a league schedule. Pass replace=true to regenerate.",
            )
        if not confirm:
            return 0, (
                "Replacing an existing schedule requires explicit confirmation (confirm=true)."
            )
        if any(fixture.match_id is not None for fixture in existing):
            return 0, (
                "Cannot regenerate: one or more league fixtures already have a linked match. "
                "Keep them or unlink them before regenerating."
            )

    team_ids = await _tournament_team_ids(db, tournament.id)
    if len(team_ids) < 2:
        return 0, "Add at least two teams before generating fixtures."

    schedule = _round_robin_pairs(team_ids, legs)
    try:
        if existing:
            fixture_ids = [fixture.id for fixture in existing]
            await db.execute(
                update(TournamentFixture)
                .where(TournamentFixture.next_fixture_id.in_(fixture_ids))
                .values(next_fixture_id=None, next_slot=None)
            )
            await db.execute(delete(TournamentFixture).where(TournamentFixture.id.in_(fixture_ids)))
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
    except Exception:
        await db.rollback()
        raise
    return len(schedule), None


async def get_fixture(
    db: AsyncSession, tournament: Tournament, fixture_id: int
) -> TournamentFixture | None:
    return (
        await db.execute(
            select(TournamentFixture)
            .options(
                selectinload(TournamentFixture.team_a),
                selectinload(TournamentFixture.team_b),
                selectinload(TournamentFixture.match),
            )
            .where(
                TournamentFixture.id == fixture_id,
                TournamentFixture.tournament_id == tournament.id,
            )
        )
    ).scalar_one_or_none()


async def _fixture_in_bracket(db: AsyncSession, fixture: TournamentFixture) -> bool:
    if fixture.next_fixture_id is not None:
        return True
    target = (
        await db.execute(
            select(TournamentFixture.id).where(TournamentFixture.next_fixture_id == fixture.id)
        )
    ).first()
    return target is not None


async def update_fixture(
    db: AsyncSession, tournament: Tournament, fixture_id: int, data: FixtureUpdate
) -> tuple[TournamentFixture | None, str | None]:
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, "Fixture not found"

    updates = data.model_dump(exclude_unset=True)
    bracket = await _fixture_in_bracket(db, fixture)
    if bracket and updates.keys() & {"team_a_id", "team_b_id", "stage"}:
        return None, (
            "Knockout participants and rounds are set by the bracket. "
            "Regenerate the draw to change them."
        )
    if fixture.match_id is not None and updates.keys() & {
        "team_a_id",
        "team_b_id",
        "stage",
    }:
        return None, "Unlink the match before changing the teams or stage."

    if "team_a_id" in updates or "team_b_id" in updates:
        error = await _validate_fixture_teams(
            db,
            tournament,
            updates.get("team_a_id", fixture.team_a_id),
            updates.get("team_b_id", fixture.team_b_id),
        )
        if error:
            return None, error

    effective_date = updates.get("scheduled_date", fixture.scheduled_date)
    effective_time = updates.get("scheduled_time", fixture.scheduled_time)
    if "scheduled_date" in updates or "scheduled_time" in updates:
        error = _validate_schedule(tournament, effective_date, effective_time)
        if error:
            return None, error

    for key, value in updates.items():
        setattr(fixture, key, value)
    await db.commit()
    await db.refresh(fixture)
    return fixture, None


async def delete_fixture(
    db: AsyncSession, tournament: Tournament, fixture_id: int
) -> tuple[bool, str | None]:
    """Delete an unplayed fixture, refusing to break a result or a bracket."""
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return False, "Fixture not found"
    if fixture.match_id is not None:
        return False, "This fixture is linked to a match. Unlink it before deleting the fixture."
    if await _fixture_in_bracket(db, fixture):
        return False, (
            "This fixture is part of the knockout bracket. "
            "Regenerate the draw instead of deleting it."
        )
    await db.delete(fixture)
    await db.commit()
    return True, None


async def _winner_of_match(db: AsyncSession, match: Match) -> int | None:
    """The winning side of a completed match, or ``None`` if it has none.

    Derived with the same helpers as the points table, so the team a bracket
    advances cannot disagree with the table built from the same result.
    """
    if match.status != MatchStatus.COMPLETED.value:
        return None
    innings = await _match_innings(db, match.id)
    cards = await _scorecards(db, innings)
    kind, winner_id = _fixture_outcome(innings, cards)
    return winner_id if kind == "result" else None


async def _advance_winner(
    db: AsyncSession, fixture: TournamentFixture, winner_id: int | None
) -> None:
    """Write this fixture's winner into the slot it feeds.

    ``None`` clears the slot, which is what relinking to a drawn or abandoned
    match must do: the previous winner cannot stay in the next round.
    """
    if fixture.next_fixture_id is None or fixture.next_slot not in ("a", "b"):
        return
    next_fixture = (
        await db.execute(
            select(TournamentFixture).where(
                TournamentFixture.id == fixture.next_fixture_id,
                TournamentFixture.tournament_id == fixture.tournament_id,
            )
        )
    ).scalar_one_or_none()
    if next_fixture is None:
        return

    slot = "team_a_id" if fixture.next_slot == "a" else "team_b_id"
    other = "team_b_id" if slot == "team_a_id" else "team_a_id"
    setattr(next_fixture, slot, winner_id)
    if winner_id is not None and getattr(next_fixture, other) == winner_id:
        setattr(next_fixture, other, None)
    await db.flush()


async def _linked_match_check(
    db: AsyncSession,
    fixture: TournamentFixture,
    user_id: int,
) -> tuple[Match | None, str | None]:
    """Return the fixture's linked match after checking it still belongs here."""
    if fixture.match_id is None:
        return None, None
    match = await get_match(db, fixture.match_id, user_id)
    if match is None:
        return None, (
            "This fixture points at a match that no longer exists. "
            "Unlink the fixture before starting a new match."
        )
    if {match.team_a_id, match.team_b_id} != {fixture.team_a_id, fixture.team_b_id}:
        return None, (
            "The match linked to this fixture has different teams. "
            "Unlink it before starting a new match."
        )
    return match, None


async def start_fixture_match(
    db: AsyncSession,
    tournament: Tournament,
    fixture_id: int,
    data: FixtureMatchStart,
    user_id: int,
) -> tuple[TournamentFixture | None, Match | None, bool, str | None]:
    """Open (or create) the scoreable match for a fixture.

    The fixture is claimed with a conditional ``match_id IS NULL`` update in the
    same transaction as the new match. If two requests race, exactly one claim
    succeeds; the other rolls its match back and returns the winner's match.
    This avoids duplicates without relying on names, dates or a client retry.

    The toss is required because the existing match model requires it and
    inventing one would silently change the game's first innings.
    """
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, None, False, "Fixture not found"

    existing, error = await _linked_match_check(db, fixture, user_id)
    if error:
        return None, None, False, error
    if existing is not None:
        return fixture, existing, False, None

    if fixture.team_a_id is None or fixture.team_b_id is None:
        return None, None, False, "This fixture's participants are not decided yet."
    error = await _validate_fixture_teams(db, tournament, fixture.team_a_id, fixture.team_b_id)
    if error:
        return None, None, False, error

    if data.toss_winner_id not in {fixture.team_a_id, fixture.team_b_id}:
        return None, None, False, "The toss winner must be one of this fixture's two teams."

    match_date = data.match_date or fixture.scheduled_date
    match_time = data.match_time or fixture.scheduled_time
    venue = (data.venue or fixture.venue or "").strip()
    if match_date is None:
        return None, None, False, "Choose a match date before starting this fixture."
    if not match_time:
        return None, None, False, "Choose a match time before starting this fixture."
    if not venue:
        return None, None, False, "Enter a venue before starting this fixture."
    error = _validate_schedule(tournament, match_date, match_time)
    if error:
        return None, None, False, error

    match_data = MatchCreate(
        match_date=match_date,
        match_time=match_time,
        venue=venue,
        match_type=data.match_type,
        team_a_id=fixture.team_a_id,
        team_b_id=fixture.team_b_id,
        toss_winner_id=data.toss_winner_id,
        toss_decision=data.toss_decision,
        total_overs=data.total_overs,
        super_over_enabled=data.super_over_enabled,
        super_over_repeat=data.super_over_repeat,
    )
    # The match is flushed but not committed: it and the fixture claim become
    # visible together, so a failed/racing claim cannot leave an orphan match.
    match, error = await create_match(db, match_data, user_id, commit=False)
    if error:
        await db.rollback()
        return None, None, False, error

    try:
        claimed = await db.execute(
            update(TournamentFixture)
            .where(
                TournamentFixture.id == fixture.id,
                TournamentFixture.match_id.is_(None),
            )
            .values(
                match_id=match.id,
                scheduled_date=match_date,
                scheduled_time=match_time,
                venue=venue,
            )
        )
        if claimed.rowcount != 1:
            await db.rollback()
            refreshed = await get_fixture(db, tournament, fixture_id)
            if refreshed is not None and refreshed.match_id is not None:
                winner, check_error = await _linked_match_check(db, refreshed, user_id)
                if check_error:
                    return None, None, False, check_error
                if winner is not None:
                    return refreshed, winner, False, None
            return None, None, False, "This fixture already has a match. Reload and try again."
        await db.commit()
    except (IntegrityError, OperationalError):
        await db.rollback()
        refreshed = await get_fixture(db, tournament, fixture_id)
        if refreshed is not None and refreshed.match_id is not None:
            winner, check_error = await _linked_match_check(db, refreshed, user_id)
            if check_error:
                return None, None, False, check_error
            if winner is not None:
                return refreshed, winner, False, None
        return (
            None,
            None,
            False,
            "The fixture was claimed by another request. Reload and try again.",
        )

    fixture = await get_fixture(db, tournament, fixture_id)
    match = await get_match(db, match.id, user_id)
    return fixture, match, True, None


async def set_fixture_result(
    db: AsyncSession, tournament: Tournament, fixture_id: int, match_id: int, user_id: int
) -> tuple[TournamentFixture | None, str | None]:
    """Link a scored match to a fixture and advance its winner.

    A fixture whose participants are still to be decided cannot take a result.
    The match must belong to the account and be the fixture's two teams. A match
    already linked to another fixture is refused (the unique index also guards
    this). An upstream change that would rewrite a downstream fixture which has
    already been played is refused rather than silently corrupting that result.
    """
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, "Fixture not found"
    if fixture.team_a_id is None or fixture.team_b_id is None:
        return None, "This fixture's participants are not decided yet."

    match = (
        await db.execute(select(Match).where(Match.id == match_id, Match.user_id == user_id))
    ).scalar_one_or_none()
    if match is None:
        return None, "Match not found"
    if match.status != MatchStatus.COMPLETED.value:
        return None, "Only a completed match can be linked to a fixture."
    if {match.team_a_id, match.team_b_id} != {fixture.team_a_id, fixture.team_b_id}:
        return None, "The match's teams do not match this fixture's teams."

    already_linked = (
        await db.execute(
            select(TournamentFixture).where(
                TournamentFixture.match_id == match_id,
                TournamentFixture.id != fixture.id,
            )
        )
    ).scalar_one_or_none()
    if already_linked is not None:
        return None, "That match is already linked to another fixture."

    winner_id = await _winner_of_match(db, match)
    if fixture.next_fixture_id is not None and fixture.next_slot in ("a", "b"):
        next_fixture = (
            await db.execute(
                select(TournamentFixture).where(
                    TournamentFixture.id == fixture.next_fixture_id,
                    TournamentFixture.tournament_id == tournament.id,
                )
            )
        ).scalar_one_or_none()
        if next_fixture is not None and next_fixture.match_id is not None:
            slot = "team_a_id" if fixture.next_slot == "a" else "team_b_id"
            if getattr(next_fixture, slot) != winner_id:
                return None, (
                    "The next round already has a linked result. "
                    "Unlink that fixture before changing this result."
                )

    try:
        fixture.match_id = match_id
        await _advance_winner(db, fixture, winner_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None, "That match is already linked to another fixture."
    await db.refresh(fixture)
    return fixture, None


async def unlink_fixture_result(
    db: AsyncSession, tournament: Tournament, fixture_id: int
) -> tuple[TournamentFixture | None, str | None]:
    """Detach a match from a fixture without touching the match or its scorecard.

    This is the escape hatch the edit guard points at: a team/stage cannot be
    changed on a linked fixture, but the operator may deliberately unlink first.
    The match itself, its innings and its scorecard are kept, so nothing recorded
    is lost. If the winner had already been written into a downstream fixture that
    itself has a result, the unlink is refused rather than rewriting that result.
    """
    fixture = await get_fixture(db, tournament, fixture_id)
    if fixture is None:
        return None, "Fixture not found"
    if fixture.match_id is None:
        return None, "This fixture has no linked match."

    if fixture.next_fixture_id is not None and fixture.next_slot in ("a", "b"):
        next_fixture = (
            await db.execute(
                select(TournamentFixture).where(
                    TournamentFixture.id == fixture.next_fixture_id,
                    TournamentFixture.tournament_id == tournament.id,
                )
            )
        ).scalar_one_or_none()
        if next_fixture is not None and next_fixture.match_id is not None:
            return None, (
                "The next round already has a linked result. "
                "Unlink that fixture before unlinking this one."
            )
        if next_fixture is not None:
            slot = "team_a_id" if fixture.next_slot == "a" else "team_b_id"
            setattr(next_fixture, slot, None)
            await db.flush()

    fixture.match_id = None
    await db.commit()
    await db.refresh(fixture)
    return fixture, None


def _next_power_of_two(value: int) -> int:
    size = 2
    while size < value:
        size *= 2
    return size


def _bracket_stages(bracket_size: int) -> list[FixtureStage]:
    """The stages a bracket of ``bracket_size`` slots is played over."""
    if bracket_size == 2:
        return [FixtureStage.FINAL]
    if bracket_size == 4:
        return [FixtureStage.SEMIFINAL, FixtureStage.FINAL]
    return [FixtureStage.QUARTERFINAL, FixtureStage.SEMIFINAL, FixtureStage.FINAL]


def _bracket_seed_order(bracket_size: int) -> list[int]:
    """Seed numbers in bracket order, so 1 and 2 can only meet in the final.

    The order is built by mirroring the current list: ``[1]`` becomes
    ``[1, 2]``, then ``[1, 4, 2, 3]``, then ``[1, 8, 4, 5, 2, 7, 3, 6]``.
    Pairing adjacent entries gives 1v8, 4v5, 2v7, 3v6 - the standard draw.
    When only six teams enter, seeds 7 and 8 are byes: the adjacent pair with a
    bye is not played, and the present seed moves straight into the next round.
    """
    order = [1]
    while len(order) < bracket_size:
        size = len(order) * 2
        order = [seed for current in order for seed in (current, size + 1 - current)]
    return order


async def generate_knockout(
    db: AsyncSession, tournament: Tournament, team_count: int
) -> tuple[list[TournamentFixture], str | None]:
    """Replace this tournament's knockout draw with a full bracket.

    The first round is seeded from the points-table order (or entry order for a
    pure knockout, which has no table). Every later round is created immediately
    with undecided slots and linked through ``next_fixture_id``/``next_slot``, so
    a result advances its winner instead of the next round having to be rebuilt
    by hand (which could put a team in two places, or the wrong one).

    Only the bracket stages are replaced. A league-plus-knockout tournament keeps
    its league fixtures, and a hand-made qualifier/eliminator is left alone. A
    bracket that already has a linked match is not regenerated: that would throw
    away a recorded result.
    """
    if tournament.format not in _KNOCKOUT_FORMATS:
        return [], "Knockout fixtures apply to knockout or league-plus-knockout tournaments."
    if not 2 <= team_count <= _KNOCKOUT_MAX_SIZE:
        return [], "A knockout draw needs between 2 and 8 teams."

    ranked = await ranked_team_ids(db, tournament)
    if len(ranked) < team_count:
        return [], f"This tournament needs at least {team_count} teams for that draw."

    bracket_size = _next_power_of_two(team_count)
    bracket_stages = _bracket_stages(bracket_size)

    existing = list(
        (
            await db.execute(
                select(TournamentFixture).where(
                    TournamentFixture.tournament_id == tournament.id,
                    TournamentFixture.stage.in_(_KNOCKOUT_STAGE_VALUES),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(fixture.match_id is not None for fixture in existing):
        return [], "Cannot regenerate: a knockout fixture already has a linked match."

    # Replace every generated bracket stage, even when the new draw is smaller.
    # Otherwise switching an eight-team draw to a four-team draw would leave the
    # old quarter-finals behind.
    bracket_stage_values = {
        FixtureStage.QUARTERFINAL.value,
        FixtureStage.SEMIFINAL.value,
        FixtureStage.FINAL.value,
    }
    replaceable = [fixture for fixture in existing if fixture.stage in bracket_stage_values]
    seeds = ranked[:team_count]
    seed_order = _bracket_seed_order(bracket_size)

    try:
        if replaceable:
            fixture_ids = [fixture.id for fixture in replaceable]
            await db.execute(
                update(TournamentFixture)
                .where(TournamentFixture.next_fixture_id.in_(fixture_ids))
                .values(next_fixture_id=None, next_slot=None)
            )
            await db.execute(delete(TournamentFixture).where(TournamentFixture.id.in_(fixture_ids)))

        rounds: list[list[TournamentFixture]] = []
        first_round_items: list[tuple[int, TournamentFixture]] = []
        first_round_byes: list[tuple[int, int]] = []
        for round_index, stage in enumerate(bracket_stages):
            round_fixtures: list[TournamentFixture] = []
            fixtures_in_round = bracket_size >> (round_index + 1)
            for slot_index in range(fixtures_in_round):
                if round_index == 0:
                    seed_a = seed_order[slot_index * 2]
                    seed_b = seed_order[slot_index * 2 + 1]
                    team_a_id = seeds[seed_a - 1] if seed_a <= team_count else None
                    team_b_id = seeds[seed_b - 1] if seed_b <= team_count else None
                    if team_a_id is None or team_b_id is None:
                        # A bye is not a fixture. The present seed goes straight
                        # into its next-round slot; the absent seed is simply
                        # not in the draw.
                        present = team_a_id if team_a_id is not None else team_b_id
                        if present is not None:
                            first_round_byes.append((slot_index, present))
                        continue
                else:
                    # The participants are decided by the previous round.
                    team_a_id = None
                    team_b_id = None
                fixture = TournamentFixture(
                    tournament_id=tournament.id,
                    team_a_id=team_a_id,
                    team_b_id=team_b_id,
                    stage=stage.value,
                    round_number=round_index + 1,
                )
                db.add(fixture)
                round_fixtures.append(fixture)
                if round_index == 0:
                    first_round_items.append((slot_index, fixture))
            rounds.append(round_fixtures)

        # Flush once so every row has an id, then draw the edges between rounds.
        await db.flush()
        if len(rounds) > 1:
            next_round = rounds[1]
            for slot_index, fixture in first_round_items:
                target = next_round[slot_index // 2]
                fixture.next_fixture_id = target.id
                fixture.next_slot = "a" if slot_index % 2 == 0 else "b"
            for slot_index, team_id in first_round_byes:
                target = next_round[slot_index // 2]
                slot = "team_a_id" if slot_index % 2 == 0 else "team_b_id"
                setattr(target, slot, team_id)

        for round_index in range(1, len(rounds) - 1):
            next_round = rounds[round_index + 1]
            for slot_index, fixture in enumerate(rounds[round_index]):
                fixture.next_fixture_id = next_round[slot_index // 2].id
                fixture.next_slot = "a" if slot_index % 2 == 0 else "b"

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    created = [fixture for round_fixtures in rounds for fixture in round_fixtures]
    for fixture in created:
        await db.refresh(fixture)
    return created, None
