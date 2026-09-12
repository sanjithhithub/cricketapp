from dataclasses import dataclass, field
from typing import Protocol

from app.scoring.enums import ExtraType, MatchFormat, WicketType

_MAX_OVERS_BY_FORMAT = {
    MatchFormat.T20: 20,
    MatchFormat.ODI: 50,
    MatchFormat.TEST: None,
}


class ScoreEngineError(ValueError):
    pass


class InningsNotFoundError(ScoreEngineError):
    pass


class MatchNotFoundError(ScoreEngineError):
    pass


class InningsEndedError(ScoreEngineError):
    pass


class BallOutOfSequenceError(ScoreEngineError):
    pass


class InvalidBatsmanError(ScoreEngineError):
    pass


class InvalidDeliveryError(ScoreEngineError):
    pass


class InvalidBattingOrderError(ScoreEngineError):
    pass


@dataclass
class DeliveryInput:
    striker_id: int
    non_striker_id: int
    bowler_id: int
    runs_batsman: int = 0
    runs_extras: int = 0
    extra_type: ExtraType = ExtraType.NONE
    wicket_type: WicketType | None = None
    dismissed_player_id: int | None = None


@dataclass
class DeliveryRecord:
    innings_id: int
    over_number: int
    ball_number: int
    striker_id: int
    non_striker_id: int
    bowler_id: int
    runs_batsman: int
    runs_extras: int
    extra_type: str
    wicket_type: str | None = None
    dismissed_player_id: int | None = None
    id: int | None = None


@dataclass
class BatsmanCard:
    player_id: int
    position: int
    runs: int = 0
    balls_faced: int = 0
    fours: int = 0
    sixes: int = 0
    strike_rate: float = 0.0
    out: bool = False
    dismissal: str | None = None
    did_not_bat: bool = False


@dataclass
class BowlerCard:
    player_id: int
    balls_bowled: int = 0
    overs: float = 0.0
    overs_str: str = "0.0"
    maidens: int = 0
    runs_conceded: int = 0
    wickets: int = 0
    economy: float = 0.0


@dataclass
class ScorecardDTO:
    innings_id: int
    match_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    total: int = 0
    wickets: int = 0
    legal_balls: int = 0
    overs_bowled: float = 0.0
    overs_bowled_str: str = "0.0"
    current_run_rate: float = 0.0
    target: int | None = None
    completed: bool = False
    end_reason: str | None = None
    batsmen: list[BatsmanCard] = field(default_factory=list)
    bowlers: list[BowlerCard] = field(default_factory=list)


class ScoreRepository(Protocol):
    async def get_innings(self, innings_id: int): ...

    async def get_match(self, match_id: int): ...

    async def list_deliveries(self, innings_id: int): ...

    async def list_batting_order(self, innings_id: int): ...

    async def add_delivery(self, delivery: DeliveryRecord) -> DeliveryRecord: ...


class _ReplayState:
    __slots__ = (
        "current_over",
        "balls_in_over",
        "legal_balls",
        "total",
        "wickets",
        "striker",
        "non_striker",
        "next_batting_position",
        "completed",
        "end_reason",
    )

    def __init__(self):
        self.current_over = 1
        self.balls_in_over = 0
        self.legal_balls = 0
        self.total = 0
        self.wickets = 0
        self.striker = None
        self.non_striker = None
        self.next_batting_position = 2
        self.completed = False
        self.end_reason = None


class ScoreEngine:
    def __init__(self, repo: ScoreRepository):
        self.repo = repo

    async def apply_delivery(
        self, innings_id: int, delivery_input: DeliveryInput
    ) -> DeliveryRecord:
        innings = await self.repo.get_innings(innings_id)
        if innings is None:
            raise InningsNotFoundError(f"Innings {innings_id} not found")
        match = await self.repo.get_match(innings.match_id)
        if match is None:
            raise MatchNotFoundError(f"Match {innings.match_id} not found")
        deliveries = await self.repo.list_deliveries(innings_id)
        order = await self.repo.list_batting_order(innings_id)
        state = self._replay(innings, match, deliveries, order)
        if state.completed:
            raise InningsEndedError(f"Innings already completed ({state.end_reason})")

        self._validate_input(delivery_input, state)
        batting_ids = {b.player_id for b in order}
        if delivery_input.bowler_id in batting_ids:
            raise InvalidDeliveryError(
                f"bowler {delivery_input.bowler_id} is in the batting order and cannot bowl"
            )

        record = DeliveryRecord(
            innings_id=innings_id,
            over_number=state.current_over,
            ball_number=state.balls_in_over + 1,
            striker_id=delivery_input.striker_id,
            non_striker_id=delivery_input.non_striker_id,
            bowler_id=delivery_input.bowler_id,
            runs_batsman=delivery_input.runs_batsman,
            runs_extras=delivery_input.runs_extras,
            extra_type=delivery_input.extra_type.value,
            wicket_type=delivery_input.wicket_type.value if delivery_input.wicket_type else None,
            dismissed_player_id=delivery_input.dismissed_player_id,
        )
        return await self.repo.add_delivery(record)

    async def is_innings_completed(self, innings_id: int) -> bool:
        innings = await self.repo.get_innings(innings_id)
        if innings is None:
            raise InningsNotFoundError(f"Innings {innings_id} not found")
        match = await self.repo.get_match(innings.match_id)
        if match is None:
            raise MatchNotFoundError(f"Match {innings.match_id} not found")
        deliveries = await self.repo.list_deliveries(innings_id)
        order = await self.repo.list_batting_order(innings_id)
        state = self._replay(innings, match, deliveries, order)
        return state.completed

    async def get_scorecard(self, innings_id: int) -> ScorecardDTO:
        innings = await self.repo.get_innings(innings_id)
        if innings is None:
            raise InningsNotFoundError(f"Innings {innings_id} not found")
        match = await self.repo.get_match(innings.match_id)
        if match is None:
            raise MatchNotFoundError(f"Match {innings.match_id} not found")
        deliveries = await self.repo.list_deliveries(innings_id)
        order = await self.repo.list_batting_order(innings_id)
        state = self._replay(innings, match, deliveries, order)

        ordered = sorted(order, key=lambda b: b.position)
        bat_stats = {
            b.player_id: {
                "runs": 0,
                "balls": 0,
                "fours": 0,
                "sixes": 0,
                "out": False,
                "dismissal": None,
            }
            for b in ordered
        }
        bowler_stats: dict[int, dict] = {}

        for d in deliveries:
            extra = self._normalize_extra(d)
            wicket = self._normalize_wicket(d)

            if d.striker_id in bat_stats:
                if extra != ExtraType.WIDE:
                    bat_stats[d.striker_id]["balls"] += 1
                bat_stats[d.striker_id]["runs"] += d.runs_batsman
                if d.runs_batsman == 4:
                    bat_stats[d.striker_id]["fours"] += 1
                if d.runs_batsman == 6:
                    bat_stats[d.striker_id]["sixes"] += 1

            bl = bowler_stats.setdefault(
                d.bowler_id, {"balls": 0, "runs": 0, "wickets": 0, "by_over": {}}
            )
            bl["balls"] += 1
            conceded = (
                d.runs_batsman + d.runs_extras
                if extra not in (ExtraType.BYE, ExtraType.LEG_BYE)
                else d.runs_batsman
            )
            bl["runs"] += conceded
            if wicket and wicket != WicketType.RUN_OUT:
                bl["wickets"] += 1
            over = bl["by_over"].setdefault(d.over_number, [0, 0])
            over[0] += 1
            over[1] += conceded

            if d.dismissed_player_id is not None and d.dismissed_player_id in bat_stats:
                bat_stats[d.dismissed_player_id]["out"] = True
                bat_stats[d.dismissed_player_id]["dismissal"] = wicket.value if wicket else "out"

        batsmen = [
            BatsmanCard(
                player_id=b.player_id,
                position=b.position,
                runs=bat_stats[b.player_id]["runs"],
                balls_faced=bat_stats[b.player_id]["balls"],
                fours=bat_stats[b.player_id]["fours"],
                sixes=bat_stats[b.player_id]["sixes"],
                strike_rate=round(
                    bat_stats[b.player_id]["runs"] * 100 / bat_stats[b.player_id]["balls"], 2
                )
                if bat_stats[b.player_id]["balls"]
                else 0.0,
                out=bat_stats[b.player_id]["out"],
                dismissal=bat_stats[b.player_id]["dismissal"],
                did_not_bat=(
                    not bat_stats[b.player_id]["out"] and bat_stats[b.player_id]["balls"] == 0
                ),
            )
            for b in ordered
        ]

        bowlers = []
        for player_id, bl in bowler_stats.items():
            full, rem = divmod(bl["balls"], 6)
            maidens = sum(1 for balls, runs in bl["by_over"].values() if balls == 6 and runs == 0)
            bowlers.append(
                BowlerCard(
                    player_id=player_id,
                    balls_bowled=bl["balls"],
                    overs=round(full + rem / 6, 2),
                    overs_str=f"{full}.{rem}",
                    maidens=maidens,
                    runs_conceded=bl["runs"],
                    wickets=bl["wickets"],
                    economy=round(bl["runs"] / (bl["balls"] / 6), 2) if bl["balls"] else 0.0,
                )
            )
        bowlers.sort(key=lambda c: c.player_id)

        full, rem = divmod(state.legal_balls, 6)
        return ScorecardDTO(
            innings_id=innings_id,
            match_id=innings.match_id,
            innings_number=innings.innings_number,
            batting_team_id=innings.batting_team_id,
            bowling_team_id=innings.bowling_team_id,
            total=state.total,
            wickets=state.wickets,
            legal_balls=state.legal_balls,
            overs_bowled=round(full + rem / 6, 2),
            overs_bowled_str=f"{full}.{rem}",
            current_run_rate=round(state.total / (state.legal_balls / 6), 2)
            if state.legal_balls
            else 0.0,
            target=innings.target,
            completed=state.completed,
            end_reason=state.end_reason,
            batsmen=batsmen,
            bowlers=bowlers,
        )

    def _validate_input(self, inp: DeliveryInput, state: _ReplayState) -> None:
        if inp.striker_id == inp.bowler_id or inp.non_striker_id == inp.bowler_id:
            raise InvalidDeliveryError("bowler cannot also be a batsman at the crease")
        if inp.striker_id != state.striker or inp.non_striker_id != state.non_striker:
            raise InvalidBatsmanError(
                f"expected striker {state.striker} / non-striker {state.non_striker}, "
                f"got {inp.striker_id} / {inp.non_striker_id}"
            )
        if inp.runs_batsman < 0 or inp.runs_extras < 0:
            raise InvalidDeliveryError("runs cannot be negative")
        if inp.extra_type == ExtraType.WIDE and inp.runs_batsman != 0:
            raise InvalidDeliveryError("runs on a wide must be recorded as extras")
        if inp.extra_type in (ExtraType.BYE, ExtraType.LEG_BYE) and inp.runs_batsman != 0:
            raise InvalidDeliveryError("bye/leg-bye runs must be recorded as extras")
        if inp.wicket_type is not None and inp.dismissed_player_id is None:
            raise InvalidDeliveryError("dismissed_player_id is required when a wicket falls")
        if inp.wicket_type is None and inp.dismissed_player_id is not None:
            raise InvalidDeliveryError("dismissed_player_id cannot be set without a wicket")
        if inp.dismissed_player_id is not None and inp.dismissed_player_id not in (
            inp.striker_id,
            inp.non_striker_id,
        ):
            raise InvalidDeliveryError("dismissed player must be the striker or non-striker")

    def _replay(
        self,
        innings,
        match,
        deliveries,
        order,
    ) -> _ReplayState:
        if len(order) < 2:
            raise InvalidBattingOrderError("batting order must contain at least two players")
        ordered = sorted(order, key=lambda b: b.position)
        state = _ReplayState()
        state.striker = ordered[0].player_id
        state.non_striker = ordered[1].player_id

        for d in deliveries:
            if d.over_number != state.current_over:
                if d.over_number != state.current_over + 1:
                    raise BallOutOfSequenceError(
                        f"expected over {state.current_over}, got delivery recorded in over {d.over_number}"
                    )
                state.current_over = d.over_number
                state.balls_in_over = 0
            if d.ball_number != state.balls_in_over + 1:
                raise BallOutOfSequenceError(
                    f"expected ball {state.balls_in_over + 1} of over {state.current_over}, "
                    f"got ball {d.ball_number}"
                )
            if d.striker_id != state.striker or d.non_striker_id != state.non_striker:
                raise InvalidBatsmanError(
                    f"delivery lists striker {d.striker_id} / non-striker {d.non_striker_id} "
                    f"but previous state has {state.striker} / {state.non_striker}"
                )

            state.total += d.runs_batsman + d.runs_extras
            extra = self._normalize_extra(d)
            legal = extra not in (ExtraType.WIDE, ExtraType.NO_BALL)
            if legal:
                state.balls_in_over += 1
                state.legal_balls += 1

            wicket = self._normalize_wicket(d)
            if wicket:
                state.wickets += 1
                if d.dismissed_player_id not in (state.striker, state.non_striker):
                    raise InvalidDeliveryError(
                        f"dismissed player {d.dismissed_player_id} was not at the crease"
                    )
                if state.wickets < 10:
                    try:
                        if d.dismissed_player_id == state.non_striker:
                            state.non_striker = self._next_batsman(ordered, state)
                        else:
                            state.striker = self._next_batsman(ordered, state)
                    except InvalidBattingOrderError:
                        state.completed = True
                        state.end_reason = "all_out"
            if not wicket and legal and (d.runs_batsman + d.runs_extras) % 2 == 1:
                state.striker, state.non_striker = state.non_striker, state.striker

            if legal and state.balls_in_over == 6:
                state.balls_in_over = 0
                state.current_over += 1
                state.striker, state.non_striker = state.non_striker, state.striker

            reason = self._end_reason(innings, match, state)
            if reason:
                state.completed = True
                state.end_reason = reason

        return state

    @staticmethod
    def _next_batsman(ordered, state: _ReplayState) -> int:
        if state.next_batting_position >= len(ordered):
            raise InvalidBattingOrderError("no batsmen remain in the batting order")
        player_id = ordered[state.next_batting_position].player_id
        state.next_batting_position += 1
        return player_id

    @staticmethod
    def _end_reason(innings, match, state: _ReplayState):
        if innings.target is not None and state.total >= innings.target:
            return "target_chased"
        if state.wickets >= 10:
            return "all_out"
        try:
            match_format = MatchFormat(str(match.match_type).upper())
        except ValueError:
            match_format = None
        max_overs = _MAX_OVERS_BY_FORMAT.get(match_format) if match_format else None
        if (
            max_overs is not None
            and state.current_over - 1 >= max_overs
            and state.balls_in_over == 0
        ):
            return "overs_complete"
        return None

    @staticmethod
    def _normalize_extra(delivery) -> ExtraType:
        value = delivery.extra_type
        return value if isinstance(value, ExtraType) else ExtraType(value)

    @staticmethod
    def _normalize_wicket(delivery) -> WicketType | None:
        value = delivery.wicket_type
        if not value:
            return None
        return value if isinstance(value, WicketType) else WicketType(value)
