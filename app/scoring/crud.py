from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.scoring.engine import DeliveryRecord, ScoreEngine, ScoreEngineError
from app.scoring.enums import MatchStatus
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.scoring.schemas import MatchCreate


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


async def get_match(db: AsyncSession, match_id: int):
    result = await db.execute(select(Match).where(Match.id == match_id))
    return result.scalar_one_or_none()


async def get_innings_by_number(db: AsyncSession, match_id: int, innings_number: int):
    result = await db.execute(
        select(Innings).where(
            Innings.match_id == match_id,
            Innings.innings_number == innings_number,
        )
    )
    return result.scalar_one_or_none()


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
):
    innings = Innings(
        match_id=match.id,
        batting_team_id=batting_team_id,
        bowling_team_id=bowling_team_id,
        innings_number=innings_number,
        target=target,
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


async def open_second_innings(db: AsyncSession, match: Match, first_total=None):
    first = await get_innings_by_number(db, match.id, 1)
    if first is None:
        raise ValueError("First innings not found")
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


async def set_match_completed(db: AsyncSession, match: Match):
    match.status = MatchStatus.COMPLETED.value
    await db.commit()
