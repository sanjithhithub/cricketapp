from dataclasses import dataclass, field, replace
from typing import Protocol

from app.players.identity import full_name
from app.scoring.enums import (
    DEAD_BALL_WICKETS,
    NON_BOWLER_WICKETS,
    STRIKER_ONLY_WICKETS,
    WICKETS_ON_NO_BALL,
    WICKETS_ON_WIDE,
    ExtraType,
    MatchFormat,
    WicketType,
)
from app.scoring.partnerships import (
    BatterContribution,
    Partnership,
    current_partnership,
)

_MAX_OVERS_BY_FORMAT = {
    MatchFormat.T20: 20,
    MatchFormat.ODI: 50,
    MatchFormat.TEST: None,
}


def max_overs_for(match, innings=None) -> int | None:
    """The over limit for an innings.

    A Super Over is always one over. Otherwise the match's own ``total_overs``
    wins when set - that is the customisable value a junior or reduced-overs game
    needs - and only when it is NULL does the format default apply (T20 20, ODI
    50, Test unlimited). Reading the limit through one helper keeps the scorecard,
    the per-bowler quota and the innings-end rule in agreement, so a change to the
    over count moves all three together.
    """
    if innings is not None and innings.is_super_over:
        return 1
    total = getattr(match, "total_overs", None)
    if total is not None:
        return total
    try:
        return _MAX_OVERS_BY_FORMAT.get(MatchFormat(str(match.match_type).upper()))
    except ValueError:
        return None


# Wides, no-balls and the dead-ball marker used for between-delivery
# dismissals are the only deliveries that do not count as a ball of the over.
# Everything else (normal runs, byes, leg byes) is a legal ball.
_ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE, ExtraType.NO_BALL, ExtraType.DEAD_BALL)

# Only these two extras carry a mandatory penalty run.
_PENALTY_EXTRAS = (ExtraType.WIDE, ExtraType.NO_BALL)


def _wickets_allowed_on(extra: ExtraType) -> tuple[WicketType, ...] | None:
    """The dismissals a penalty delivery can produce, or None for a normal ball.

    A wide keeps the ball live for a stumping, a run out or obstruction. A
    no-ball removes the stumping (it becomes a run out) but allows the striker
    to be out hitting the ball twice.
    """
    if extra == ExtraType.WIDE:
        return WICKETS_ON_WIDE
    if extra == ExtraType.NO_BALL:
        return WICKETS_ON_NO_BALL
    return None


@dataclass(frozen=True)
class RunComponents:
    """One delivery resolved into the run components the Laws of Cricket name.

    ``total`` is what the team received, ``runs_extras`` (``total`` minus the
    batter's runs) is what lands in the extras column, and ``completed_runs``
    is what the two batters physically ran between the wickets - the number
    strike rotation turns on. Penalty runs and boundary allowances are
    deliberately excluded from it: neither was run, so neither may move the
    batters.
    """

    extra: ExtraType
    batter_runs: int
    wides: int
    no_balls: int
    byes: int
    leg_byes: int
    # Penalty runs under Law 28 that belong to neither a wide nor a no ball.
    penalty_runs: int
    completed_runs: int
    boundary_runs: int
    total: int

    @property
    def runs_extras(self) -> int:
        """Every run the team scored that was not credited to the batter.

        Invariant: ``total == batter_runs + runs_extras`` and
        ``runs_extras == wides + no_balls + byes + leg_byes + penalty_runs``.
        """
        return self.total - self.batter_runs

    @property
    def mandatory_penalty(self) -> int:
        """The penalty runs the batters never ran.

        A wide is always worth its one penalty run; a no ball carries the
        penalty run(s) recorded in its own component (which for a delivery
        stored the old way is the whole extras figure); Law 28 penalties are
        added on top.
        """
        if self.extra is ExtraType.WIDE and self.wides > 0:
            base = 1
        elif self.extra is ExtraType.NO_BALL:
            base = self.no_balls
        else:
            base = 0
        return base + self.penalty_runs

    @property
    def legal_ball(self) -> bool:
        """A wide, a no ball and the dead-ball marker are not balls of the over."""
        return self.extra not in _ILLEGAL_BALL_EXTRAS

    @property
    def bowler_conceded(self) -> int:
        """What the bowler pays for: everything except byes and leg byes."""
        return self.batter_runs + self.wides + self.no_balls + self.penalty_runs


def delivery_components(delivery) -> RunComponents:
    """Read one delivery row (ORM object or :class:`DeliveryRecord`) as runs.

    Rows written by the engine carry their components, so they are read
    straight off. A row that carries none - one written before the components
    existed, or inserted directly rather than through the engine - is re-derived
    from ``extra_type`` plus ``runs_extras``, which is exactly how every
    delivery used to be stored. The two paths agree on the total and on the
    parity of the completed runs, so replaying an old log gives the same ends.
    """
    raw_extra = getattr(delivery, "extra_type", None)
    extra = (
        raw_extra
        if isinstance(raw_extra, ExtraType)
        else ExtraType(raw_extra or ExtraType.NONE.value)
    )

    wides = int(getattr(delivery, "runs_wides", 0) or 0)
    no_balls = int(getattr(delivery, "runs_no_balls", 0) or 0)
    byes = int(getattr(delivery, "runs_byes", 0) or 0)
    leg_byes = int(getattr(delivery, "runs_leg_byes", 0) or 0)
    penalty_runs = int(getattr(delivery, "runs_penalty", 0) or 0)

    if not (wides or no_balls or byes or leg_byes or penalty_runs):
        extras = int(getattr(delivery, "runs_extras", 0) or 0)
        wides = extras if extra is ExtraType.WIDE else 0
        no_balls = extras if extra is ExtraType.NO_BALL else 0
        byes = extras if extra is ExtraType.BYE else 0
        leg_byes = extras if extra is ExtraType.LEG_BYE else 0
        penalty_runs = 0

    batter_runs = int(getattr(delivery, "runs_batsman", 0) or 0)
    total = batter_runs + wides + no_balls + byes + leg_byes + penalty_runs

    mandatory = 0
    if extra is ExtraType.WIDE and wides > 0:
        mandatory = 1
    elif extra is ExtraType.NO_BALL:
        mandatory = no_balls
    mandatory += penalty_runs
    non_penalty = max(0, total - mandatory)

    stored_boundary = getattr(delivery, "boundary_runs", None)
    boundary_runs = (
        int(stored_boundary)
        if stored_boundary is not None
        else (non_penalty if non_penalty in (4, 6) else 0)
    )
    stored_completed = getattr(delivery, "completed_runs", None)
    completed_runs = (
        int(stored_completed)
        if stored_completed is not None
        else max(0, non_penalty - boundary_runs)
    )

    return RunComponents(
        extra=extra,
        batter_runs=batter_runs,
        wides=wides,
        no_balls=no_balls,
        byes=byes,
        leg_byes=leg_byes,
        penalty_runs=penalty_runs,
        completed_runs=completed_runs,
        boundary_runs=boundary_runs,
        total=total,
    )


def resolve_delivery_runs(inp: "DeliveryInput") -> "DeliveryInput":
    """Fill in a delivery's run components, or reject a contradictory one.

    Two shapes are accepted and both end up fully componentised:

    - **component shape** - any of ``runs_wides``/``runs_no_balls``/
      ``runs_byes``/``runs_leg_byes``/``runs_penalty`` is non-zero. This is the
      only way to say "a no ball that also went for two byes", because a single
      ``extra_type`` cannot name two extras at once. ``runs_extras`` must then
      be 0 or exactly their sum, and ``extra_type`` must name the delivery's
      primary component (a no ball beats a wide, which is what
      "no ball overrides a wide" means).
    - **legacy shape** - one ``extra_type`` and one ``runs_extras`` figure,
      which is how every delivery was recorded before. The whole figure goes
      into that one component.

    Either way the run total, the boundary allowance and the runs actually
    completed between the wickets come out the same, which is what keeps old
    innings replaying to the same ends.
    """
    extra = inp.extra_type
    wides = inp.runs_wides
    no_balls = inp.runs_no_balls
    byes = inp.runs_byes
    leg_byes = inp.runs_leg_byes
    penalty_runs = inp.runs_penalty

    # The umpire disallowing the leg byes: those runs are struck out before
    # anything else is derived, because every later step (the extras figure,
    # the components, the rotation) must see a delivery that never scored
    # them. ``runs_extras`` is reduced by the same amount, so a client that
    # sent the leg byes in either shape - as a component or as the whole
    # extras figure - ends up with the same zero-run delivery.
    runs_extras = inp.runs_extras
    if not inp.leg_byes_allowed:
        claimed_leg_byes = leg_byes or (runs_extras if extra is ExtraType.LEG_BYE else 0)
        leg_byes = 0
        runs_extras = max(0, runs_extras - claimed_leg_byes)

    given = wides or no_balls or byes or leg_byes or penalty_runs
    if extra is ExtraType.DEAD_BALL and (given or runs_extras or inp.runs_batsman):
        raise InvalidDeliveryError("a dead ball carries no runs")

    if given:
        if wides and no_balls:
            raise InvalidDeliveryError("a no ball overrides a wide - record the no ball only")
        component_sum = wides + no_balls + byes + leg_byes + penalty_runs
        if runs_extras not in (0, component_sum):
            raise InvalidDeliveryError(
                "runs_extras must equal the sum of the run components "
                f"({component_sum}), not {runs_extras}"
            )
        runs_extras = component_sum
        primary = (
            ExtraType.NO_BALL
            if no_balls
            else ExtraType.WIDE
            if wides
            else ExtraType.BYE
            if byes
            else ExtraType.LEG_BYE
            if leg_byes
            else None
        )
        if primary is not None and extra is not primary:
            raise InvalidDeliveryError(
                f"a delivery scoring {component_sum} in the {primary.value} component "
                f"must be recorded as {primary.value}, not {extra.value}"
            )
    else:
        if extra is ExtraType.WIDE:
            wides = runs_extras
        elif extra is ExtraType.NO_BALL:
            no_balls = runs_extras
        elif extra is ExtraType.BYE:
            byes = runs_extras
        elif extra is ExtraType.LEG_BYE:
            leg_byes = runs_extras
        elif extra is ExtraType.DEAD_BALL:
            wides = no_balls = byes = leg_byes = penalty_runs = 0

    component_sum = wides + no_balls + byes + leg_byes + penalty_runs
    if runs_extras != component_sum:
        if extra is ExtraType.NONE:
            raise InvalidDeliveryError(
                "extra runs require an extra type (wide, no_ball, bye, leg_bye)"
            )
        raise InvalidDeliveryError(
            "runs_extras must equal the sum of the run components "
            f"({component_sum}), not {runs_extras}"
        )

    total = inp.runs_batsman + component_sum
    mandatory = 0
    if extra is ExtraType.WIDE and wides > 0:
        mandatory = 1
    elif extra is ExtraType.NO_BALL:
        mandatory = no_balls
    mandatory += penalty_runs
    non_penalty = max(0, total - mandatory)

    boundary_runs = inp.boundary_runs
    if boundary_runs is None:
        boundary_runs = non_penalty if non_penalty in (4, 6) else 0
    else:
        if boundary_runs not in (0, 4, 6):
            raise InvalidDeliveryError("a boundary allowance must be 4 or 6 runs")
        if boundary_runs > non_penalty:
            raise InvalidDeliveryError(
                f"boundary_runs ({boundary_runs}) cannot exceed the {non_penalty} "
                "runs that were not a penalty"
            )
    completed_runs = max(0, non_penalty - boundary_runs)

    return replace(
        inp,
        runs_wides=wides,
        runs_no_balls=no_balls,
        runs_byes=byes,
        runs_leg_byes=leg_byes,
        runs_penalty=penalty_runs,
        runs_extras=runs_extras,
        boundary_runs=boundary_runs,
        completed_runs=completed_runs,
        total_team_runs=total,
    )


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
    # The fielder credited with the dismissal (catch, run out, stumping). Does
    # not affect the score at all; it is recorded only so fielding figures can
    # be attributed later.
    fielder_id: int | None = None
    # --- MCC run components (see resolve_delivery_runs) ---------------------
    # All zero means "legacy shape": the whole extras figure belongs to whatever
    # ``extra_type`` names. Non-zero means "component shape", which is the only
    # way to record e.g. a no ball that also went for byes.
    runs_wides: int = 0
    runs_no_balls: int = 0
    runs_byes: int = 0
    runs_leg_byes: int = 0
    runs_penalty: int = 0
    # Filled in by resolve_delivery_runs.
    boundary_runs: int | None = None
    completed_runs: int | None = None
    total_team_runs: int | None = None
    # Whether the batters had crossed when the ball became dead - the fact a
    # run out turns on (Law 38.3). None means "derive from the completed runs".
    batters_crossed: bool | None = None
    # The umpire disallowing the leg byes: no runs, and no change of ends.
    leg_byes_allowed: bool = True
    # Law 37.3 - wilful obstruction that prevented a catch from being taken.
    obstruction_prevented_catch: bool = False
    # Idempotency key: resubmitting the same id must not score the ball twice.
    client_request_id: str | None = None


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
    fielder_id: int | None = None
    id: int | None = None
    # Run components as recorded (see RunComponents). The completed / boundary /
    # total columns stay None on a record built by hand - a legacy log - and
    # delivery_components derives them from the extras figure instead.
    runs_wides: int = 0
    runs_no_balls: int = 0
    runs_byes: int = 0
    runs_leg_byes: int = 0
    runs_penalty: int = 0
    completed_runs: int | None = None
    boundary_runs: int | None = None
    total_team_runs: int | None = None
    batters_crossed: bool | None = None
    client_request_id: str | None = None


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
    # Human-readable dismissal, e.g. "c Keeper b Bowler" or "lbw b Bowler".
    # Optional so older consumers keep working; ``dismissal`` remains the enum.
    dismissal_text: str | None = None
    did_not_bat: bool = False


@dataclass
class FallOfWicket:
    wicket_number: int
    score: int
    player_id: int
    first_name: str | None = None
    last_name: str | None = None
    player_code: str | None = None
    over_number: int = 0
    ball_number: int = 0
    overs_str: str = "0.0"


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
    # Wides and no-balls the bowler conceded. They cost runs and do not count
    # as legal balls, so they are reported separately from the over count.
    wides: int = 0
    no_balls: int = 0


@dataclass
class PartnershipBatterDTO:
    """One batsman's share of the current stand, resolved for the client."""

    player_id: int
    name: str
    runs: int
    balls: int
    contribution_percentage: float


@dataclass
class PartnershipDTO:
    """The stand at the crease, in the shape a live scoreboard shows it.

    ``total_runs`` counts the team's runs while the pair were together (extras
    included), so it can exceed the two batsmen's own runs. A stand with no legal
    ball yet reports a 0.0 run rate rather than dividing by zero.
    """

    batter1: PartnershipBatterDTO
    batter2: PartnershipBatterDTO
    total_runs: int
    total_balls: int
    run_rate: float


@dataclass
class ScorecardDTO:
    innings_id: int
    match_id: int
    innings_number: int
    batting_team_id: int
    bowling_team_id: int
    total: int = 0
    wickets: int = 0
    # One entry per wicket that fell, in the order they fell: the score at the
    # fall (team total including this delivery), the over.ball and who fell.
    fall_of_wickets: list["FallOfWicket"] = field(default_factory=list)
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
    # The stand at the crease right now. None until two batsmen are together
    # (before the innings starts, or between a wicket and the incoming pick).
    partnership: PartnershipDTO | None = None


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
        self._state_cache = {}  # (innings_id, delivery_count) -> state

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

        # The striker is the batter who faced this ball. For a bowled, caught,
        # LBW, stumped, hit-wicket or hit-the-ball-twice dismissal only that
        # batter can be out, so never trust a client's stale picker over the
        # delivery's own snapshot.
        delivery_input = self._normalize_dismissal(delivery_input)
        # Split the runs into their components before anything is validated or
        # stored, so the schema, the engine and the row all agree on what this
        # delivery actually scored.
        delivery_input = resolve_delivery_runs(delivery_input)

        batting_ids = {b.player_id for b in order}
        self._validate_input(
            delivery_input,
            state,
            max_over_limit=self._max_over_per_player(match, innings),
            batting_ids=batting_ids,
        )
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

        # The ends only change when the batters actually ran an odd number of
        # runs. Penalties and boundary allowances were never run, so they are
        # already excluded from ``completed_runs``; an explicit ``batters_crossed``
        # (the scorer's answer at the instant a wicket fell) wins over parity.
        completed = delivery_input.completed_runs or 0
        batters_crossed = (
            delivery_input.batters_crossed
            if delivery_input.batters_crossed is not None
            else completed % 2 == 1
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
            fielder_id=delivery_input.fielder_id,
            runs_wides=delivery_input.runs_wides,
            runs_no_balls=delivery_input.runs_no_balls,
            runs_byes=delivery_input.runs_byes,
            runs_leg_byes=delivery_input.runs_leg_byes,
            runs_penalty=delivery_input.runs_penalty,
            completed_runs=completed,
            boundary_runs=delivery_input.boundary_runs or 0,
            total_team_runs=delivery_input.total_team_runs
            if delivery_input.total_team_runs is not None
            else delivery_input.runs_batsman + delivery_input.runs_extras,
            batters_crossed=batters_crossed,
            client_request_id=delivery_input.client_request_id,
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

        max_overs = max_overs_for(match, innings)

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
                is_super_over=innings.is_super_over,
                awaiting_batsman=False,
                striker_id=None,
                non_striker_id=None,
                batsmen=[],
                bowlers=[],
            )

        cache_key = (innings_id, len(deliveries))
        if cache_key in self._state_cache:
            state = self._state_cache[cache_key]
        else:
            state = self._replay(innings, match, deliveries, order)
            self._state_cache[cache_key] = state

        ordered = sorted(order, key=lambda b: b.position)
        bat_stats = {
            b.player_id: {
                "runs": 0,
                "balls": 0,
                "fours": 0,
                "sixes": 0,
                "out": False,
                "dismissal": None,
                "dismissal_bowler_id": None,
                "dismissal_fielder_id": None,
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

        # Fall-of-wicket needs the team total at the instant each wicket fell,
        # so it is tracked alongside the per-batsman stats below. The same
        # running total finds the delivery that first reaches the chase target:
        # that winning ball's wicket (if any) must not be credited to anyone,
        # mirroring _replay. Folded into this pass rather than a separate
        # pre-scan - the total was being accumulated twice over every delivery.
        running_score = 0
        fow_records: list[dict] = []
        target_reached = False
        for d in deliveries:
            extra = self._normalize_extra(d)
            wicket = self._normalize_wicket(d)
            running_score += d.runs_batsman + d.runs_extras
            winning_ball = (
                not target_reached
                and innings.target is not None
                and running_score >= innings.target
            )
            target_reached = target_reached or winning_ball

            if d.striker_id in bat_stats:
                # A wide is not faced by the batsman; a no-ball IS faced but
                # the batter scores only the runs off the bat. A dead ball is
                # not a delivery at all, so it faces nobody.
                if extra not in (ExtraType.WIDE, ExtraType.DEAD_BALL):
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
                bat_stats[d.dismissed_player_id]["dismissal_bowler_id"] = d.bowler_id
                bat_stats[d.dismissed_player_id]["dismissal_fielder_id"] = d.fielder_id
                fow_records.append(
                    {
                        "wicket_number": len(fow_records) + 1,
                        "score": running_score,
                        "player_id": d.dismissed_player_id,
                        "over_number": d.over_number,
                        "ball_number": d.ball_number,
                    }
                )

            # A dead ball is a between-deliveries dismissal marker, not a
            # delivery: the bowler neither bowled it nor is credited with it.
            if extra == ExtraType.DEAD_BALL:
                continue

            # A bowler always belongs to the fielding side. A delivery recorded
            # with a bowler from the batting side is corrupt data: credit no
            # bowling stats for it. The runs, balls and dismissal above are
            # still recorded because they genuinely happened.
            if d.bowler_id in batting_ids:
                continue

            bl = bowler_stats.setdefault(
                d.bowler_id,
                {"balls": 0, "runs": 0, "wickets": 0, "wides": 0, "no_balls": 0, "by_over": {}},
            )
            # Byes and leg byes are not charged to the bowler. Runs off the bat,
            # the wide / no-ball penalty and Law 28 penalty runs are - which is
            # what bowler_conceded() adds up, component by component, so a no
            # ball that also went for byes charges only the penalty.
            comp = delivery_components(d)
            conceded = comp.bowler_conceded
            bl["runs"] += conceded
            if extra == ExtraType.WIDE:
                bl["wides"] += 1
            elif extra == ExtraType.NO_BALL:
                bl["no_balls"] += 1
            # Wides and no-balls are charged to the bowler but are not balls of
            # the over, so a bowler's figures count LEGAL balls only. A maiden
            # therefore needs 6 legal balls and no runs — and any wide/no-ball
            # concedes runs, which automatically disqualifies that over.
            over = bl["by_over"].setdefault(d.over_number, [0, 0])
            if extra not in _ILLEGAL_BALL_EXTRAS:
                bl["balls"] += 1
                over[0] += 1
            over[1] += conceded
            if wicket and not winning_ball and wicket not in NON_BOWLER_WICKETS:
                bl["wickets"] += 1

        # The stand at the crease right now. Derived from the same delivery log
        # the scorecard is built from (see app.scoring.partnerships). Before the
        # first ball there is no delivery to replay, so the opening pair the
        # replay put on strike is used with a zeroed stand.
        stand = current_partnership(deliveries, innings.target)
        if (
            stand is None
            and not deliveries
            and state.striker is not None
            and state.non_striker is not None
        ):
            stand = Partnership(
                batter1=BatterContribution(state.striker),
                batter2=BatterContribution(state.non_striker),
            )
        partnership_player_ids = (
            [stand.batter1.player_id, stand.batter2.player_id] if stand is not None else []
        )

        # Names and codes for everyone on the card. A missing id resolves to
        # blanks rather than failing the scorecard: a deleted player should not
        # take a match's figures down with it. The bowler and fielder named in a
        # dismissal are on the card too, so "c X b Y" can be rendered without a
        # second lookup.
        dismissal_ids = [
            pid
            for stats in bat_stats.values()
            for pid in (
                stats["dismissal_bowler_id"],
                stats["dismissal_fielder_id"],
            )
            if pid is not None
        ]
        identities = await self.repo.player_identities(
            list(bat_stats) + list(bowler_stats) + partnership_player_ids + dismissal_ids
        )

        def identity_name(player_id: int | None) -> str | None:
            if player_id is None:
                return None
            first, last, _code = identities.get(player_id, (None, None, None))
            return full_name(first, last) or None

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
                dismissal_text=self._dismissal_text(
                    bat_stats[b.player_id]["dismissal"],
                    identity_name(bat_stats[b.player_id]["dismissal_bowler_id"]),
                    identity_name(bat_stats[b.player_id]["dismissal_fielder_id"]),
                ),
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
                    wides=bl["wides"],
                    no_balls=bl["no_balls"],
                )
            )
        bowlers.sort(key=lambda c: c.player_id)

        fall_of_wickets = [
            FallOfWicket(
                wicket_number=r["wicket_number"],
                score=r["score"],
                player_id=r["player_id"],
                first_name=identities.get(r["player_id"], (None, None, None))[0],
                last_name=identities.get(r["player_id"], (None, None, None))[1],
                player_code=identities.get(r["player_id"], (None, None, None))[2],
                over_number=r["over_number"],
                ball_number=r["ball_number"],
                overs_str=f"{r['over_number'] - 1}.{r['ball_number']}",
            )
            for r in fow_records
        ]

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
            is_super_over=innings.is_super_over,
            awaiting_batsman=state.awaiting_batsman and not state.completed,
            striker_id=state.striker,
            non_striker_id=state.non_striker,
            batsmen=batsmen,
            bowlers=bowlers,
            fall_of_wickets=fall_of_wickets,
            partnership=self._partnership_dto(stand, identities),
        )

    @staticmethod
    def _partnership_dto(stand: Partnership | None, identities) -> PartnershipDTO | None:
        """Resolve the id-based stand into the name-carrying DTO the client gets.

        Names are looked up here because two players may share one; the id is
        what identifies a batsman, the name is what makes the payload readable.
        """
        if stand is None:
            return None

        def card(batter: BatterContribution) -> PartnershipBatterDTO:
            first, last, _code = identities.get(batter.player_id, (None, None, None))
            return PartnershipBatterDTO(
                player_id=batter.player_id,
                name=full_name(first, last) or f"Player {batter.player_id}",
                runs=batter.runs,
                balls=batter.balls,
                contribution_percentage=stand.contribution_of(batter.player_id),
            )

        return PartnershipDTO(
            batter1=card(stand.batter1),
            batter2=card(stand.batter2),
            total_runs=stand.total_runs,
            total_balls=stand.total_balls,
            run_rate=stand.run_rate,
        )

    @staticmethod
    def _normalize_dismissal(inp: DeliveryInput) -> DeliveryInput:
        """Return a delivery whose dismissed batter matches the dismissal type.

        A bowled, caught, LBW, stumped, hit-wicket or hit-the-ball-twice
        dismissal can only remove the batter facing the ball. If a client sends
        the non-striker's id - a stale picker, or an old screen that offered
        both batters for every wicket - correct it to ``striker_id`` rather than
        recording the wicket against the wrong player.

        A run out, obstruction or retirement is left exactly as sent because
        either batter may legitimately be out and the scorer chose which one.
        Timed out is also left alone: the batter it removes is the incoming one,
        whom the scorer has just sent in to the vacant end.
        """
        if (
            inp.wicket_type in STRIKER_ONLY_WICKETS
            and inp.dismissed_player_id == inp.non_striker_id
            and inp.non_striker_id != inp.striker_id
        ):
            return replace(inp, dismissed_player_id=inp.striker_id)
        return inp

    @staticmethod
    def _dismissal_text(
        wicket: str | None,
        bowler: str | None,
        fielder: str | None,
    ) -> str | None:
        """Cricket notation for one dismissal, or None for a not-out batter.

        The scorecard keeps the raw enum in ``dismissal`` (clients and data
        migrations rely on it); this is the readable form a scorecard shows. A
        fielder is named only when the delivery actually recorded one, so a
        catch without attribution reads "caught b Bowler" rather than inventing
        a keeper.
        """
        if not wicket:
            return None
        if bowler is None:
            bowler = "the bowler"
        if wicket == WicketType.CAUGHT.value:
            return f"c {fielder} b {bowler}" if fielder else f"caught b {bowler}"
        if wicket == WicketType.BOWLED.value:
            return f"b {bowler}"
        if wicket == WicketType.LBW.value:
            return f"lbw b {bowler}"
        if wicket == WicketType.STUMPED.value:
            return f"st {fielder} b {bowler}" if fielder else f"st b {bowler}"
        if wicket == WicketType.HIT_WICKET.value:
            return f"hit wicket b {bowler}"
        if wicket == WicketType.RUN_OUT.value:
            return f"run out ({fielder})" if fielder else "run out"
        if wicket == WicketType.OBSTRUCTING_THE_FIELD.value:
            return "obstructing the field"
        if wicket == WicketType.HIT_THE_BALL_TWICE.value:
            return "hit the ball twice"
        if wicket == WicketType.RETIRED_OUT.value:
            return "retired out"
        if wicket == WicketType.TIMED_OUT.value:
            return "timed out"
        return wicket.replace("_", " ")

    def _validate_input(
        self,
        inp: DeliveryInput,
        state: _ReplayState,
        max_over_limit: int | None = None,
        batting_ids: set[int] | None = None,
    ) -> None:
        if inp.striker_id == inp.non_striker_id:
            # Two names for one end each. Without this, filling a vacancy with
            # the surviving partner (the vacant end plus the same player again)
            # would put one man at both ends: strike rotation becomes a no-op,
            # and his runs would be counted once per delivery while appearing
            # twice on the crease report.
            raise InvalidDeliveryError("striker and non-striker must be different players")
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
            # The vacancy is the one moment a new player can enter the crease, so
            # it is also the one moment the engine can insist he belongs on the
            # card. A player outside the batting order would have his runs
            # counted in the team total but land on no batsman card - a silent
            # scoring bug rather than an error the scorer can see. The real flow
            # never hits this: POST /batting-order upserts the pick into the
            # order before the delivery arrives. This rejects the client bug.
            if batting_ids is not None and incoming not in batting_ids:
                raise InvalidBatsmanError(f"player {incoming} is not in the batting order")
        if inp.runs_batsman < 0 or inp.runs_extras < 0:
            raise InvalidDeliveryError("runs cannot be negative")
        if (
            inp.extra_type == ExtraType.NONE
            and inp.runs_extras != 0
            # Law 28 penalty runs are real extras with no `extra_type` of their
            # own: they arrive as the runs_penalty component, and a delivery
            # that carries only that component is recordable with extra_type
            # "none". A legacy payload (one extras figure, no components, no
            # type) is already rejected inside resolve_delivery_runs, which
            # runs before this check, so the component test here is what keeps
            # penalty runs from being refused twice.
            and not (
                inp.runs_wides
                or inp.runs_no_balls
                or inp.runs_byes
                or inp.runs_leg_byes
                or inp.runs_penalty
            )
        ):
            raise InvalidDeliveryError(
                "extra runs require an extra type (wide, no_ball, bye, leg_bye)"
            )
        if inp.extra_type == ExtraType.WIDE and inp.runs_batsman != 0:
            raise InvalidDeliveryError("runs on a wide must be recorded as extras")
        if inp.extra_type in (ExtraType.BYE, ExtraType.LEG_BYE) and inp.runs_batsman != 0:
            raise InvalidDeliveryError("bye/leg-bye runs must be recorded as extras")
        if inp.extra_type == ExtraType.DEAD_BALL:
            # A dead ball is a marker for a dismissal between deliveries
            # (retired out, timed out). It is not a delivery at all: it carries
            # no runs, consumes no ball of the over and earns the bowler nothing.
            if inp.runs_batsman or inp.runs_extras:
                raise InvalidDeliveryError("a dead ball carries no runs")
            if inp.wicket_type not in DEAD_BALL_WICKETS:
                raise InvalidDeliveryError(
                    "a dead ball must record a retired out or timed out dismissal"
                )
        elif inp.extra_type in _PENALTY_EXTRAS and inp.runs_extras < 1:
            # A wide and a no-ball are penalty deliveries: each is worth at
            # least one extra run before any runs off the bat.
            label = inp.extra_type.value.replace("_", " ")
            raise InvalidDeliveryError(f"a {label} is worth at least 1 extra run")
        if inp.wicket_type in DEAD_BALL_WICKETS and inp.extra_type != ExtraType.DEAD_BALL:
            raise InvalidDeliveryError(
                f"a {inp.wicket_type.value.replace('_', ' ')} dismissal must be recorded "
                "as a dead ball"
            )
        if inp.wicket_type is not None and inp.dismissed_player_id is None:
            raise InvalidDeliveryError("dismissed_player_id is required when a wicket falls")
        if inp.wicket_type is None and inp.dismissed_player_id is not None:
            raise InvalidDeliveryError("dismissed_player_id cannot be set without a wicket")
        if inp.dismissed_player_id is not None and inp.dismissed_player_id not in (
            inp.striker_id,
            inp.non_striker_id,
        ):
            raise InvalidDeliveryError("dismissed player must be the striker or non-striker")
        if inp.wicket_type in STRIKER_ONLY_WICKETS and inp.dismissed_player_id not in (
            None,
            inp.striker_id,
        ):
            raise InvalidDeliveryError(
                f"a {inp.wicket_type.value.replace('_', ' ')} dismissal must be the striker"
            )
        # A wide stays live for a stumping, a run out or obstruction; a no-ball
        # removes the stumping but adds hit-the-ball-twice. Every other wicket
        # needs a ball that was actually delivered to the batsman.
        allowed_on_extra = _wickets_allowed_on(inp.extra_type)
        if (
            allowed_on_extra is not None
            and inp.wicket_type is not None
            and inp.wicket_type not in allowed_on_extra
        ):
            label = inp.extra_type.value.replace("_", " ")
            allowed = ", ".join(w.value.replace("_", " ") for w in allowed_on_extra)
            raise InvalidDeliveryError(
                f"only a {allowed} can be recorded on a {label}, not {inp.wicket_type.value}"
            )
        # Law 37.3 - wilful obstruction or distraction that prevented a catch.
        # It is given as obstructing the field (never as a catch), the bowler
        # is not credited with it, and it cannot happen on a no ball, where the
        # Law does not apply.
        if inp.obstruction_prevented_catch:
            if inp.wicket_type is not WicketType.OBSTRUCTING_THE_FIELD:
                raise InvalidDeliveryError(
                    "obstruction that prevents a catch is recorded as "
                    "obstructing the field, not as a catch"
                )
            if inp.extra_type is ExtraType.NO_BALL:
                raise InvalidDeliveryError(
                    "obstruction that prevents a catch cannot be given on a no ball"
                )
        if inp.extra_type != ExtraType.DEAD_BALL:
            # A bowler bowls a full over; the next over must be bowled by
            # someone else. Only enforced on the first ball of an over. A dead
            # ball shares the slot with the next real delivery, so it must not
            # trip these checks.
            if state.balls_in_over == 0 and state.last_over_bowler is not None:
                if inp.bowler_id == state.last_over_bowler:
                    raise InvalidDeliveryError("a bowler cannot bowl two consecutive overs")
            # A single bowler cannot bowl more than the format's per-player
            # limit (T20: 4 overs, ODI: 10). Enforced on every ball so a bowler
            # cannot even start the over that would exceed the limit.
            if max_over_limit is not None:
                max_balls = max_over_limit * 6
                if state.bowler_balls.get(inp.bowler_id, 0) >= max_balls:
                    raise InvalidDeliveryError(
                        f"bowler has bowled the maximum {max_over_limit} overs"
                    )

    @staticmethod
    def _max_over_per_player(match, innings) -> int | None:
        if innings.is_super_over:
            # A Super Over is a single over. Do not cap deliveries per bowler
            # here: wides / no-balls do not count as legal balls, so a single
            # bowler keeps bowling until 6 legal balls land or the target is hit.
            return None
        innings_overs = max_overs_for(match, innings)
        if innings_overs is None:
            return None
        # The standard quota is one fifth of the innings (T20 4, ODI 10). Custom
        # over counts scale the same way, rounded, and never below one over.
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
                #
                # The recorded pair is adopted wholesale here rather than only
                # filling the vacant slot. A log written before the current
                # end-change rules may place the surviving batsman at the
                # opposite end to the engine's expectation, and refusing it would
                # make every match recorded back then unreplayable. The recorded
                # crease is also where runs and dismissals are actually
                # attributed, so it is the authority for who faced the ball.
                vacant = self._vacant_slot(state)
                survivor = state.striker if state.striker is not None else state.non_striker
                incoming = d.non_striker_id if d.striker_id == survivor else d.striker_id
                if (
                    vacant is not None
                    and state.awaiting_batsman
                    and d.striker_id is not None
                    and d.non_striker_id is not None
                    and d.striker_id != d.non_striker_id
                    and survivor in (d.striker_id, d.non_striker_id)
                    and incoming not in state.dismissed
                ):
                    state.striker, state.non_striker = d.striker_id, d.non_striker_id
                    state.awaiting_batsman = False
                elif (
                    d.striker_id == state.non_striker
                    and d.non_striker_id == state.striker
                    and state.awaiting_batsman is False
                ):
                    # The log names exactly the two batsmen already at the crease,
                    # but reversed: a pre-rule-change log whose end-change
                    # disagreed with the engine's. Trust the recorded crease.
                    state.striker, state.non_striker = d.striker_id, d.non_striker_id
                elif (
                    state.awaiting_batsman is False
                    and d.striker_id is not None
                    and d.non_striker_id is not None
                    and d.striker_id != d.non_striker_id
                    and (state.striker in state.dismissed or state.non_striker in state.dismissed)
                ):
                    # An innings the engine treats as finished (a Super Over that
                    # has lost its 2nd wicket, or an all-out side) skips vacating
                    # the dismissed slot, so the dismissed batsman is still shown
                    # at the crease. A legacy log that kept batting anyway names
                    # the real crease on the next ball; trust it. Live scoring is
                    # unchanged - only replay accepts these extra deliveries.
                    state.striker, state.non_striker = d.striker_id, d.non_striker_id
                else:
                    raise InvalidBatsmanError(
                        f"delivery lists striker {d.striker_id} / non-striker {d.non_striker_id} "
                        f"but previous state has {state.striker} / {state.non_striker}"
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
            # change. This is NOT the team total: the penalty runs of a wide or
            # a no ball, and any boundary allowance, are runs nobody ran, so
            # they are already out of ``completed_runs``. A delivery recorded
            # with an explicit ``batters_crossed`` - the scorer's answer at the
            # instant a wicket fell - overrides the parity.
            comp = delivery_components(d)
            crossed_value = getattr(d, "batters_crossed", None)
            crossed = (
                bool(crossed_value) if crossed_value is not None else comp.completed_runs % 2 == 1
            )

            wicket = self._normalize_wicket(d)
            if wicket:
                if d.dismissed_player_id not in (state.striker, state.non_striker):
                    raise InvalidDeliveryError(
                        f"dismissed player {d.dismissed_player_id} was not at the crease"
                    )
                state.wickets += 1
                state.dismissed.add(d.dismissed_player_id)
                # Crossing decides the ends ONLY for the dismissals where it is
                # a fact of the wicket - a run out (the batter out is the one
                # whose ground was broken, Law 38.4) and obstruction. For a
                # striker-only dismissal (bowled, caught, LBW, stumped, hit
                # wicket, hit the ball twice) the Law sends the not-out batter
                # back to their ORIGINAL end - since October 2022 the incoming
                # batter always takes the end the striker was at, regardless of
                # whether the pair had crossed. Swapping first would strand the
                # survivor at the striker's end and send the new batter in
                # against the strike.
                if crossed and wicket not in STRIKER_ONLY_WICKETS:
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
        max_overs = max_overs_for(match, innings)
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
