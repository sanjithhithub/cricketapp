"""MCC Law-conformant extras and strike rotation - the 25 enumerated cases.

Every engine test here drives the real ``ScoreEngine.apply_delivery`` (or
``DeliveryCreate`` for payload validation) and asserts the run COMPONENTS the
backend persisted - ``runs_wides`` / ``runs_no_balls`` / ``runs_byes`` /
``runs_leg_byes`` / ``runs_penalty`` / ``completed_runs`` / ``boundary_runs``
/ ``total_team_runs`` / ``batters_crossed`` - together with the ends the
backend reports, because the frontend must be able to trust both after every
ball.

The 25 cases:

 1. wide alone = the 1 penalty run, no rotation, not a legal ball
 2. wide + 1 completed run = 2, odd completed run crosses
 3. wide + 2 completed runs = 3, even completed run keeps the ends
 4. wide crossing the boundary = 5 (penalty 1 + boundary 4), no rotation
 5. no ball alone = 1 penalty run, no rotation, not a legal ball, faced
 6. no ball + bat runs = split credit, rotation follows the bat runs
 7. no ball + byes = component split, bowler charged only the penalty
 8. no ball + wide on one delivery = refused ("a no ball overrides a wide")
 9. byes = legal, no batter/bowler credit, rotation on completed runs
10. leg byes = legal, no batter/bowler credit, rotation on completed runs
11. leg byes disallowed by the umpire = no runs, positions restored
12. dot ball = no runs, legal, no rotation
13. bat runs 0/2/4/6 keep the ends, 1/3/5 change them
14. boundaries are an allowance, not runs between the wickets
15. explicit boundary_runs + invalid boundary values refused
16. odd/even runs on the LAST ball of the over decide who faces next
17. six LEGAL deliveries end the over (wides/no-balls out, byes in)
18. illegal balls do not advance the ball number; byes do
19. run out of the striker: the actual dismissed batter + batters_crossed
20. run out of the non-striker: the actual dismissed batter + batters_crossed
21. run out is permitted on a wide / no ball and is never the bowler's wicket
22. bowled/caught/LBW/stumped/hit-wicket/hit-twice send the survivor back to
    his original end even when the pair had crossed
23. a wicket on the last ball of the over still ends the over
24. invalid dismissals are rejected (and a striker-only wicket of the
    non-striker is corrected to the batter who faced)
25. obstruction preventing a catch = obstructing the field; never a catch,
    never on a no ball

plus: Law 28 penalty runs, the analytics component breakdown, and the API
contract - component-shaped payloads, idempotent submission (client_request_id),
undo after an extra, and a reload showing the identical scorecard.

The helpers below are local copies of the ones in ``test_extras_and_crease``
and ``test_undo_delivery``: ``tests/`` is not a Python package, so no test
module may import another.
"""

import asyncio
from dataclasses import dataclass
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.models import User
from app.auth.security import get_current_user, hash_password
from app.database import async_session
from app.levels.models import TeamLevel
from app.main import app
from app.matches.models import Match
from app.models import City, Country, State
from app.players.models import Player
from app.scoring.analytics import build_innings_analytics
from app.scoring.engine import (
    DeliveryInput,
    DeliveryRecord,
    InvalidDeliveryError,
    ScoreEngine,
)
from app.scoring.enums import ExtraType, WicketType
from app.scoring.schemas import DeliveryCreate
from app.teams.models import PlayerTeamAssignment, Team

# --- engine harness (a fake repository, no database) ------------------------


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
    fielder_id=None,
    runs_wides=0,
    runs_no_balls=0,
    runs_byes=0,
    runs_leg_byes=0,
    runs_penalty=0,
    boundary_runs=None,
    batters_crossed=None,
    leg_byes_allowed=True,
    obstruction_prevented_catch=False,
    client_request_id=None,
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
            fielder_id=fielder_id,
            runs_wides=runs_wides,
            runs_no_balls=runs_no_balls,
            runs_byes=runs_byes,
            runs_leg_byes=runs_leg_byes,
            runs_penalty=runs_penalty,
            boundary_runs=boundary_runs,
            batters_crossed=batters_crossed,
            leg_byes_allowed=leg_byes_allowed,
            obstruction_prevented_catch=obstruction_prevented_catch,
            client_request_id=client_request_id,
        ),
    )


async def crease(engine):
    """The striker / non-striker the backend considers to be at the crease."""
    card = await engine.get_scorecard(engine.repo.innings.id)
    return (card.striker_id, card.non_striker_id)


async def ball(engine, bowler_id=200, **kwargs):
    """Record a delivery using the crease the backend last reported."""
    striker, non_striker = await crease(engine)
    return await deliver(engine, striker, non_striker, bowler_id=bowler_id, **kwargs)


async def send_in(engine, incoming, *, bowler_id=200, **kwargs):
    """Record the delivery that brings `incoming` in for the fallen wicket."""
    striker, non_striker = await crease(engine)
    assert (striker is None) != (non_striker is None), "expected exactly one vacant end"
    if striker is None:
        striker = incoming
    else:
        non_striker = incoming
    return await deliver(engine, striker, non_striker, bowler_id=bowler_id, **kwargs)


def batsman(card, player_id):
    return next(c for c in card.batsmen if c.player_id == player_id)


def bowler(card, player_id):
    return next(c for c in card.bowlers if c.player_id == player_id)


# --- cases 1-4: wides -------------------------------------------------------


def test_01_wide_alone_is_the_one_penalty_run_and_never_rotates():
    async def scenario():
        # Legacy shape: one extras figure plus the type.
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.total == 1
        assert card.extras == 1
        assert card.legal_balls == 0  # a wide is not a ball of the over
        assert await crease(engine) == (1, 2)  # nobody ran, nobody crossed

        rec = repo.deliveries[-1]
        assert rec.runs_wides == 1
        assert rec.runs_no_balls == rec.runs_byes == rec.runs_leg_byes == 0
        assert rec.runs_extras == 1
        assert rec.completed_runs == 0 and rec.boundary_runs == 0
        assert rec.total_team_runs == 1
        assert rec.batters_crossed is False

        # The batter neither faced it nor scored, the bowler conceded it all.
        assert batsman(card, 1).runs == 0
        assert batsman(card, 1).balls_faced == 0
        assert bowler(card, 200).runs_conceded == 1
        assert bowler(card, 200).wides == 1

    asyncio.run(scenario())


def test_02_wide_plus_one_completed_run_is_two_runs_and_crosses():
    async def scenario():
        engine, repo = make([1, 2, 3])
        # Component shape: the penalty (1) plus one run actually completed.
        await ball(engine, runs_wides=2, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.total == 2 and card.extras == 2
        assert card.legal_balls == 0
        assert await crease(engine) == (2, 1)  # odd completed run crosses

        rec = repo.deliveries[-1]
        assert rec.runs_wides == 2 and rec.completed_runs == 0 + 1
        assert rec.boundary_runs == 0 and rec.total_team_runs == 2
        assert rec.batters_crossed is True
        assert bowler(card, 200).runs_conceded == 2

    asyncio.run(scenario())


def test_03_wide_plus_two_completed_runs_is_three_runs_and_keeps_the_ends():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_wides=3, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.total == 3 and card.extras == 3
        assert await crease(engine) == (1, 2)  # even completed run

        rec = repo.deliveries[-1]
        assert rec.runs_wides == 3 and rec.completed_runs == 2
        assert rec.batters_crossed is False

    asyncio.run(scenario())


def test_04_wide_across_the_boundary_is_five_runs_and_never_rotates():
    async def scenario():
        engine, repo = make([1, 2, 3])
        # Penalty 1 + boundary allowance 4 = 5. The 4 were not run, so the
        # batters stay where they are.
        await ball(engine, runs_wides=5, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.total == 5 and card.extras == 5
        assert card.legal_balls == 0
        assert await crease(engine) == (1, 2)

        rec = repo.deliveries[-1]
        assert rec.runs_wides == 5
        assert rec.boundary_runs == 4
        assert rec.completed_runs == 0
        assert rec.total_team_runs == 5
        assert rec.batters_crossed is False
        assert bowler(card, 200).runs_conceded == 5

    asyncio.run(scenario())


# --- cases 5-8: no balls ----------------------------------------------------


def test_05_no_ball_alone_is_one_penalty_run_and_keeps_the_ends():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_extras=1, extra_type=ExtraType.NO_BALL)
        card = await engine.get_scorecard(7)
        assert card.total == 1 and card.extras == 1
        assert card.legal_balls == 0
        assert await crease(engine) == (1, 2)

        rec = repo.deliveries[-1]
        assert rec.runs_no_balls == 1 and rec.runs_extras == 1
        assert rec.completed_runs == 0 and rec.total_team_runs == 1

        # Unlike a wide, a no ball IS faced by the batter.
        assert batsman(card, 1).balls_faced == 1
        assert batsman(card, 1).runs == 0
        assert bowler(card, 200).runs_conceded == 1
        assert bowler(card, 200).no_balls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("bat_runs, crossed", [(2, False), (1, True)])
def test_06_no_ball_with_bat_runs_splits_the_credit_and_rotates_on_odd(bat_runs, crossed):
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_no_balls=1, runs_batsman=bat_runs, extra_type=ExtraType.NO_BALL)
        card = await engine.get_scorecard(7)
        assert card.total == 1 + bat_runs
        assert card.extras == 1  # only the penalty is an extra
        assert card.legal_balls == 0
        assert await crease(engine) == ((2, 1) if crossed else (1, 2))

        rec = repo.deliveries[-1]
        assert rec.runs_no_balls == 1
        assert rec.runs_batsman == bat_runs
        assert rec.completed_runs == bat_runs  # the penalty was never run
        assert rec.boundary_runs == 0
        assert rec.total_team_runs == 1 + bat_runs

        assert batsman(card, 1).runs == bat_runs
        # The bowler is charged the penalty AND the runs off the bat.
        assert bowler(card, 200).runs_conceded == 1 + bat_runs

    asyncio.run(scenario())


def test_07_no_ball_that_also_went_for_byes_splits_into_components():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(
            engine,
            runs_no_balls=1,
            runs_byes=2,
            runs_extras=3,
            extra_type=ExtraType.NO_BALL,
        )
        card = await engine.get_scorecard(7)
        assert card.total == 3 and card.extras == 3
        assert card.legal_balls == 0  # the no ball still is not a ball
        # 2 completed runs, even: the ends hold.
        assert await crease(engine) == (1, 2)

        rec = repo.deliveries[-1]
        assert rec.runs_no_balls == 1
        assert rec.runs_byes == 2
        assert rec.runs_extras == 3 == 1 + 2
        assert rec.completed_runs == 2
        assert rec.total_team_runs == 3

        # Byes are never the bowler's: he concedes only the penalty.
        assert batsman(card, 1).runs == 0
        assert bowler(card, 200).runs_conceded == 1
        assert bowler(card, 200).no_balls == 1
        assert bowler(card, 200).wides == 0

    asyncio.run(scenario())


def test_08_a_no_ball_overrides_a_wide_on_the_same_delivery():
    async def scenario():
        engine, repo = make([1, 2, 3])
        with pytest.raises(InvalidDeliveryError, match="no ball overrides a wide"):
            await ball(
                engine,
                runs_wides=1,
                runs_no_balls=1,
                runs_extras=2,
                extra_type=ExtraType.NO_BALL,
            )
        # Nothing was written: a refused ball must not touch the score.
        assert repo.deliveries == []
        card = await engine.get_scorecard(7)
        assert card.total == 0 and card.legal_balls == 0

    asyncio.run(scenario())


# --- cases 9-11: byes and leg byes -----------------------------------------


@pytest.mark.parametrize("runs, crossed", [(1, True), (2, False)])
def test_09_byes_are_legal_and_never_credit_batter_or_bowler(runs, crossed):
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_byes=runs, runs_extras=runs, extra_type=ExtraType.BYE)
        card = await engine.get_scorecard(7)
        assert card.total == runs and card.extras == runs
        assert card.legal_balls == 1  # byes ARE a ball of the over
        assert await crease(engine) == ((2, 1) if crossed else (1, 2))

        rec = repo.deliveries[-1]
        assert rec.runs_byes == runs and rec.completed_runs == runs
        assert rec.runs_batsman == 0

        assert batsman(card, 1).runs == 0
        assert bowler(card, 200).runs_conceded == 0
        assert bowler(card, 200).balls_bowled == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("runs, crossed", [(1, True), (2, False)])
def test_10_leg_byes_are_legal_and_never_credit_batter_or_bowler(runs, crossed):
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_leg_byes=runs, runs_extras=runs, extra_type=ExtraType.LEG_BYE)
        card = await engine.get_scorecard(7)
        assert card.total == runs and card.extras == runs
        assert card.legal_balls == 1
        assert await crease(engine) == ((2, 1) if crossed else (1, 2))

        rec = repo.deliveries[-1]
        assert rec.runs_leg_byes == runs and rec.completed_runs == runs

        assert batsman(card, 1).runs == 0
        assert bowler(card, 200).runs_conceded == 0

    asyncio.run(scenario())


def test_11_disallowed_leg_byes_score_nothing_and_restore_the_positions():
    async def scenario():
        engine, repo = make([1, 2, 3])
        # The batters ran 3, but the umpire waves them away: no runs and the
        # pair ends up back where it started.
        await ball(
            engine,
            runs_leg_byes=3,
            runs_extras=3,
            extra_type=ExtraType.LEG_BYE,
            leg_byes_allowed=False,
        )
        card = await engine.get_scorecard(7)
        assert card.total == 0 and card.extras == 0
        assert card.legal_balls == 1  # the delivery still counts
        assert await crease(engine) == (1, 2)  # restored, despite 3 "run"

        rec = repo.deliveries[-1]
        assert rec.runs_leg_byes == 0
        assert rec.runs_extras == 0
        assert rec.completed_runs == 0
        assert rec.batters_crossed is False
        assert rec.total_team_runs == 0

        assert bowler(card, 200).runs_conceded == 0

    asyncio.run(scenario())


# --- cases 12-15: dots, bat runs, boundaries -------------------------------


def test_12_dot_ball_scores_nothing_and_is_a_legal_ball():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine)
        card = await engine.get_scorecard(7)
        assert card.total == 0 and card.extras == 0
        assert card.legal_balls == 1 and card.overs_bowled_str == "0.1"
        assert await crease(engine) == (1, 2)

        rec = repo.deliveries[-1]
        assert rec.completed_runs == 0 and rec.boundary_runs == 0
        assert rec.total_team_runs == 0
        assert rec.batters_crossed is False

        assert batsman(card, 1).balls_faced == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "runs, crossed",
    [(0, False), (2, False), (4, False), (6, False), (1, True), (3, True), (5, True)],
)
def test_13_bat_runs_rotate_by_parity(runs, crossed):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await ball(engine, runs_batsman=runs)
        card = await engine.get_scorecard(7)
        assert card.total == runs
        assert card.legal_balls == 1
        assert await crease(engine) == ((2, 1) if crossed else (1, 2))
        assert batsman(card, 1).runs == runs

    asyncio.run(scenario())


@pytest.mark.parametrize("runs, fours, sixes", [(4, 1, 0), (6, 0, 1)])
def test_14_boundaries_are_an_allowance_not_runs_between_the_wickets(runs, fours, sixes):
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_batsman=runs)
        card = await engine.get_scorecard(7)
        assert card.total == runs
        # The ball did all the work: nobody crossed.
        assert await crease(engine) == (1, 2)

        rec = repo.deliveries[-1]
        assert rec.boundary_runs == runs
        assert rec.completed_runs == 0
        assert rec.batters_crossed is False

        b = batsman(card, 1)
        assert b.runs == runs and b.fours == fours and b.sixes == sixes

    asyncio.run(scenario())


def test_15_explicit_boundary_runs_and_invalid_boundary_values():
    async def scenario():
        engine, repo = make([1, 2, 3])
        # The scorer insists the 4 were run, not hit to the rope: the runs
        # become completed runs (still even, so the ends hold either way, but
        # the recorded components differ).
        await ball(engine, runs_batsman=4, boundary_runs=0)
        rec = repo.deliveries[-1]
        assert rec.boundary_runs == 0 and rec.completed_runs == 4

        # 2 and 6-with-4-runs are impossible boundary allowances.
        with pytest.raises(InvalidDeliveryError, match="boundary allowance must be 4 or 6"):
            await ball(engine, runs_batsman=2, boundary_runs=2)
        with pytest.raises(InvalidDeliveryError, match="cannot exceed"):
            await ball(engine, runs_batsman=4, boundary_runs=6)
        assert len(engine.repo.deliveries) == 1  # neither refusal wrote a row

    asyncio.run(scenario())


# --- cases 16-18: overs, illegal balls, ball numbering ----------------------


def test_16_odd_runs_on_the_last_ball_of_the_over_decide_who_faces_next():
    async def scenario():
        # Odd run on ball 6: the striker crosses, then the over-end swap
        # puts the SAME batter back on strike for the new over.
        engine, _ = make([1, 2, 3])
        for _ in range(5):
            await ball(engine)
        await ball(engine, runs_batsman=1)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6 and card.overs_bowled_str == "1.0"
        assert await crease(engine) == (1, 2)

    asyncio.run(scenario())

    async def scenario_even():
        # Even run on ball 6: no crossing, so the over-end swap hands the
        # strike to the partner. There is no blind toggle - every swap is
        # accounted for by a run or by the over ending.
        engine, _ = make([1, 2, 3])
        for _ in range(5):
            await ball(engine)
        await ball(engine, runs_batsman=2)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6 and card.overs_bowled_str == "1.0"
        assert await crease(engine) == (2, 1)

    asyncio.run(scenario_even())


def test_17_only_six_legal_deliveries_end_the_over():
    async def scenario():
        engine, _ = make([1, 2, 3])
        await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)  # illegal
        await ball(engine, runs_extras=1, extra_type=ExtraType.NO_BALL)  # illegal
        for _ in range(4):
            await ball(engine)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 4 and card.overs_bowled_str == "0.4"

        # Byes count towards the six...
        await ball(engine, runs_extras=1, extra_type=ExtraType.BYE)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 5 and card.overs_bowled_str == "0.5"

        # ...a wide does not...
        await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 5 and card.overs_bowled_str == "0.5"

        # ...and the sixth legal ball ends the over.
        await ball(engine)
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6 and card.overs_bowled_str == "1.0"
        assert card.completed is False

    asyncio.run(scenario())


def test_18_illegal_balls_do_not_advance_the_ball_number_but_byes_do():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine)  # 1.1
        await ball(engine, runs_extras=1, extra_type=ExtraType.WIDE)  # 1.2
        await ball(engine)  # still 1.2 - the wide was not a ball
        await ball(engine, runs_extras=1, extra_type=ExtraType.BYE)  # 1.3
        assert [(d.over_number, d.ball_number) for d in repo.deliveries] == [
            (1, 1),
            (1, 2),
            (1, 2),
            (1, 3),
        ]
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 3 and card.overs_bowled_str == "0.3"

    asyncio.run(scenario())


# --- cases 19-21: run outs --------------------------------------------------


def test_19_striker_run_out_records_the_actual_batter_and_the_crossing():
    async def scenario():
        # They ran 2 (even - parity says "not crossed") but the scorer says
        # they HAD crossed when the wicket fell: the explicit answer wins.
        engine, repo = make([1, 2, 3])
        await ball(
            engine,
            runs_batsman=2,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=1,
            batters_crossed=True,
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert batsman(card, 1).out is True
        assert batsman(card, 1).dismissal == "run_out"
        assert batsman(card, 2).out is False
        # The survivor completed the run to the striker's end: he stays there
        # and the new batter takes the other end.
        assert await crease(engine) == (2, None)
        await send_in(engine, 3)
        assert await crease(engine) == (2, 3)
        # A run out is never charged to the bowler.
        assert bowler(card, 200).wickets == 0

    asyncio.run(scenario())

    async def scenario_not_crossed():
        # Odd run completed (parity says "crossed") but they had NOT crossed
        # when the ball became dead: the dismissal keeps the original ends.
        engine, _ = make([1, 2, 3])
        await ball(
            engine,
            runs_batsman=1,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=1,
            batters_crossed=False,
        )
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 2)

    asyncio.run(scenario_not_crossed())


def test_20_non_striker_run_out_records_the_actual_batter_and_the_crossing():
    async def scenario():
        engine, _ = make([1, 2, 3])
        await ball(
            engine,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=2,
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert batsman(card, 2).out is True
        assert batsman(card, 1).out is False
        # The striker was nowhere near the incident: he holds his end.
        assert await crease(engine) == (1, None)
        await send_in(engine, 3)
        assert await crease(engine) == (1, 3)

    asyncio.run(scenario())

    async def scenario_crossed():
        # Same non-striker out, but the pair HAD crossed: the survivor is at
        # the striker's end, so the incoming batter takes the other one.
        engine, _ = make([1, 2, 3])
        await ball(
            engine,
            runs_batsman=2,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=2,
            batters_crossed=True,
        )
        assert await crease(engine) == (None, 1)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 1)

    asyncio.run(scenario_crossed())


@pytest.mark.parametrize("extra", [ExtraType.WIDE, ExtraType.NO_BALL])
def test_21_run_out_is_allowed_on_a_penalty_ball_and_is_not_the_bowlers(extra):
    async def scenario():
        engine, _ = make([1, 2, 3])
        await ball(
            engine,
            runs_extras=1,
            extra_type=extra,
            wicket_type=WicketType.RUN_OUT,
            dismissed_player_id=1,
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert card.legal_balls == 0  # still not a ball of the over
        assert bowler(card, 200).wickets == 0
        assert bowler(card, 200).runs_conceded == 1
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 2)

    asyncio.run(scenario())


# --- cases 22-23: striker-only wickets and the over ending ------------------


@pytest.mark.parametrize(
    "wicket",
    [
        WicketType.BOWLED,
        WicketType.CAUGHT,
        WicketType.LBW,
        WicketType.STUMPED,
        WicketType.HIT_WICKET,
        WicketType.HIT_THE_BALL_TWICE,
    ],
)
def test_22_striker_only_wickets_send_the_survivor_back_to_his_original_end(wicket):
    async def scenario():
        engine, _ = make([1, 2, 3])
        # The pair HAD crossed when the ball became dead. Since October 2022
        # the incoming batter still takes the striker's end and the not-out
        # batter returns to his original end - crossing changes nothing here.
        await ball(
            engine,
            wicket_type=wicket,
            dismissed_player_id=1,
            batters_crossed=True,
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert batsman(card, 1).out is True
        assert batsman(card, 2).out is False
        assert await crease(engine) == (None, 2)
        await send_in(engine, 3)
        assert await crease(engine) == (3, 2)  # new batter takes the strike

        # The bowler is credited with everything except hit-the-ball-twice.
        assert bowler(card, 200).wickets == (0 if wicket == WicketType.HIT_THE_BALL_TWICE else 1)

    asyncio.run(scenario())


def test_23_a_wicket_on_the_last_ball_still_ends_the_over():
    async def scenario():
        engine, _ = make([1, 2, 3, 4])
        for _ in range(5):
            await ball(engine)
        await ball(engine, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        card = await engine.get_scorecard(7)
        assert card.legal_balls == 6 and card.overs_bowled_str == "1.0"
        # The survivor was the non-striker for ball 6, so the over-end swap
        # puts him on strike for the new over.
        assert await crease(engine) == (2, None)
        # The new over belongs to a different bowler.
        await send_in(engine, 3, bowler_id=201)
        assert await crease(engine) == (2, 3)

    asyncio.run(scenario())


# --- case 24: invalid dismissals -------------------------------------------


def test_24_invalid_dismissals_are_refused_and_nothing_is_written():
    async def scenario():
        engine, repo = make([1, 2, 3])
        with pytest.raises(InvalidDeliveryError, match="can be recorded on a wide"):
            await ball(
                engine,
                runs_extras=1,
                extra_type=ExtraType.WIDE,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=1,
            )
        with pytest.raises(InvalidDeliveryError, match="can be recorded on a no ball"):
            await ball(
                engine,
                runs_extras=1,
                extra_type=ExtraType.NO_BALL,
                wicket_type=WicketType.STUMPED,
                dismissed_player_id=1,
            )
        with pytest.raises(InvalidDeliveryError, match="must be recorded as a dead ball"):
            await ball(engine, wicket_type=WicketType.RETIRED_OUT, dismissed_player_id=1)
        assert repo.deliveries == []
        card = await engine.get_scorecard(7)
        assert card.total == 0 and card.wickets == 0

    asyncio.run(scenario())


def test_24b_the_schema_refuses_unlawful_wickets_before_the_engine_sees_them():
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
    with pytest.raises(ValidationError, match="only a .* can be recorded on a wide"):
        DeliveryCreate(
            **{
                **payload,
                "extra_type": "wide",
                "runs_extras": 1,
                "wicket_type": "bowled",
                "dismissed_player_id": 1,
            }
        )
    with pytest.raises(ValidationError, match="must be recorded as a dead ball"):
        DeliveryCreate(**{**payload, "wicket_type": "retired_out", "dismissed_player_id": 1})
    with pytest.raises(ValidationError, match="dismissed_player_id is required"):
        DeliveryCreate(**{**payload, "wicket_type": "bowled"})


def test_24c_a_striker_only_wicket_of_the_non_striker_is_corrected_to_the_striker():
    async def scenario():
        # The delivery snapshot is the authority for who faced the ball: a
        # stale picker naming the non-striker records the wicket against the
        # batter who was actually on strike.
        engine, repo = make([1, 2, 3])
        await ball(engine, wicket_type=WicketType.BOWLED, dismissed_player_id=2)
        rec = repo.deliveries[-1]
        assert rec.dismissed_player_id == 1

        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert batsman(card, 1).out is True
        assert batsman(card, 2).out is False
        assert await crease(engine) == (None, 2)

    asyncio.run(scenario())


# --- case 25: obstruction ---------------------------------------------------


def test_25_obstruction_that_prevented_a_catch_is_obstructing_the_field():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(
            engine,
            wicket_type=WicketType.OBSTRUCTING_THE_FIELD,
            dismissed_player_id=1,
            obstruction_prevented_catch=True,
        )
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert batsman(card, 1).out is True
        assert batsman(card, 1).dismissal == "obstructing_the_field"
        # Law 37.3 is not the bowler's wicket.
        assert bowler(card, 200).wickets == 0
        assert repo.deliveries[-1].wicket_type == "obstructing_the_field"

    asyncio.run(scenario())


def test_25b_obstruction_preventing_a_catch_is_never_a_catch_and_never_on_a_no_ball():
    async def scenario():
        engine, repo = make([1, 2, 3])
        with pytest.raises(InvalidDeliveryError, match="not as a catch"):
            await ball(
                engine,
                wicket_type=WicketType.CAUGHT,
                dismissed_player_id=1,
                obstruction_prevented_catch=True,
            )
        with pytest.raises(InvalidDeliveryError, match="cannot be given on a no ball"):
            await ball(
                engine,
                runs_extras=1,
                extra_type=ExtraType.NO_BALL,
                wicket_type=WicketType.OBSTRUCTING_THE_FIELD,
                dismissed_player_id=1,
                obstruction_prevented_catch=True,
            )
        assert repo.deliveries == []

    asyncio.run(scenario())


# --- Law 28 penalty runs and the analytics breakdown ------------------------


def test_penalty_runs_are_team_runs_that_nobody_ran():
    async def scenario():
        engine, repo = make([1, 2, 3])
        await ball(engine, runs_penalty=5, runs_extras=5)
        card = await engine.get_scorecard(7)
        assert card.total == 5 and card.extras == 5
        assert await crease(engine) == (1, 2)  # nobody ran them

        rec = repo.deliveries[-1]
        assert rec.runs_penalty == 5
        assert rec.completed_runs == 0
        assert rec.total_team_runs == 5
        # Penalty runs are charged to the bowler's figures.
        assert bowler(card, 200).runs_conceded == 5

    asyncio.run(scenario())


def test_analytics_breaks_extras_down_by_component_not_by_extra_type():
    def delivery(over, ball_no, striker, non_striker, *, did, **runs):
        return DeliveryRecord(
            innings_id=7,
            over_number=over,
            ball_number=ball_no,
            striker_id=striker,
            non_striker_id=non_striker,
            bowler_id=200,
            runs_batsman=0,
            runs_extras=sum(
                runs.get(k, 0)
                for k in (
                    "runs_wides",
                    "runs_no_balls",
                    "runs_byes",
                    "runs_leg_byes",
                    "runs_penalty",
                )
            ),
            extra_type=runs.pop("extra_type", ExtraType.NONE).value,
            id=did,
            **runs,
        )

    deliveries = [
        delivery(1, 1, 1, 2, did=1, extra_type=ExtraType.WIDE, runs_wides=1),
        # The no ball carries byes as well: each lands in its own bucket.
        delivery(1, 1, 1, 2, did=2, extra_type=ExtraType.NO_BALL, runs_no_balls=1, runs_byes=2),
        delivery(1, 2, 1, 2, did=3, extra_type=ExtraType.LEG_BYE, runs_leg_byes=1),
    ]
    data = build_innings_analytics(FakeInnings(), deliveries, {}, max_overs=20)
    # Exactly four buckets plus the total - the shape the frontend renders.
    assert data["extras"] == {
        "wides": 1,
        "no_balls": 1,
        "byes": 2,
        "leg_byes": 1,
        "total": 5,
    }


# --- the API contract -------------------------------------------------------

EMAIL = "mcc_extras@test.com"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def as_admin():
    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(scope="module")
def world():
    """An admin, two teams with an 11-player squad each, and one match."""

    async def setup():
        async with async_session() as db:
            admin = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if admin is None:
                admin = User(
                    email=EMAIL,
                    hashed_password=hash_password("Test@1234"),
                    full_name="MCC Extras Admin",
                    role="admin",
                    is_active=True,
                    is_verified=True,
                )
                db.add(admin)
                await db.flush()

            level = (await db.execute(select(TeamLevel).limit(1))).scalar()
            country = (await db.execute(select(Country).limit(1))).scalar()
            state = (
                await db.execute(select(State).where(State.country_id == country.id).limit(1))
            ).scalar()
            city = (
                await db.execute(select(City).where(City.state_id == state.id).limit(1))
            ).scalar()

            def new_team(team, short):
                team.name = f"{short} Team"
                team.short_name = short
                team.homeground = "Test Ground"
                team.founder = "Founder"
                team.founded_year = 2020
                team.owner = "Owner"
                team.country_id = country.id
                team.state_id = state.id
                team.city_id = city.id
                team.level_id = level.id
                team.user_id = admin.id
                db.add(team)
                return team

            team_a = new_team(Team(), f"MCA {admin.id}")
            team_b = new_team(Team(), f"MCB {admin.id}")
            await db.flush()

            squads = {}
            for team, prefix in ((team_a, "A"), (team_b, "B")):
                ids = []
                for i in range(11):
                    email = f"mcc_{prefix}{i}_{admin.id}@test.com"
                    player = (
                        await db.execute(select(Player).where(Player.email == email))
                    ).scalar_one_or_none()
                    if player is None:
                        player = Player(
                            first_name=f"Bat{prefix}",
                            last_name=f"Player{i}",
                            date_of_birth=date(2000, 1, 1),
                            gender="male",
                            profile_image=None,
                            batting_hand="right",
                            batting_position="middle",
                            bowling_hand="right",
                            bowling_type="medium",
                            country_id=country.id,
                            state_id=state.id,
                            city_id=city.id,
                            height=175.0,
                            weight=70.0,
                            country_code="+91",
                            mobile_number=9700000000 + int(f"{prefix}{i}", 36),
                            email=email,
                        )
                        db.add(player)
                        await db.flush()
                    db.add(
                        PlayerTeamAssignment(
                            player_id=player.id,
                            team_id=team.id,
                            level_id=level.id,
                            role="playing_11",
                        )
                    )
                    ids.append(player.id)
                squads[team.id] = ids

            match = Match(
                match_date=date(2026, 1, 1),
                match_time="18:00",
                venue="Test Ground",
                match_type="T20",
                result=None,
                team_a_id=team_a.id,
                team_b_id=team_b.id,
                toss_winner_id=team_a.id,
                toss_decision="bat",
                status="live",
                current_innings_number=0,
                user_id=admin.id,
            )
            db.add(match)
            await db.commit()
            return {
                "admin": admin,
                "team_a": team_a.id,
                "team_b": team_b.id,
                "batting": squads[team_a.id],
                "bowling": squads[team_b.id],
                "match_id": match.id,
            }

    return asyncio.run(setup())


@pytest.fixture
def new_match(client, as_admin, world):
    """Factory: a fresh match with its first innings already started."""

    async def setup(order_size, total_overs):
        async with async_session() as db:
            match = Match(
                match_date=date(2026, 1, 5),
                match_time="18:00",
                venue="Test Ground",
                match_type="T20",
                result=None,
                team_a_id=world["team_a"],
                team_b_id=world["team_b"],
                toss_winner_id=world["team_a"],
                toss_decision="bat",
                status="live",
                current_innings_number=0,
                total_overs=total_overs,
                user_id=world["admin"].id,
            )
            db.add(match)
            await db.commit()
            return match.id

    def _start(order_size=11, total_overs=None):
        as_admin(world["admin"])
        match_id = asyncio.run(setup(order_size, total_overs))
        resp = client.post(
            f"/v1/matches/{match_id}/start-innings",
            json={"batting_order": world["batting"][:order_size]},
        )
        assert resp.status_code == 200, resp.text
        return {**world, "match_id": match_id}

    return _start


def _ball(client, match_id, striker, non_striker, bowler, **over):
    payload = {
        "striker_id": striker,
        "non_striker_id": non_striker,
        "bowler_id": bowler,
        "runs_batsman": 0,
        "runs_extras": 0,
        "extra_type": "none",
        "wicket_type": None,
        "dismissed_player_id": None,
    }
    payload.update(over)
    return client.post(f"/v1/matches/{match_id}/deliveries", json=payload)


def _scorecard(client, match_id):
    return client.get(f"/v1/matches/{match_id}/scorecard").json()


def _bowler(card, player_id):
    return next(b for b in card["bowlers"] if b["player_id"] == player_id)


def test_api_component_shaped_extras_and_a_refused_ball_leaves_the_score_alone(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    # A no ball that also went for byes - impossible before components.
    resp = _ball(
        client,
        m["match_id"],
        a[0],
        a[1],
        b[0],
        extra_type="no_ball",
        runs_no_balls=1,
        runs_byes=2,
        runs_extras=3,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 3 and body["extras"] == 3
    assert body["legal_balls"] == 0
    assert body["striker_id"] == a[0] and body["non_striker_id"] == a[1]
    assert _bowler(body, b[0])["runs_conceded"] == 1  # byes are not his

    # A wide and a no ball on one delivery is refused, and the refusal must
    # not move the displayed score by a single run.
    bad = _ball(
        client,
        m["match_id"],
        a[0],
        a[1],
        b[0],
        extra_type="no_ball",
        runs_wides=1,
        runs_no_balls=1,
        runs_extras=2,
    )
    assert bad.status_code == 400, bad.text
    assert "overrides a wide" in bad.json()["detail"]
    fresh = _scorecard(client, m["match_id"])
    assert fresh["total"] == 3 and fresh["extras"] == 3
    assert fresh["legal_balls"] == 0


def test_api_resubmitting_the_same_client_request_id_scores_only_once(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    payload = {
        "striker_id": a[0],
        "non_striker_id": a[1],
        "bowler_id": b[0],
        "runs_batsman": 4,
        "extra_type": "none",
        "client_request_id": "mcc-idem-ball-1",
    }
    first = client.post(f"/v1/matches/{m['match_id']}/deliveries", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["total"] == 4 and first.json()["legal_balls"] == 1

    # The retry (a network hiccup resending the identical attempt) returns the
    # very same scorecard instead of a second boundary.
    retry = client.post(f"/v1/matches/{m['match_id']}/deliveries", json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json() == first.json()

    fresh = _scorecard(client, m["match_id"])
    assert fresh["total"] == 4 and fresh["legal_balls"] == 1
    assert len(fresh["batsmen"]) > 0
    assert next(x for x in fresh["batsmen"] if x["player_id"] == a[0])["runs"] == 4

    # A NEW id is a new ball and scores normally.
    next_ball = {**payload, "client_request_id": "mcc-idem-ball-2", "runs_batsman": 1}
    resp = client.post(f"/v1/matches/{m['match_id']}/deliveries", json=next_ball)
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 5 and resp.json()["legal_balls"] == 2


def test_api_undo_after_extras_restores_the_whole_scorecard(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    wide = _ball(client, m["match_id"], a[0], a[1], b[0], extra_type="wide", runs_extras=1)
    assert wide.status_code == 201, wide.text
    assert wide.json()["total"] == 1 and wide.json()["extras"] == 1

    no_ball = _ball(
        client,
        m["match_id"],
        a[0],
        a[1],
        b[0],
        extra_type="no_ball",
        runs_no_balls=1,
        runs_batsman=2,
        runs_extras=1,
    )
    assert no_ball.status_code == 201, no_ball.text
    after = no_ball.json()
    assert after["total"] == 4  # 1 wide + 1 penalty + 2 off the bat
    assert after["extras"] == 2
    assert after["legal_balls"] == 0
    assert _bowler(after, b[0])["runs_conceded"] == 4  # wide 1 + no-ball 1 + 2 off the bat
    assert next(x for x in after["batsmen"] if x["player_id"] == a[0])["runs"] == 2

    undone = client.post(f"/v1/matches/{m['match_id']}/deliveries/undo")
    assert undone.status_code == 200, undone.text
    half = undone.json()
    assert half["total"] == 1 and half["extras"] == 1
    # Only the wide is left: one charged run, still no legal ball.
    assert _bowler(half, b[0])["runs_conceded"] == 1
    assert _bowler(half, b[0])["wides"] == 1
    assert _bowler(half, b[0])["balls_bowled"] == 0

    undone = client.post(f"/v1/matches/{m['match_id']}/deliveries/undo")
    assert undone.status_code == 200, undone.text
    card = undone.json()
    assert card["total"] == 0 and card["extras"] == 0
    assert card["legal_balls"] == 0 and card["overs_bowled_str"] == "0.0"
    assert card["striker_id"] == a[0] and card["non_striker_id"] == a[1]
    batter = next(x for x in card["batsmen"] if x["player_id"] == a[0])
    assert batter["runs"] == 0 and batter["balls_faced"] == 0 and not batter["out"]
    # No deliveries left at all: the bowler carries no figures either.
    assert all(x["player_id"] != b[0] for x in card["bowlers"])

    # The undo is persisted: a reload shows the same empty scorecard.
    assert _scorecard(client, m["match_id"]) == card


def test_api_a_reload_after_several_balls_shows_the_identical_scorecard(client, new_match):
    m = new_match()
    a, b = m["batting"], m["bowling"]

    r = _ball(client, m["match_id"], a[0], a[1], b[0], extra_type="wide", runs_extras=1)
    assert r.status_code == 201, r.text
    r = _ball(client, m["match_id"], a[0], a[1], b[0], runs_batsman=4)
    assert r.status_code == 201, r.text
    r = _ball(
        client,
        m["match_id"],
        a[0],
        a[1],
        b[0],
        extra_type="bye",
        runs_byes=2,
        runs_extras=2,
    )
    assert r.status_code == 201, r.text
    r = _ball(
        client,
        m["match_id"],
        a[0],
        a[1],
        b[0],
        extra_type="leg_bye",
        runs_leg_byes=1,
        runs_extras=1,
    )
    assert r.status_code == 201, r.text
    last = r.json()
    assert last["total"] == 8 and last["extras"] == 4
    assert last["legal_balls"] == 3  # the wide is not a ball

    # What a page reload fetches must be exactly what the console last saw.
    assert _scorecard(client, m["match_id"]) == last
