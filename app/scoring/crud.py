from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.models import Player
from app.scoring.engine import DeliveryRecord, ScoreEngine, ScoreEngineError
from app.scoring.enums import MatchStatus
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.scoring.schemas import MatchCreate
from app.teams.models import PlayerTeamAssignment, Team


class ScoringRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_innings(self, innings_id: int):
        result = await self.db.execute(select(Innings).where(Innings.id == innings_id))
        return result.scalar_one_or_none()

    async def get_match(self, match_id: int):
        result = await self.db.execute(select(Match).where(Match.id == match_id))
        return result.scalar_one_or_none()

    async def list_deliveries(self, innings_id: int):
        result = await self.db.execute(
            select(Delivery).where(Delivery.innings_id == innings_id).order_by(Delivery.id)
        )
        return result.scalars().all()

    async def list_batting_order(self, innings_id: int):
        result = await self.db.execute(
            select(InningsBatsman)
            .where(InningsBatsman.innings_id == innings_id)
            .order_by(InningsBatsman.position)
        )
        return result.scalars().all()

    async def player_identities(self, player_ids) -> dict[int, tuple[str, str, str | None]]:
        """Map player id -> (first, last, code) for the ids given.

        The scorecard is built from ids, but a card that prints only ids is
        unreadable, and one that prints only names is ambiguous because two
        players may share a name. Resolving the name here - at the single place
        that has the session - keeps both available to the caller.
        """
        ids = {pid for pid in player_ids if pid is not None}
        if not ids:
            return {}
        result = await self.db.execute(
            select(Player.id, Player.first_name, Player.last_name, Player.player_code).where(
                Player.id.in_(ids)
            )
        )
        return {row[0]: (row[1], row[2], row[3]) for row in result.all()}

    async def add_delivery(self, record: DeliveryRecord):
        delivery = Delivery(
            innings_id=record.innings_id,
            over_number=record.over_number,
            ball_number=record.ball_number,
            striker_id=record.striker_id,
            non_striker_id=record.non_striker_id,
            bowler_id=record.bowler_id,
            runs_batsman=record.runs_batsman,
            runs_extras=record.runs_extras,
            extra_type=record.extra_type,
            wicket_type=record.wicket_type,
            dismissed_player_id=record.dismissed_player_id,
        )
        self.db.add(delivery)
        await self.db.flush()
        await self.db.refresh(delivery)
        return delivery


async def get_match(db: AsyncSession, match_id: int, user_id: int):
    result = await db.execute(select(Match).where(Match.id == match_id, Match.user_id == user_id))
    return result.scalar_one_or_none()


async def get_innings_by_number(db: AsyncSession, match_id: int, innings_number: int):
    result = await db.execute(
        select(Innings)
        .where(
            Innings.match_id == match_id,
            Innings.innings_number == innings_number,
        )
        .order_by(Innings.id)
    )
    return result.scalars().first()


async def get_batting_order(db: AsyncSession, innings_id: int):
    result = await db.execute(
        select(InningsBatsman)
        .where(InningsBatsman.innings_id == innings_id)
        .order_by(InningsBatsman.position)
    )
    return result.scalars().all()


async def create_scoring_match(db: AsyncSession, data: MatchCreate):
    errors = []
    if data.team_a_id == data.team_b_id:
        errors.append("Team A and Team B must be different teams")
    if data.toss_winner_id not in (data.team_a_id, data.team_b_id):
        errors.append("Toss winner must be Team A or Team B")
    if errors:
        return None, "; ".join(errors)

    match = Match(**data.model_dump())
    db.add(match)
    await db.commit()
    await db.refresh(match)
    return match, None


async def _seed_batting_order(db: AsyncSession, innings_id: int, batting_order: list[int]):
    for position, player_id in enumerate(batting_order, start=1):
        db.add(InningsBatsman(innings_id=innings_id, player_id=player_id, position=position))
    await db.commit()


async def _create_innings(
    db: AsyncSession,
    match: Match,
    innings_number: int,
    batting_team_id: int,
    bowling_team_id: int,
    target=None,
    batting_order=None,
    is_super_over: bool = False,
):
    innings = Innings(
        match_id=match.id,
        batting_team_id=batting_team_id,
        bowling_team_id=bowling_team_id,
        innings_number=innings_number,
        target=target,
        is_super_over=is_super_over,
    )
    db.add(innings)
    await db.flush()
    if batting_order:
        for position, player_id in enumerate(batting_order, start=1):
            db.add(
                InningsBatsman(
                    innings_id=innings.id,
                    player_id=player_id,
                    position=position,
                )
            )
    return innings


async def ensure_current_innings(
    db: AsyncSession,
    match: Match,
):
    if match.current_innings_number == 0:
        return None, "Innings not started. Please start the innings first"

    if match.current_innings_number == 2:
        innings = await get_innings_by_number(db, match.id, 2)
        if innings is None:
            innings = await open_second_innings(db, match, None)
    else:
        innings = await get_innings_by_number(db, match.id, match.current_innings_number)
        if innings is None:
            return None, "No innings available to score"

    order = await get_batting_order(db, innings.id)
    if not order:
        return None, "Batting order not set for this innings. Please start the innings first."
    return innings, None


async def _validate_batting_order_team(
    db: AsyncSession, batting_team_id: int, batting_order: list[int]
):
    result = await db.execute(
        select(PlayerTeamAssignment.player_id).where(
            PlayerTeamAssignment.team_id == batting_team_id,
            PlayerTeamAssignment.player_id.in_(batting_order),
        )
    )
    members = set(result.scalars().all())
    invalid = [pid for pid in batting_order if pid not in members]
    if invalid:
        return None, (
            f"Players {invalid} are not in the batting team (team {batting_team_id}). "
            "Select players from the batting side only."
        )
    return batting_team_id, None


async def start_scoring(
    db: AsyncSession,
    match: Match,
    batting_order: list[int],
):
    if not batting_order:
        return None, "batting_order is required"
    if len(batting_order) < 2:
        return None, "batting_order must list at least two players"
    if len(batting_order) > 11:
        return None, "batting_order cannot have more than 11 players"
    if len(set(batting_order)) != len(batting_order):
        return None, "batting_order cannot contain duplicate players"

    if match.current_innings_number == 0:
        if match.toss_decision == "bat":
            batting_team_id = match.toss_winner_id
        else:
            batting_team_id = (
                match.team_b_id if match.toss_winner_id == match.team_a_id else match.team_a_id
            )
        existing = await get_innings_by_number(db, match.id, 1)
        if existing is not None:
            # Never create a duplicate first innings (double submit, crash
            # after insert, ...). Reuse the recorded innings; only seed the
            # batting order if it is still empty.
            if existing.batting_team_id != batting_team_id:
                return None, (
                    "A first innings already exists for this match with a different "
                    "batting team. Reset the match before starting again."
                )
            order = await get_batting_order(db, existing.id)
            if order:
                return existing, None
            _, error = await _validate_batting_order_team(
                db, existing.batting_team_id, batting_order
            )
            if error:
                return None, error
            await _seed_batting_order(db, existing.id, batting_order)
            return existing, None

        _, error = await _validate_batting_order_team(db, batting_team_id, batting_order)
        if error:
            return None, error
        bowling_team_id = match.team_b_id if batting_team_id == match.team_a_id else match.team_a_id
        innings = await _create_innings(
            db,
            match,
            innings_number=1,
            batting_team_id=batting_team_id,
            bowling_team_id=bowling_team_id,
            batting_order=batting_order,
        )
        match.current_innings_number = 1
        match.status = MatchStatus.LIVE.value
        await db.commit()
        await db.refresh(innings)
        return innings, None

    innings = await get_innings_by_number(db, match.id, match.current_innings_number)
    if innings is None:
        if match.current_innings_number == 2:
            innings = await open_second_innings(db, match, None)
        else:
            return None, "No innings available to score"

    _, error = await _validate_batting_order_team(db, innings.batting_team_id, batting_order)
    if error:
        return None, error

    engine = ScoreEngine(ScoringRepository(db))
    try:
        completed = await engine.is_innings_completed(innings.id)
    except ScoreEngineError:
        completed = False

    if completed:
        if innings.innings_number == 1:
            second = await open_second_innings(db, match, None)
            await _seed_batting_order(db, second.id, batting_order)
            return second, None
        return None, "Match already completed"

    order = await get_batting_order(db, innings.id)
    if order:
        return None, "Batting order is already set for this innings"

    await _seed_batting_order(db, innings.id, batting_order)
    return innings, None


async def upsert_batsman_row(db: AsyncSession, innings: Innings, player_id: int):
    """Give ``player_id`` a row in the innings batting order.

    A player who is already in the order keeps his existing position, so picking
    a lower-order batter does not reshuffle the card. A squad player who was
    never in the XI is appended at the end and simply reads as ``did_not_bat``
    unless he goes in.
    """
    result = await db.execute(
        select(InningsBatsman).where(
            InningsBatsman.innings_id == innings.id,
            InningsBatsman.player_id == player_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    result = await db.execute(
        select(func.max(InningsBatsman.position)).where(InningsBatsman.innings_id == innings.id)
    )
    position = (result.scalar() or 0) + 1
    row = InningsBatsman(innings_id=innings.id, player_id=player_id, position=position)
    db.add(row)
    await db.flush()
    return row


async def add_batsman(
    db: AsyncSession,
    innings: Innings,
    player_id: int,
):
    """Pick the batsman who comes in for the next vacancy at the crease.

    Rejects only on genuine unavailability - the player is out, already at the
    crease, or not in the batting team's squad. Anything else is accepted: a
    remaining batter from the order, or a squad player who was never in the XI
    (a substitute), whichever the scorer chooses.
    """
    member = await db.execute(
        select(PlayerTeamAssignment.player_id).where(
            PlayerTeamAssignment.team_id == innings.batting_team_id,
            PlayerTeamAssignment.player_id == player_id,
        )
    )
    if member.scalar_one_or_none() is None:
        return None, "Player not in team"

    dismissed = await db.execute(
        select(Delivery.id).where(
            Delivery.innings_id == innings.id,
            Delivery.dismissed_player_id == player_id,
        )
    )
    if dismissed.scalar_one_or_none() is not None:
        return None, "Player is dismissed"

    engine = ScoreEngine(ScoringRepository(db))
    scorecard = await engine.get_scorecard(innings.id)
    if scorecard.completed:
        return None, "Innings already completed"
    if player_id in (scorecard.striker_id, scorecard.non_striker_id):
        return None, "Player is already at the crease"

    await upsert_batsman_row(db, innings, player_id)
    innings.next_batsman_id = player_id
    await db.commit()
    return innings, None


async def open_second_innings(db: AsyncSession, match: Match, first_total=None):
    first = await get_innings_by_number(db, match.id, 1)
    if first is None:
        raise ValueError("First innings not found")

    existing = await get_innings_by_number(db, match.id, 2)
    if existing is not None:
        match.current_innings_number = 2
        await db.commit()
        return existing

    if first_total is None:
        result = await db.execute(
            select(func.coalesce(func.sum(Delivery.runs_batsman + Delivery.runs_extras), 0)).where(
                Delivery.innings_id == first.id
            )
        )
        first_total = result.scalar_one()

    innings = await _create_innings(
        db,
        match,
        innings_number=2,
        batting_team_id=first.bowling_team_id,
        bowling_team_id=first.batting_team_id,
        target=first_total + 1,
    )
    match.current_innings_number = 2
    await db.commit()
    await db.refresh(innings)
    return innings


async def _super_over_innings_number(db: AsyncSession, match: Match) -> int:
    """Next innings number for a Super Over — one past the highest recorded."""
    result = await db.execute(
        select(func.max(Innings.innings_number)).where(Innings.match_id == match.id)
    )
    current = result.scalar_one() or match.current_innings_number
    return max(current, match.current_innings_number) + 1


async def open_super_over_innings(
    db: AsyncSession,
    match: Match,
    batting_team_id: int,
    bowling_team_id: int,
    target: int | None = None,
) -> Innings:
    """Create the next Super Over innings (one over per side, at most 6 legal
    balls or 2 wickets). These are separate innings numbered 3, 4, 5, ... so
    they never corrupt the normal match statistics."""
    number = await _super_over_innings_number(db, match)
    innings = await _create_innings(
        db,
        match,
        innings_number=number,
        batting_team_id=batting_team_id,
        bowling_team_id=bowling_team_id,
        target=target,
        is_super_over=True,
    )
    match.current_innings_number = number
    await db.commit()
    await db.refresh(innings)
    return innings


async def _first_innings_total(db: AsyncSession, match: Match) -> int | None:
    first = await get_innings_by_number(db, match.id, 1)
    if first is None:
        return None
    engine = ScoreEngine(ScoringRepository(db))
    try:
        card = await engine.get_scorecard(first.id)
    except ScoreEngineError:
        return None
    return card.total


async def advance_match_after_innings(
    db: AsyncSession,
    match: Match,
    innings: Innings,
    scorecard,
) -> None:
    """Decide what happens once an innings has genuinely completed.

    - Innings 1 -> open the second innings.
    - Innings 2 -> win by runs/wickets, or a tie. A tie goes to a Super Over
      when the match is configured for it (super_over_enabled); otherwise the
      match is tied.
    - Super Over setter innings (odd, >= 3) -> open the chase innings targeting
      the setter's total + 1.
    - Super Over chase innings (even, >= 4) -> the run chase wins immediately on
      reaching the target (never a tie); a fishing-level Super Over opens
      another one when super_over_repeat is configured, otherwise the match is
      tied.
    """
    if not scorecard.completed:
        return

    if innings.innings_number == 1:
        await open_second_innings(db, match, scorecard.total)
        return

    if innings.innings_number == 2:
        first_total = await _first_innings_total(db, match)
        if first_total is not None and scorecard.total == first_total:
            if match.super_over_enabled:
                first = await get_innings_by_number(db, match.id, 1)
                if first is not None:
                    await open_super_over_innings(
                        db, match, first.batting_team_id, first.bowling_team_id
                    )
                    return
        await set_match_completed(db, match)
        return

    if innings.is_super_over:
        if innings.innings_number % 2 == 1:
            # Setter innings ended: the other side chases its total + 1.
            await open_super_over_innings(
                db,
                match,
                innings.bowling_team_id,
                innings.batting_team_id,
                target=scorecard.total + 1,
            )
            return
        # Chase innings ended: reached target = win already handled by the
        # engine (end_reason target_chased), so compare totals to decide.
        setter = await get_innings_by_number(db, match.id, innings.innings_number - 1)
        if setter is None:
            await set_match_completed(db, match)
            return
        engine = ScoreEngine(ScoringRepository(db))
        try:
            setter_card = await engine.get_scorecard(setter.id)
        except ScoreEngineError:
            setter_card = None
        if setter_card is None:
            await set_match_completed(db, match)
            return
        if scorecard.total != setter_card.total:
            await set_match_completed(db, match)
            return
        # Super Over itself tied.
        if match.super_over_repeat:
            await open_super_over_innings(
                db,
                match,
                innings.batting_team_id,
                innings.bowling_team_id,
            )
            return
        await set_match_completed(db, match)
        return

    await set_match_completed(db, match)


async def _team_names(db: AsyncSession, *team_ids: int) -> dict[int, str]:
    ids = [t for t in team_ids if t is not None]
    if not ids:
        return {}
    result = await db.execute(select(Team.id, Team.name).where(Team.id.in_(ids)))
    return {row[0]: row[1] for row in result.all()}


async def _super_over_result(db: AsyncSession, match: Match) -> str | None:
    """Derive the official result from Super Over innings when they exist.

    Super Over innings are numbered 3, 4, 5, ... (odd = setter, even = chase).
    The latest Super Over chase decides the match; only a level super over that
    was NOT repeated falls through to "Match tied"."""
    engine = ScoreEngine(ScoringRepository(db))
    all_innings = await db.execute(
        select(Innings)
        .where(Innings.match_id == match.id, Innings.innings_number > 2)
        .order_by(Innings.innings_number)
    )
    super_over = list(all_innings.scalars().all())
    if not super_over:
        return None

    chase = super_over[-1]
    setter = next((i for i in super_over if i.innings_number == chase.innings_number - 1), None)
    if setter is None:
        return None
    try:
        setter_card = await engine.get_scorecard(setter.id)
        chase_card = await engine.get_scorecard(chase.id)
    except ScoreEngineError:
        return None

    names = await _team_names(db, setter.batting_team_id, chase.batting_team_id)
    if setter_card.total < chase_card.total:
        winner = names.get(chase.batting_team_id, f"Team {chase.batting_team_id}")
        return f"{winner} won the Super Over"
    if chase_card.total < setter_card.total:
        winner = names.get(setter.batting_team_id, f"Team {setter.batting_team_id}")
        return f"{winner} won the Super Over"
    return "Match tied"


async def _compute_match_result(db: AsyncSession, match: Match) -> str | None:
    """Compute the official result from the innings scorecards.

    Rules:
    - Chasing team scores MORE than the first-innings total (i.e. reaches the
      target of first_total + 1) -> chased team wins by (10 - wickets) wickets.
    - Chasing team finishes BELOW the first-innings total -> defending team
      wins by (first_total - second_total) runs.
    - Both totals equal -> "Match tied" (or a Super Over decides the match).

    Returns a human-readable message, or None if the totals cannot be
    compared (missing innings / scorecard error)."""
    first = await get_innings_by_number(db, match.id, 1)
    second = await get_innings_by_number(db, match.id, 2)
    if first is None or second is None:
        return None

    engine = ScoreEngine(ScoringRepository(db))
    try:
        first_card = await engine.get_scorecard(first.id)
        second_card = await engine.get_scorecard(second.id)
    except ScoreEngineError:
        return None

    # A Super Over was played: its result supersedes the level main-match total.
    super_result = await _super_over_result(db, match)
    if super_result is not None:
        return super_result

    names = await _team_names(db, first.batting_team_id, second.batting_team_id)

    # Reaching/passing the first-innings total means the target (total + 1)
    # was reached: the chasing team WON. This is never a tie.
    if second_card.total > first_card.total:
        winner_id = second.batting_team_id
        winner = names.get(winner_id, f"Team {winner_id}")
        wickets_left = 10 - second_card.wickets
        if wickets_left < 1:
            return f"{winner} won on the last ball"
        unit = "wicket" if wickets_left == 1 else "wickets"
        return f"{winner} won by {wickets_left} {unit}"

    # Chasing team fell short: the defending team wins by the run margin.
    if second_card.total < first_card.total:
        winner_id = first.batting_team_id
        winner = names.get(winner_id, f"Team {winner_id}")
        margin = first_card.total - second_card.total
        unit = "run" if margin == 1 else "runs"
        return f"{winner} won by {margin} {unit}"

    # Both teams finished on exactly the same total: tie.
    return "Match tied"


async def set_match_completed(db: AsyncSession, match: Match):
    match.status = MatchStatus.COMPLETED.value
    match.result = await _compute_match_result(db, match)
    await db.commit()


async def sync_match_result(db: AsyncSession, match: Match) -> str | None:
    """Recompute the official result of a completed match and persist it when
    it differs from what is stored (heals stale/manual values such as a
    leftover 'tie' picked in the create-match form)."""
    computed = await _compute_match_result(db, match)
    if computed is not None and computed != match.result:
        match.result = computed
        await db.commit()
    return match.result
