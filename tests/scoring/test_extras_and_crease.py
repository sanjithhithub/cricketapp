"""Extras, wickets and the crease after every delivery.

Every test here drives the real `ScoreEngine.apply_delivery` (no replayed
records) and then asserts the striker / non-striker the ENGINE reports, so the
frontend can rely on `striker_id` / `non_striker_id` being correct after each
ball — including extras and wickets.

Rules covered:

- normal bat runs: 1/3/5 change the ends, 0/2/4/6 keep them; always a legal ball
- wide:  team + runs, bowler + runs, batter +0, NOT a legal ball; the first
  extra run is the penalty the batters never ran, so the ends follow the runs
  beyond it
- no-ball:  at least 1 extra, team + (extra + bat runs), batter + bat runs only,
  bowler + total, NOT a legal ball, ends follow the bat runs only
- bye / leg bye:  team + runs, batter +0, bowler +0, legal ball, ends follow the
  completed runs
- all six wicket types; run out is the only wicket off a wide / no-ball
- 6 legal balls = 1 over; wides / no-balls do not count; bowler over limit is
  measured in legal balls
"""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from app.scoring.engine import (
    DeliveryInput,
    DeliveryRecord,
    InvalidBatsmanError,
    InvalidDeliveryError,
    ScoreEngine,
)
from app.scoring.enums import ExtraType, WicketType
from app.scoring.schemas import DeliveryCreate

ALL_WICKETS = [
    WicketType.BOWLED,
    WicketType.CAUGHT,
    WicketType.LBW,
    WicketType.RUN_OUT,
    WicketType.STUMPED,
    WicketType.HIT_WICKET,
]


@dataclass
class FakeMatch:
    id: int
    match_type: str = "T20"


@dataclass
class FakeInnings:
    id: int = 7
    match_id: int = 99
    innings_number: int = 1
    batting_team_id: int = 11
    bowling_team_id: int = 22
    target: int | None = None
    is_super_over: bool = False


@dataclass
class FakeBatsman:
    player_id: int
    position: int


class FakeRepo:
    def __init__(self, order_ids, fmt="T20", target=None, is_super_over=False):
        self.match = FakeMatch(id=99, match_type=fmt)
        self.innings = FakeInnings(target=target, is_super_over=is_super_over)
        self.order = [
            FakeBatsman(player_id=pid, position=pos) for pos, pid in enumerate(order_ids, start=1)
        ]
        self.deliveries: list[DeliveryRecord] = []

    async def get_innings(self, innings_id):
        return self.innings if innings_id == self.innings.id else None

    async def get_match(self, match_id):
        return self.match if match_id == self.match.id else None

    async def list_deliveries(self, innings_id):
        return list(self.deliveries)

    async def list_batting_order(self, innings_id):
        return list(self.order)

    async def add_delivery(self, record):
        record.id = len(self.deliveries) + 1
        self.deliveries.append(record)
        return record

    async def player_identities(self, player_ids):
        # The stub has no players, so the scorecard carries no names. That is the
        # same as a real innings whose players have all been deleted, and the
        # card must still build.
        return {}


def make(order_ids, fmt="T20", target=None, is_super_over=False):
    repo = FakeRepo(order_ids, fmt=fmt, target=target, is_super_over=is_super_over)
    return ScoreEngine(repo), repo


async def deliver(
    engine,
    striker,
    non_striker,
    *,
    runs_batsman=0,
    runs_extras=0,
    extra_type=ExtraType.NONE,
    wicket_type=None,
    dismissed_player_id=None,
    bowler_id=200,
):
    return await engine.apply_delivery(
        engine.repo.innings.id,
        DeliveryInput(
            striker_id=striker,
            non_striker_id=non_striker,
            bowler_id=bowler_id,
            runs_batsman=runs_batsman,
            runs_extras=runs_extras,
            extra_type=extra_type,
            wicket_type=wicket_type,
            dismissed_player_id=dismissed_player_id,
        ),
    )


async def crease(engine):
    """The striker / non-striker the backend considers to be at the crease."""
    card = await engine.get_scorecard(engine.repo.innings.id)
    return (card.striker_id, card.non_striker_id)


async def ball(engine, bowler_id=200, **kwargs):
    """Record a delivery using the crease the backend last reported.

    This mirrors what the frontend does (it always submits the striker /
    non-striker from the scorecard response) and keeps the non-crease tests
    from having to hand-compute the ends after every extra.
    """
    striker, non_striker = await crease(engine)
    return await deliver(engine, striker, non_striker, bowler_id=bowler_id, **kwargs)


async def send_in(engine, incoming, *, bowler_id=200, **kwargs):
    """Record the delivery that brings `incoming` in for the fallen wicket.

    A wicket leaves the dismissed end of the crease empty. The scorer chooses
    the replacement, and that choice is recorded on the next delivery, so this
    fills the vacancy from the scorecard the way the frontend does.
    """
    striker, non_striker = await crease(engine)
    assert (striker is None) != (non_striker is None), "expected exactly one vacant end"
    if striker is None:
        striker = incoming
    else:
        non_striker = incoming
    return await deliver(engine, striker, non_striker, bowler_id=bowler_id, **kwargs)


# --- normal runs ------------------------------------------------------------


@pytest.mark.parametrize("runs", [1, 3, 5])
def test_odd_bat_runs_change_the_ends(runs):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_batsman=runs)
        assert await crease(engine) == (2, 1)
        card = await engine.get_scorecard(7)
        assert card.total == runs
        assert card.legal_balls == 1
        assert card.overs_bowled_str == "0.1"

    asyncio.run(scenario())


@pytest.mark.parametrize("runs", [0, 2, 4, 6])
def test_even_bat_runs_keep_the_ends(runs):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_batsman=runs)
        assert await crease(engine) == (1, 2)
        card = await engine.get_scorecard(7)
        assert card.total == runs
        assert card.legal_balls == 1
        b1 = next(c for c in card.batsmen if c.player_id == 1)
        assert b1.runs == runs
        assert b1.balls_faced == 1

    asyncio.run(scenario())


def test_odd_runs_on_the_last_ball_of_an_over_cancel_the_end_change():
    async def scenario():
        engine, _ = make([1, 2, 3])
        for _ in range(5):
            await deliver(engine, 1, 2)
        await deliver(engine, 1, 2, runs_batsman=1)
        # 1 run swaps the ends, then the end of the over swaps them back.
        assert await crease(engine) == (1, 2)
        card = await engine.get_scorecard(7)
        assert card.overs_bowled_str == "1.0"
        assert card.completed is False

    asyncio.run(scenario())


# --- wide -------------------------------------------------------------------


@pytest.mark.parametrize(
    "runs,expected_crease",
    [
        (1, (1, 2)),  # the penalty alone: the batters never ran
        (2, (2, 1)),  # 1 wide + 1 completed run -> crossed
        (3, (1, 2)),  # 1 wide + 2 completed runs -> even
        (4, (2, 1)),  # 1 wide + 3 completed runs -> crossed
    ],
)
def test_wide_credit_and_crease_follow_the_completed_runs(runs, expected_crease):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_extras=runs, extra_type=ExtraType.WIDE)

        assert await crease(engine) == expected_crease

        card = await engine.get_scorecard(7)
        assert card.total == runs  # team + runs
        assert card.extras == runs
        assert card.legal_balls == 0  # NOT a legal ball
        assert card.overs_bowled_str == "0.0"

        bat = next(c for c in card.batsmen if c.player_id == 1)
        assert bat.runs == 0  # batter +0
        assert bat.balls_faced == 0  # not faced

        bowler = card.bowlers[0]
        assert bowler.runs_conceded == runs  # bowler + runs
        assert bowler.balls_bowled == 0

    asyncio.run(scenario())


def test_wide_does_not_advance_the_ball_number():
    async def scenario():
        engine, _ = make([1, 2, 3])
        d1 = await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        d2 = await ball(engine)
        d3 = await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        # The leading wide and the dot both occupy slot 1.1 (the wide is not a
        # ball of the over); the trailing wide is the next slot, 1.2.
        assert (d1.ball_number, d2.ball_number, d3.ball_number) == (1, 1, 2)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 1
        assert card.overs_bowled_str == "0.1"

    asyncio.run(scenario())


def test_wide_needs_at_least_one_extra_run():
    async def scenario():
        engine, _ = make([1, 2])
        with pytest.raises(InvalidDeliveryError, match="at least 1 extra run"):
            await deliver(engine, 1, 2, runs_extras=0, extra_type=ExtraType.WIDE)

    asyncio.run(scenario())


# --- no-ball ----------------------------------------------------------------


def test_no_ball_without_bat_runs_gives_1_extra_and_keeps_the_ends():
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_extras=1, extra_type=ExtraType.NO_BALL)

        assert await crease(engine) == (1, 2)  # 0 completed runs -> same ends

        card = await engine.get_scorecard(7)
        assert card.total == 1
        assert card.extras == 1
        assert card.legal_balls == 0
        assert card.overs_bowled_str == "0.0"

        bat = next(c for c in card.batsmen if c.player_id == 1)
        assert bat.runs == 0  # batter scores only runs off the bat
        assert bat.balls_faced == 1  # but the no-ball IS faced

        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 1
        assert bowler.balls_bowled == 0

    asyncio.run(scenario())


def test_no_ball_with_bat_runs_splits_the_credit():
    async def scenario():
        engine, _ = make([1, 2, 3])
        # 1 extra + 4 off the bat = 5 to the team, all 5 to the bowler, 4 to
        # the batter. The completed runs are the 4 off the bat (the penalty is a
        # dead ball), so the ends do not change.
        await deliver(engine, 1, 2, runs_batsman=4, runs_extras=1, extra_type=ExtraType.NO_BALL)

        assert await crease(engine) == (1, 2)

        card = await engine.get_scorecard(7)
        assert card.total == 5
        assert card.extras == 1
        assert card.legal_balls == 0

        bat = next(c for c in card.batsmen if c.player_id == 1)
        assert bat.runs == 4
        assert bat.balls_faced == 1
        assert bat.fours == 1

        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 5  # bowler gets the total
        assert bowler.balls_bowled == 0

    asyncio.run(scenario())


def test_no_ball_with_odd_bat_runs_changes_the_ends():
    async def scenario():
        engine, _ = make([1, 2, 3])
        # 1 extra + 1 off the bat: the batters ran a single, so ends change.
        await deliver(engine, 1, 2, runs_batsman=1, runs_extras=1, extra_type=ExtraType.NO_BALL)
        assert await crease(engine) == (2, 1)
        card = await engine.get_scorecard(7)
        assert card.total == 2
        assert card.legal_balls == 0

    asyncio.run(scenario())


def test_no_ball_needs_at_least_one_extra_run():
    async def scenario():
        engine, _ = make([1, 2])
        with pytest.raises(InvalidDeliveryError, match="at least 1 extra run"):
            await deliver(engine, 1, 2, runs_batsman=4, extra_type=ExtraType.NO_BALL)

    asyncio.run(scenario())


# --- bye / leg bye ----------------------------------------------------------


@pytest.mark.parametrize("extra", [ExtraType.BYE, ExtraType.LEG_BYE])
@pytest.mark.parametrize("runs,expected_crease", [(0, (1, 2)), (1, (2, 1)), (2, (1, 2))])
def test_byes_and_leg_byes_credit_and_crease(extra, runs, expected_crease):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_extras=runs, extra_type=extra)

        assert await crease(engine) == expected_crease

        card = await engine.get_scorecard(7)
        assert card.total == runs  # team + runs
        assert card.extras == runs
        assert card.legal_balls == 1  # legal ball
        assert card.overs_bowled_str == "0.1"

        bat = next(c for c in card.batsmen if c.player_id == 1)
        assert bat.runs == 0  # batter +0
        assert bat.balls_faced == 1

        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 0  # bowler +0
        assert bowler.balls_bowled == 1

    asyncio.run(scenario())


# --- wickets ----------------------------------------------------------------


@pytest.mark.parametrize("wicket", ALL_WICKETS)
def test_every_wicket_type_marks_the_right_player_out(wicket):
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(
            engine,
            1,
            2,
            wicket_type=wicket,
            dismissed_player_id=1,
            bowler_id=200,
        )

        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert next(c for c in card.batsmen if c.player_id == 1).out is True
        assert next(c for c in card.batsmen if c.player_id == 1).dismissal == wicket.value
        assert next(c for c in card.batsmen if c.player_id == 2).out is False
        # The striker end is empty until the scorer sends a batsman in.
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        # A new batsman takes the striker's end.
        assert await crease(engine) == (3, 2)
        # A run out is not charged to the bowler; every other wicket is.
        assert card.bowlers[0].wickets == (0 if wicket == WicketType.RUN_OUT else 1)

    asyncio.run(scenario())


def test_wicket_of_the_non_striker_keeps_the_striker_on_strike():
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(engine, 1, 2, wicket_type=WicketType.RUN_OUT, dismissed_player_id=2)
        assert await crease(engine) == (1, None)
        await send_in(engine, 3)
        assert await crease(engine) == (1, 3)
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert next(c for c in card.batsmen if c.player_id == 2).out is True

    asyncio.run(scenario())


def test_striker_run_out_after_an_odd_run_puts_the_new_batsman_at_the_far_end():
    # 1 run completed (the batters crossed) and then the striker is run out.
    # The dismissed batsman is on strike again after crossing, so the incoming
    # batsman must go to the NON-striker end, keeping the runner on strike.
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(
            engine, 1, 2, runs_batsman=1, wicket_type=WicketType.RUN_OUT, dismissed_player_id=1
        )
        # After crossing: 2 is on strike, 1 is at the far end. 1 is out, so the
        # far end is the vacant one and 2 stays on strike.
        assert await crease(engine) == (2, None)
        await send_in(engine, 3)
        # The new batsman (3) comes in at the non-striker end, 2 stays on strike.
        assert await crease(engine) == (2, 3)
        card = await engine.get_scorecard(7)
        assert card.total == 1
        assert card.wickets == 1
        assert next(c for c in card.batsmen if c.player_id == 1).out is True

    asyncio.run(scenario())


def test_non_striker_run_out_after_an_odd_run_puts_the_new_batsman_on_strike():
    # 1 run completed, then the NON-striker is run out. After crossing the
    # original striker is at the non-striker end, so the incoming batsman takes
    # the striker end.
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(
            engine, 1, 2, runs_batsman=1, wicket_type=WicketType.RUN_OUT, dismissed_player_id=2
        )
        assert await crease(engine) == (None, 1)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 1)
        card = await engine.get_scorecard(7)
        assert card.total == 1
        assert card.wickets == 1

    asyncio.run(scenario())


def test_wicket_after_an_even_number_of_runs_does_not_change_ends():
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(
            engine, 1, 2, runs_batsman=2, wicket_type=WicketType.CAUGHT, dismissed_player_id=1
        )
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 2)
        card = await engine.get_scorecard(7)
        assert card.total == 2
        assert card.wickets == 1

    asyncio.run(scenario())


def test_a_wicket_is_credited_once_only():
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await deliver(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert sum(1 for c in card.batsmen if c.out) == 1
        assert card.bowlers[0].wickets == 1

    asyncio.run(scenario())


# --- run out off a wide / no-ball ------------------------------------------


@pytest.mark.parametrize(
    "extra,expected_crease",
    [
        # A wide's single run is the penalty, which is a dead ball: 0 completed
        # runs, so the dismissed striker is still on strike and the new batsman
        # takes the striker end. Same for a no-ball.
        (ExtraType.WIDE, (3, 2)),
        (ExtraType.NO_BALL, (3, 2)),
    ],
)
def test_run_out_on_a_penalty_extra_is_an_illegal_ball_but_counts_the_wicket(
    extra, expected_crease
):
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        await ball(
            engine,
            runs_extras=1,
            extra_type=extra,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=1,
        )

        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert card.total == 1
        assert card.legal_balls == 0  # wide / no-ball is not a legal ball
        assert card.overs_bowled_str == "0.0"
        assert next(c for c in card.batsmen if c.player_id == 1).out is True
        assert next(c for c in card.batsmen if c.player_id == 1).dismissal == "run_out"
        assert card.bowlers[0].wickets == 0  # not the bowler's wicket
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        assert await crease(engine) == expected_crease

    asyncio.run(scenario())


@pytest.mark.parametrize("extra", [ExtraType.WIDE, ExtraType.NO_BALL])
@pytest.mark.parametrize(
    "wicket", [WicketType.BOWLED, WicketType.CAUGHT, WicketType.LBW, WicketType.STUMPED]
)
def test_only_a_run_out_is_allowed_on_a_wide_or_no_ball(extra, wicket):
    async def scenario():
        engine, _ = make([1, 2, 3])
        with pytest.raises(InvalidDeliveryError, match="only a run out"):
            await deliver(
                engine,
                1,
                2,
                runs_extras=1,
                extra_type=extra,
                wicket_type=wicket,
                dismissed_player_id=1,
            )

    asyncio.run(scenario())


# --- overs ------------------------------------------------------------------


def test_six_legal_balls_complete_an_over_and_wides_do_not_count():
    async def scenario():
        engine, _ = make([1, 2, 3])
        for _ in range(3):
            await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        for _ in range(6):
            await ball(engine)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6
        assert card.overs_bowled_str == "1.0"
        assert card.completed is False
        assert card.last_over_bowler_id == 200

    asyncio.run(scenario())


def test_bowler_over_limit_counts_legal_balls_only():
    # Bowler 200 bowls 4 T20 overs and every one of them contains a wide. He
    # still gets all 4 full overs, because a wide is not a ball of the over and
    # must not eat into his quota. He is stopped only after 24 LEGAL balls.
    async def scenario():
        engine, _ = make([1, 2, 3], fmt="T20")
        for over in range(1, 8):
            bowler = 200 if over % 2 == 1 else 300
            if bowler == 200:
                await ball(engine, bowler_id=bowler, runs_extras=1, extra_type=ExtraType.WIDE)
            for _ in range(6):
                await ball(engine, bowler_id=bowler)

        card = await engine.get_scorecard(7)
        b200 = next(b for b in card.bowlers if b.player_id == 200)
        assert b200.balls_bowled == 24  # the 4 wides are not balls
        assert b200.overs_str == "4.0"
        assert b200.runs_conceded == 4  # but he did pay for them
        assert card.legal_balls == 42
        assert card.overs_bowled_str == "7.0"

        # Over 8 goes to 300, so 200 is only stopped by his own limit.
        for _ in range(6):
            await ball(engine, bowler_id=300)
        with pytest.raises(InvalidDeliveryError, match="maximum 4 overs"):
            await ball(engine, bowler_id=200)

    asyncio.run(scenario())


def test_bowler_figures_charge_wides_but_not_ball_count():
    async def scenario():
        engine, _ = make([1, 2, 3])
        for _ in range(3):
            await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        await ball(engine, runs_batsman=2)
        await ball(engine, runs_extras=1, extra_type=ExtraType.BYE)

        card = await engine.get_scorecard(7)
        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 5  # 3 wides + 2 off the bat; no bye
        assert bowler.balls_bowled == 2  # the 3 wides are not balls
        assert bowler.economy == 15.0

    asyncio.run(scenario())


def test_maiden_needs_six_legal_balls_and_no_runs():
    async def scenario():
        engine, _ = make([1, 2, 3])
        # An over of 6 dots is a maiden; a wide in the next over concedes a run
        # so that over is not a maiden even once its 6 legal balls are done.
        for _ in range(6):
            await ball(engine, bowler_id=200)
        await ball(engine, bowler_id=300, runs_extras=1, extra_type=ExtraType.WIDE)
        for _ in range(6):
            await ball(engine, bowler_id=300)

        card = await engine.get_scorecard(7)
        b200 = next(b for b in card.bowlers if b.player_id == 200)
        b300 = next(b for b in card.bowlers if b.player_id == 300)
        assert b200.maidens == 1
        assert b200.overs_str == "1.0"
        assert b300.maidens == 0  # the wide's run disqualifies the over
        assert b300.overs_str == "1.0"
        assert b300.runs_conceded == 1

    asyncio.run(scenario())


# --- the crease is the single source of truth ------------------------------


def test_engine_rejects_a_stale_crease_so_the_client_must_use_the_backend():
    async def scenario():
        engine, _ = make([1, 2, 3])
        await deliver(engine, 1, 2, runs_batsman=1)
        # The client kept 1 on strike; the engine says 2 is on strike.
        with pytest.raises(InvalidBatsmanError, match="expected striker 2"):
            await deliver(engine, 1, 2)

    asyncio.run(scenario())


def test_crease_is_correct_after_every_delivery_type():
    async def scenario():
        engine, _ = make([1, 2, 3, 4, 5, 6])
        steps = [
            # (runs_batsman, runs_extras, extra_type, wicket, dismissed)
            (1, 0, ExtraType.NONE, None, None),  # odd -> ends change
            (4, 0, ExtraType.NONE, None, None),  # even -> stay
            (0, 1, ExtraType.WIDE, None, None),  # penalty only -> stay
            (0, 2, ExtraType.WIDE, None, None),  # 1 wide + 1 run -> change
            (1, 1, ExtraType.NO_BALL, None, None),  # only the 1 off bat ran
            (0, 1, ExtraType.LEG_BYE, None, None),
            (0, 0, ExtraType.NONE, WicketType.CAUGHT, 2),
            (0, 0, ExtraType.NONE, WicketType.RUN_OUT, 3),
        ]
        striker, non_striker = 1, 2
        expected = []
        # The two wicket steps dismiss 2 then 3, so those are who comes in.
        incoming, replacement_bowler = 3, 201
        for runs_batsman, runs_extras, extra, wicket, dismissed in steps:
            await deliver(
                engine,
                striker,
                non_striker,
                runs_batsman=runs_batsman,
                runs_extras=runs_extras,
                extra_type=extra,
                wicket_type=wicket,
                dismissed_player_id=dismissed,
            )
            card = await engine.get_scorecard(7)
            if wicket is not None:
                # A wicket leaves one end empty until the scorer picks the
                # replacement, which the next delivery then records.
                assert (card.striker_id is None) != (card.non_striker_id is None)
                assert card.awaiting_batsman is True
                await send_in(engine, incoming, bowler_id=replacement_bowler)
                incoming += 1
                replacement_bowler += 1
                card = await engine.get_scorecard(7)
            assert card.striker_id is not None and card.non_striker_id is not None
            expected.append((card.striker_id, card.non_striker_id))
            # The crease the engine reports is always the crease to use next.
            striker, non_striker = card.striker_id, card.non_striker_id
        # Sanity check the walk actually moved around rather than staying put.
        assert len(set(expected)) > 1

    asyncio.run(scenario())


# --- super over -------------------------------------------------------------


def test_super_over_penalty_extras_do_not_count_as_balls():
    async def scenario():
        engine, _ = make([1, 2, 3], is_super_over=True)
        for _ in range(6):
            await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 0
        assert card.completed is False  # the over is not over yet

        await ball(engine, runs_extras=1, extra_type=ExtraType.NO_BALL)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 0
        assert card.completed is False

        # The first legal ball finally starts the over.
        await ball(engine)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 1
        assert card.overs_bowled_str == "0.1"

    asyncio.run(scenario())


def test_super_over_second_wicket_ends_the_innings_immediately():
    async def scenario():
        engine, _ = make([1, 2, 3, 4, 5], is_super_over=True)
        await deliver(
            engine, 1, 2, runs_batsman=4, wicket_type=WicketType.CAUGHT, dismissed_player_id=1
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert card.completed is False
        assert card.awaiting_batsman is True
        assert await crease(engine) == (None, 2)

        # The scorer sends 3 in; that fill counts as a legal ball.
        await send_in(engine, 3)
        assert await crease(engine) == (3, 2)

        await deliver(engine, 3, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=3)
        card = await engine.get_scorecard(7)
        assert card.wickets == 2
        assert card.legal_balls == 3
        assert card.completed is True
        assert card.end_reason == "wickets_exhausted"
        assert card.is_super_over is True
        assert card.max_overs == 1

    asyncio.run(scenario())


def test_super_over_reaching_the_target_ends_immediately():
    async def scenario():
        engine, _ = make([1, 2, 3], target=3, is_super_over=True)
        await deliver(engine, 1, 2, runs_batsman=2)
        card = await engine.get_scorecard(7)
        assert card.completed is False

        await deliver(engine, 1, 2, runs_batsman=1)
        card = await engine.get_scorecard(7)
        assert card.total == 3
        assert card.completed is True
        assert card.end_reason == "target_chased"
        assert card.wickets == 0

    asyncio.run(scenario())


def test_super_over_ends_after_six_legal_balls():
    async def scenario():
        engine, _ = make([1, 2, 3], is_super_over=True)
        striker, non_striker = 1, 2
        for _ in range(6):
            await deliver(engine, striker, non_striker, runs_batsman=1)
            striker, non_striker = non_striker, striker
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6
        assert card.overs_bowled_str == "1.0"
        assert card.completed is True
        assert card.end_reason == "overs_complete"

    asyncio.run(scenario())


# --- the API contract (what the frontend can send) --------------------------


def _delivery(**overrides):
    payload = {
        "striker_id": 1,
        "non_striker_id": 2,
        "bowler_id": 200,
        "runs_batsman": 0,
        "runs_extras": 0,
        "extra_type": "none",
        "wicket_type": None,
        "dismissed_player_id": None,
    }
    payload.update(overrides)
    return payload


def test_api_rejects_a_wide_or_no_ball_with_no_extra_run():
    for extra in ("wide", "no_ball"):
        with pytest.raises(ValidationError, match="at least 1 extra run"):
            DeliveryCreate(**_delivery(extra_type=extra, runs_extras=0))
    # One extra run is accepted.
    assert DeliveryCreate(**_delivery(extra_type="no_ball", runs_extras=1)).runs_extras == 1
    assert DeliveryCreate(**_delivery(extra_type="wide", runs_extras=1)).runs_extras == 1


def test_api_allows_only_a_run_out_on_a_wide_or_no_ball():
    for extra in ("wide", "no_ball"):
        ok = DeliveryCreate(
            **_delivery(
                extra_type=extra,
                runs_extras=1,
                wicket_type="run_out",
                dismissed_player_id=1,
            )
        )
        assert ok.wicket_type == "run_out"
        for wicket in ("bowled", "caught", "lbw", "stumped", "hit_wicket"):
            with pytest.raises(ValidationError, match="only a run out"):
                DeliveryCreate(
                    **_delivery(
                        extra_type=extra,
                        runs_extras=1,
                        wicket_type=wicket,
                        dismissed_player_id=1,
                    )
                )


def test_api_accepts_every_wicket_type_on_a_normal_delivery():
    for wicket in ("bowled", "caught", "lbw", "run_out", "stumped", "hit_wicket"):
        payload = DeliveryCreate(**_delivery(wicket_type=wicket, dismissed_player_id=1))
        assert payload.wicket_type == wicket


def test_api_still_rejects_runs_on_the_bat_for_wide_and_byes():
    with pytest.raises(ValidationError, match="recorded as extras"):
        DeliveryCreate(**_delivery(extra_type="wide", runs_extras=1, runs_batsman=1))
    for extra in ("bye", "leg_bye"):
        with pytest.raises(ValidationError, match="recorded as extras"):
            DeliveryCreate(**_delivery(extra_type=extra, runs_extras=1, runs_batsman=1))
