from dataclasses import dataclass, field
from typing import Protocol

from app.scoring.enums import ExtraType, MatchFormat, WicketType

_MAX_OVERS_BY_FORMAT = {
    MatchFormat.T20: 20,
    MatchFormat.ODI: 50,
    MatchFormat.TEST: None,
}

# Wides and no-balls are the only deliveries that do not count as a ball of
# the over. Everything else (normal runs, byes, leg byes) is a legal ball.
_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE, ExtraType.NO_BALL)

# A penalty delivery is dead by the time it reaches the batsman, so the only
# dismissal it can produce is a run out. Every other wicket needs a ball that
# was actually delivered.
_WICKETS_OFF_EXTRA = (WicketType.RUN_OUT,)

# A batsman must be dismissed in a normal innings to end it; a Super Over is
# capped at 2 wickets instead.
_NORMAL_INNINGS_WICKETS = 10
_SUPER_OVER_WICKETS = 2


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
    # Carried on the card rather than left for the client to look up by id: two
    # players may share a name, so the name alone does not identify who batted.
    # The code is what does.
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
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
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
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
    extras: int = 0
    target: int | None = None
    completed: bool = False
    end_reason: str | None = None
    max_overs: int | None = None
    last_over_bowler_id: int | None = None
    # True when a wicket fell with no batsman left to come in, so the innings
    # is paused with a vacant crease slot rather than over. A batsman still has
    # to be picked before scoring can resume. Kept under the historical name so
    # the scorecard response shape is unchanged.
    order_exhausted: bool = False
    # True for Super Over innings (max 6 legal deliveries per side).
    is_super_over: bool = False
    # True when the innings is waiting for a batsman to be picked: a wicket fell
    # and no player remains in the batting order. The vacant slot is reported as
    # None (striker_id and/or non_striker_id) so the client knows to ask.
    awaiting_batsman: bool = False
    striker_id: int | None = None
    non_striker_id: int | None = None
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
        "last_over_bowler",
        "bowler_balls",
        "awaiting_batsman",
        "dismissed",
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
        self.last_over_bowler = None
        self.bowler_balls: dict[int, int] = {}
        self.awaiting_batsman = False
        # Every player dismissed so far in this innings. A batsman filling a
        # wicket vacancy may not be one of them.
        self.dismissed: set[int] = set()


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

        self._validate_input(
            delivery_input,
            state,
            max_over_limit=self._max_over_per_player(match, innings),
        )
        batting_ids = {b.player_id for b in order}
        if delivery_input.bowler_id in batting_ids:
            raise InvalidDeliveryError(
                f"bowler {delivery_input.bowler_id} is in the batting order and cannot bowl"
            )

        # A manual pick is a one-shot "who comes in next?". It is consumed only
        # once a delivery actually puts that player at the crease - clearing it
        # any earlier would lose the pick, because the next replay would fall
        # back to the seeded order for the very wicket it was chosen for.
        picked = getattr(innings, "next_batsman_id", None)
        if picked is not None and picked in (
            delivery_input.striker_id,
            delivery_input.non_striker_id,
        ):
            innings.next_batsman_id = None

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

        try:
            max_overs = _MAX_OVERS_BY_FORMAT.get(MatchFormat(str(match.match_type).upper()))
        except ValueError:
            max_overs = None

        if len(order) < 2:
            return ScorecardDTO(
                innings_id=innings_id,
                match_id=innings.match_id,
                innings_number=innings.innings_number,
                batting_team_id=innings.batting_team_id,
                bowling_team_id=innings.bowling_team_id,
                total=0,
                wickets=0,
                legal_balls=0,
                overs_bowled=0.0,
                overs_bowled_str="0.0",
                current_run_rate=0.0,
                extras=0,
                target=innings.target,
                completed=False,
                end_reason=None,
                max_overs=1 if innings.is_super_over else max_overs,
                last_over_bowler_id=None,
                order_exhausted=False,
                is_super_over=innings.is_super_over,
                awaiting_batsman=False,
                striker_id=None,
                non_striker_id=None,
                batsmen=[],
                bowlers=[],
            )

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
        batting_ids = {b.player_id for b in ordered}
        # Everyone who ever stood at the crease. A batsman who came in as the
        # non-striker and never faced a ball has batted (did_not_bat is False).
        at_crease: set[int] = set()
        for d in deliveries:
            at_crease.add(d.striker_id)
            at_crease.add(d.non_striker_id)
        for player_id in (state.striker, state.non_striker):
            if player_id is not None:
                at_crease.add(player_id)

        # The delivery that first reaches the chase target wins the match: its
        # wicket (if any) must not be credited to anyone, mirroring _replay.
        target_delivery_id = None
        if innings.target is not None:
            running = 0
            for d in deliveries:
                running += d.runs_batsman + d.runs_extras
                if running >= innings.target:
                    target_delivery_id = d.id
                    break

        for d in deliveries:
            extra = self._normalize_extra(d)
            wicket = self._normalize_wicket(d)
            winning_ball = d.id == target_delivery_id

            if d.striker_id in bat_stats:
                # A wide is not faced by the batsman; a no-ball IS faced but
                # the batter scores only the runs off the bat.
                if extra != ExtraType.WIDE:
                    bat_stats[d.striker_id]["balls"] += 1
                bat_stats[d.striker_id]["runs"] += d.runs_batsman
                if d.runs_batsman == 4:
                    bat_stats[d.striker_id]["fours"] += 1
                if d.runs_batsman == 6:
                    bat_stats[d.striker_id]["sixes"] += 1

            if (
                d.dismissed_player_id is not None
                and not winning_ball
                and d.dismissed_player_id in bat_stats
            ):
                bat_stats[d.dismissed_player_id]["out"] = True
                bat_stats[d.dismissed_player_id]["dismissal"] = wicket.value if wicket else "out"

            # A bowler always belongs to the fielding side. A delivery recorded
            # with a bowler from the batting side is corrupt data: credit no
            # bowling stats for it. The runs, balls and dismissal above are
            # still recorded because they genuinely happened.
            if d.bowler_id in batting_ids:
                continue

            bl = bowler_stats.setdefault(
                d.bowler_id, {"balls": 0, "runs": 0, "wickets": 0, "by_over": {}}
            )
            # Byes and leg byes are not charged to the bowler. Wides and
            # no-balls are: the bowler concedes the whole delivery.
            conceded = (
                d.runs_batsman + d.runs_extras
                if extra not in (ExtraType.BYE, ExtraType.LEG_BYE)
                else d.runs_batsman
            )
            bl["runs"] += conceded
            # Wides and no-balls are charged to the bowler but are not balls of
            # the over, so a bowler's figures count LEGAL balls only. A maiden
            # therefore needs 6 legal balls and no runs — and any wide/no-ball
            # concedes runs, which automatically disqualifies that over.
            over = bl["by_over"].setdefault(d.over_number, [0, 0])
            if extra not in _ILLEGAL_BALL_EXTRAS:
                bl["balls"] += 1
                over[0] += 1
            over[1] += conceded
            if wicket and not winning_ball and wicket not in _WICKETS_OFF_EXTRA:
                bl["wickets"] += 1

        # Names and codes for everyone on the card. A missing id resolves to
        # blanks rather than failing the scorecard: a deleted player should not
        # take a match's figures down with it.
        identities = await self.repo.player_identities(list(bat_stats) + list(bowler_stats))

        batsmen = [
            BatsmanCard(
                player_id=b.player_id,
                position=b.position,
                first_name=identities.get(b.player_id, (None, None, None))[0],
                last_name=identities.get(b.player_id, (None, None, None))[1],
                player_code=identities.get(b.player_id, (None, None, None))[2],
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
                did_not_bat=b.player_id not in at_crease,
            )
            for b in ordered
        ]

        bowlers = []
        for player_id, bl in bowler_stats.items():
            full, rem = divmod(bl["balls"], 6)
            maidens = sum(1 for balls, runs in bl["by_over"].values() if balls == 6 and runs == 0)
            first, last, code = identities.get(player_id, (None, None, None))
            bowlers.append(
                BowlerCard(
                    player_id=player_id,
                    first_name=first,
                    last_name=last,
                    player_code=code,
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
            extras=sum(d.runs_extras for d in deliveries),
            target=innings.target,
            completed=state.completed,
            end_reason=state.end_reason,
            max_overs=1 if innings.is_super_over else max_overs,
            last_over_bowler_id=state.last_over_bowler,
            order_exhausted=state.awaiting_batsman and not state.completed,
            is_super_over=innings.is_super_over,
            awaiting_batsman=state.awaiting_batsman and not state.completed,
            striker_id=state.striker,
            non_striker_id=state.non_striker,
            batsmen=batsmen,
            bowlers=bowlers,
        )

    def _validate_input(
        self, inp: DeliveryInput, state: _ReplayState, max_over_limit: int | None = None
    ) -> None:
        if inp.striker_id == inp.bowler_id or inp.non_striker_id == inp.bowler_id:
            raise InvalidDeliveryError("bowler cannot also be a batsman at the crease")
        if inp.striker_id != state.striker or inp.non_striker_id != state.non_striker:
            # A wicket on the previous ball left a crease slot vacant, and this
            # delivery is what fills it: the scorer's chosen batsman is recorded
            # on the delivery itself. Any other mismatch is a client bug.
            vacant = self._vacant_slot(state) if state.awaiting_batsman else None
            if vacant is None:
                raise InvalidBatsmanError(
                    f"expected striker {state.striker} / non-striker {state.non_striker}, "
                    f"got {inp.striker_id} / {inp.non_striker_id}"
                )
            expected = state.striker if vacant == "striker" else state.non_striker
            incoming = inp.striker_id if vacant == "striker" else inp.non_striker_id
            if expected is not None and expected != incoming:
                raise InvalidBatsmanError(
                    f"expected {vacant.replace('_', '-')} {expected}, got {incoming}"
                )
            if vacant == "striker" and inp.non_striker_id != state.non_striker:
                raise InvalidBatsmanError(
                    f"expected non-striker {state.non_striker}, got {inp.non_striker_id}"
                )
            if vacant == "non_striker" and inp.striker_id != state.striker:
                raise InvalidBatsmanError(f"expected striker {state.striker}, got {inp.striker_id}")
            if incoming == inp.bowler_id:
                raise InvalidDeliveryError("bowler cannot also be a batsman at the crease")
            if incoming in state.dismissed:
                raise InvalidBatsmanError(f"player {incoming} has already been dismissed")
        if inp.runs_batsman < 0 or inp.runs_extras < 0:
            raise InvalidDeliveryError("runs cannot be negative")
        if inp.extra_type == ExtraType.NONE and inp.runs_extras != 0:
            raise InvalidDeliveryError(
                "extra runs require an extra type (wide, no_ball, bye, leg_bye)"
            )
        if inp.extra_type == ExtraType.WIDE and inp.runs_batsman != 0:
            raise InvalidDeliveryError("runs on a wide must be recorded as extras")
        if inp.extra_type in (ExtraType.BYE, ExtraType.LEG_BYE) and inp.runs_batsman != 0:
            raise InvalidDeliveryError("bye/leg-bye runs must be recorded as extras")
        if inp.extra_type in _ILLEGAL_BALL_EXTRAS and inp.runs_extras < 1:
            # A wide and a no-ball are penalty deliveries: each is worth at
            # least one extra run before any runs off the bat.
            label = inp.extra_type.value.replace("_", " ")
            raise InvalidDeliveryError(f"a {label} is worth at least 1 extra run")
        if inp.wicket_type is not None and inp.dismissed_player_id is None:
            raise InvalidDeliveryError("dismissed_player_id is required when a wicket falls")
        if inp.wicket_type is None and inp.dismissed_player_id is not None:
            raise InvalidDeliveryError("dismissed_player_id cannot be set without a wicket")
        if inp.dismissed_player_id is not None and inp.dismissed_player_id not in (
            inp.striker_id,
            inp.non_striker_id,
        ):
            raise InvalidDeliveryError("dismissed player must be the striker or non-striker")
        # A wide / no-ball is dead by the time it reaches the batsman, so the
        # only wicket it can carry is a run out.
        if (
            inp.extra_type in _ILLEGAL_BALL_EXTRAS
            and inp.wicket_type is not None
            and inp.wicket_type not in _WICKETS_OFF_EXTRA
        ):
            label = inp.extra_type.value.replace("_", " ")
            raise InvalidDeliveryError(
                f"only a run out can be recorded on a {label}, not {inp.wicket_type.value}"
            )
        # A bowler bowls a full over; the next over must be bowled by someone
        # else. Only enforced on the first ball of an over.
        if state.balls_in_over == 0 and state.last_over_bowler is not None:
            if inp.bowler_id == state.last_over_bowler:
                raise InvalidDeliveryError("a bowler cannot bowl two consecutive overs")
        # A single bowler cannot bowl more than the format's per-player limit
        # (T20: 4 overs, ODI: 10). Enforced on every ball so a bowler cannot
        # even start the over that would exceed the limit.
        if max_over_limit is not None:
            max_balls = max_over_limit * 6
            if state.bowler_balls.get(inp.bowler_id, 0) >= max_balls:
                raise InvalidDeliveryError(f"bowler has bowled the maximum {max_over_limit} overs")

    @staticmethod
    def _max_over_per_player(match, innings) -> int | None:
        if innings.is_super_over:
            # A Super Over is a single over. Do not cap deliveries per bowler
            # here: wides / no-balls do not count as legal balls, so a single
            # bowler keeps bowling until 6 legal balls land or the target is hit.
            return None
        try:
            innings_overs = _MAX_OVERS_BY_FORMAT.get(MatchFormat(str(match.match_type).upper()))
        except ValueError:
            innings_overs = None
        if innings_overs is None:
            return None
        return max(1, round(innings_overs / 5))

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
                # A wicket on the previous ball left a slot vacant and the scorer
                # chose who came in: the crease recorded on THIS delivery is that
                # choice, so it resolves the vacancy. Replay reads the answer off
                # the log instead of re-deriving it from the batting order.
                vacant = self._vacant_slot(state)
                if vacant is None or not state.awaiting_batsman:
                    raise InvalidBatsmanError(
                        f"delivery lists striker {d.striker_id} / non-striker {d.non_striker_id} "
                        f"but previous state has {state.striker} / {state.non_striker}"
                    )
                setattr(state, vacant, d.striker_id if vacant == "striker" else d.non_striker_id)
                state.awaiting_batsman = False
                if d.striker_id != state.striker or d.non_striker_id != state.non_striker:
                    raise InvalidBatsmanError(
                        f"delivery lists striker {d.striker_id} / non-striker {d.non_striker_id} "
                        f"but the other end is {state.striker} / {state.non_striker}"
                    )

            state.total += d.runs_batsman + d.runs_extras
            extra = self._normalize_extra(d)
            legal = extra not in _ILLEGAL_BALL_EXTRAS
            if legal:
                state.balls_in_over += 1
                state.legal_balls += 1
                # The per-bowler over limit counts LEGAL balls: a bowler who
                # bowls four overs containing wides has still bowled his
                # quota, and must be allowed a full set of overs.
                state.bowler_balls[d.bowler_id] = state.bowler_balls.get(d.bowler_id, 0) + 1

            # A chase ends the instant the target is reached — before overs run
            # out, before the next ball, and BEFORE a wicket on the same ball
            # is registered. Otherwise the 10th wicket landing on the winning
            # delivery is counted and the winning side is (wrongly) reported
            # with zero wickets in hand ("won by 0 wickets").
            if innings.target is not None and state.total >= innings.target:
                state.completed = True
                state.end_reason = "target_chased"
                break

            # The runs the batters actually completed decide whether the ends
            # change. This is NOT always the team total: a wide/bye/leg-bye
            # awards no runs to the striker, and the penalty run of a no-ball
            # is a dead ball the batters never ran.
            crossed = self._completed_runs(extra, d.runs_batsman, d.runs_extras) % 2 == 1

            wicket = self._normalize_wicket(d)
            if wicket:
                if d.dismissed_player_id not in (state.striker, state.non_striker):
                    raise InvalidDeliveryError(
                        f"dismissed player {d.dismissed_player_id} was not at the crease"
                    )
                state.wickets += 1
                state.dismissed.add(d.dismissed_player_id)
                # Odd completed runs mean the batters crossed for those runs
                # BEFORE the dismissal, so the striker/non-striker roles must be
                # swapped FIRST. Otherwise the incoming batsman is sent to the
                # wrong end and the striker who ran the odd run stays on strike.
                if crossed:
                    state.striker, state.non_striker = state.non_striker, state.striker
                if self._wickets_remaining(innings, state):
                    # The scorer chooses who comes in. The slot is left genuinely
                    # vacant (None) so the client is told to ask, and scoring
                    # pauses until a pick is made. The next delivery's recorded
                    # crease is what resolves it - replay never has to guess.
                    slot = (
                        "non_striker" if d.dismissed_player_id == state.non_striker else "striker"
                    )
                    setattr(state, slot, None)
                    state.awaiting_batsman = True
            elif crossed:
                state.striker, state.non_striker = state.non_striker, state.striker

            if legal and state.balls_in_over == 6:
                state.balls_in_over = 0
                state.current_over += 1
                state.last_over_bowler = d.bowler_id
                state.striker, state.non_striker = state.non_striker, state.striker

            reason = self._end_reason(innings, match, state)
            if reason:
                state.completed = True
                state.end_reason = reason

        # A pending manual pick resolves a vacancy the wicket left open. This
        # only applies when the wicket is still the last thing that happened -
        # if a later delivery already recorded the crease, the vacancy was
        # resolved from the log above and the pick has been consumed.
        picked = getattr(innings, "next_batsman_id", None)
        vacant = self._vacant_slot(state)
        if picked is not None and vacant is not None and state.awaiting_batsman:
            setattr(state, vacant, picked)
            state.awaiting_batsman = False

        return state

    @staticmethod
    def _vacant_slot(state: _ReplayState) -> str | None:
        """Which crease slot is empty, if any.

        At most one can be: a single delivery can only dismiss one of the two
        batters at the crease. Derived from the state rather than tracked, so it
        stays correct across the end-of-over change of ends.
        """
        if state.striker is None:
            return "striker"
        if state.non_striker is None:
            return "non_striker"
        return None

    @staticmethod
    def _wickets_remaining(innings, state: _ReplayState) -> bool:
        """Whether a new batsman still comes in after the wicket just recorded.

        A normal innings is all out at 10 wickets; a Super Over is done the
        moment its 2nd wicket falls, so nobody comes in for the 3rd.
        """
        limit = _SUPER_OVER_WICKETS if innings.is_super_over else _NORMAL_INNINGS_WICKETS
        return state.wickets < limit

    @staticmethod
    def _completed_runs(extra: ExtraType, runs_batsman: int, runs_extras: int) -> int:
        """Runs the batters actually completed on this delivery.

        This drives the change of ends and is deliberately NOT the team total:

        - ``none`` / ``no_ball``: the no-ball penalty is a dead ball the batters
          never ran, so only the runs off the bat were completed.
        - ``bye`` / ``leg_bye``: the striker scores none of these, but the
          batters did run, so the recorded extras are the completed runs.
        - ``wide``: the FIRST extra run is the wide penalty itself, which the
          batters never ran. Anything beyond it (chasing down a wide, a bye off
          the bat) was run by the batters. So a plain ``1wd`` completes 0 runs
          and does not change the ends.
        """
        if extra is ExtraType.WIDE:
            return max(0, runs_extras - 1)
        if extra in (ExtraType.BYE, ExtraType.LEG_BYE):
            return runs_extras
        return runs_batsman

    @staticmethod
    def _end_reason(innings, match, state: _ReplayState):
        if innings.target is not None and state.total >= innings.target:
            return "target_chased"
        if state.wickets >= 10:
            return "all_out"
        if innings.is_super_over:
            # Super Over: at most 6 LEGAL deliveries and at most 2 wickets.
            # Losing the second wicket ends the innings immediately (even with
            # legal balls still available); target_chased is checked above.
            if state.wickets >= 2:
                return "wickets_exhausted"
            if state.current_over - 1 >= 1 and state.balls_in_over == 0:
                return "overs_complete"
            return None
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
