import asyncio
from dataclasses import dataclass

import pytest

from app.scoring.engine import (
    DeliveryInput,
    DeliveryRecord,
    InningsEndedError,
    InningsNotFoundError,
    InvalidBatsmanError,
    InvalidDeliveryError,
    ScoreEngine,
)
from app.scoring.enums import ExtraType, WicketType


@dataclass
class FakeMatch:
    id: int
    match_type: str


@dataclass
class FakeInnings:
    id: int
    match_id: int
    innings_number: int
    batting_team_id: int = 11
    bowling_team_id: int = 22
    target: int | None = None


@dataclass
class FakeBatsman:
    player_id: int
    position: int


class FakeExpandedRepo:
    def __init__(self, match: FakeMatch, innings: FakeInnings, order_ids: list[int]):
        self.match = match
        self.innings = innings
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
        return self.order

    async def add_delivery(self, record):
        record.id = len(self.deliveries) + 1
        self.deliveries.append(record)
        return record


def make_fake(order_ids, fmt="T20", target=None, innings_number=1):
    match = FakeMatch(id=99, match_type=fmt)
    innings = FakeInnings(
        id=7,
        match_id=99,
        innings_number=innings_number,
        target=target,
    )
    repo = FakeExpandedRepo(match, innings, order_ids)
    return ScoreEngine(repo), repo


async def ball(
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


def test_normal_scoring_and_strike_rotation():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        d1 = await ball(engine, 1, 2, runs_batsman=1)
        assert (d1.over_number, d1.ball_number, d1.runs_batsman) == (1, 1, 1)
        d2 = await ball(engine, 2, 1)
        assert (d2.over_number, d2.ball_number) == (1, 2)
        d3 = await ball(engine, 2, 1, runs_batsman=2)
        assert d3.ball_number == 3
        d4 = await ball(engine, 2, 1, runs_batsman=1)
        assert d4.ball_number == 4
        card = await engine.get_scorecard(7)
        assert card.total == 4
        assert card.wickets == 0
        assert card.legal_balls == 4
        assert card.overs_bowled_str == "0.4"
        assert [c.runs for c in card.batsmen] == [1, 3, 0]

    asyncio.run(scenario())


def test_wide_and_no_ball_are_illegal_balls():
    async def scenario():
        engine, repo = make_fake([1, 2])
        wide = await ball(engine, 1, 2, runs_extras=1, extra_type=ExtraType.WIDE)
        assert (wide.over_number, wide.ball_number) == (1, 1)
        assert engine.repo is repo
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 0

        single = await ball(engine, 1, 2, runs_batsman=1)
        assert (single.over_number, single.ball_number) == (1, 1)
        assert single.striker_id == 1
        assert single.non_striker_id == 2

        no_ball_four = await ball(engine, 2, 1, runs_batsman=4, extra_type=ExtraType.NO_BALL)
        assert (no_ball_four.over_number, no_ball_four.ball_number) == (1, 2)
        assert no_ball_four.striker_id == 2

        card = await engine.get_scorecard(7)
        assert card.total == 1 + 1 + 4
        assert card.legal_balls == 1
        b2 = next(c for c in card.batsmen if c.player_id == 2)
        assert b2.runs == 4
        assert b2.balls_faced == 1
        assert b2.fours == 1
        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 1 + 1 + 4
        assert bowler.balls_bowled == 3

    asyncio.run(scenario())


def test_bye_and_leg_bye_go_to_extras_and_can_rotate_strike():
    async def scenario():
        engine, _ = make_fake([1, 2])
        lb = await ball(engine, 1, 2, runs_extras=1, extra_type=ExtraType.LEG_BYE)
        assert (lb.over_number, lb.ball_number) == (1, 1)
        await ball(engine, 2, 1, runs_extras=0, extra_type=ExtraType.BYE)
        card = await engine.get_scorecard(7)
        assert card.total == 1
        b1 = next(c for c in card.batsmen if c.player_id == 1)
        assert b1.runs == 0
        assert b1.balls_faced == 1
        bowler = card.bowlers[0]
        assert bowler.runs_conceded == 0

    asyncio.run(scenario())


def test_wicket_next_batsman_on_strike():
    async def scenario():
        engine, _ = make_fake([1, 2, 3, 4])
        await ball(engine, 1, 2, runs_batsman=1)
        await ball(engine, 2, 1)
        d = await ball(engine, 2, 1, wicket_type=WicketType.CAUGHT, dismissed_player_id=2)
        assert d.wicket_type == WicketType.CAUGHT.value
        card = await engine.get_scorecard(7)
        assert card.wickets == 1
        assert next(c for c in card.batsmen if c.player_id == 2).out is True
        await ball(engine, 3, 1)

    asyncio.run(scenario())


def test_non_striker_out_keeps_striker_on_strike():
    async def scenario():
        engine, _ = make_fake([1, 2, 3, 4])
        await ball(engine, 1, 2)
        await ball(engine, 1, 2)
        d = await ball(engine, 1, 2, wicket_type=WicketType.RUN_OUT, dismissed_player_id=2)
        assert d.dismissed_player_id == 2
        await ball(engine, 1, 3)

    asyncio.run(scenario())


def test_over_completion_swaps_ends():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        for _ in range(5):
            await ball(engine, 1, 2)
        d6 = await ball(engine, 1, 2)
        assert (d6.over_number, d6.ball_number) == (1, 6)
        await ball(engine, 2, 1)

    asyncio.run(scenario())


def test_odd_runs_on_last_ball_keep_strike_for_next_over():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        for _ in range(5):
            await ball(engine, 1, 2)
        d6 = await ball(engine, 1, 2, runs_batsman=1)
        assert (d6.over_number, d6.ball_number) == (1, 6)
        await ball(engine, 1, 2)

    asyncio.run(scenario())


def test_innings_end_all_out():
    async def scenario():
        engine, _ = make_fake([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        striker, non_striker = 1, 2
        next_pos = 2
        order = list(range(1, 12))
        for i in range(10):
            dismissed = striker if i % 2 == 0 else non_striker
            await ball(
                engine,
                striker,
                non_striker,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=dismissed,
            )
            if i < 9:
                if dismissed == striker:
                    striker = order[next_pos]
                else:
                    non_striker = order[next_pos]
                next_pos += 1
            if (i + 1) % 6 == 0:
                striker, non_striker = non_striker, striker

        card = await engine.get_scorecard(7)
        assert card.completed is True
        assert card.end_reason == "all_out"
        assert card.wickets == 10

        with pytest.raises(InningsEndedError):
            await ball(engine, striker, non_striker)

    asyncio.run(scenario())


def test_innings_end_overs_complete():
    async def scenario():
        engine, _ = make_fake([1, 2], fmt="T20")
        striker, non_striker = 1, 2
        for _ in range(20):
            for _ in range(6):
                await ball(engine, striker, non_striker)
            striker, non_striker = non_striker, striker
        card = await engine.get_scorecard(7)
        assert card.completed is True
        assert card.end_reason == "overs_complete"
        assert card.overs_bowled_str == "20.0"

        with pytest.raises(InningsEndedError):
            await ball(engine, striker, non_striker)

    asyncio.run(scenario())


def test_innings_end_target_chased():
    async def scenario():
        engine, _ = make_fake([1, 2, 3], fmt="ODI", target=5, innings_number=2)
        d = await ball(engine, 1, 2, runs_batsman=3)
        assert d.ball_number == 1
        await ball(engine, 2, 1, runs_batsman=2)
        card = await engine.get_scorecard(7)
        assert card.completed is True
        assert card.end_reason == "target_chased"
        assert card.total == 5

        with pytest.raises(InningsEndedError):
            await ball(engine, 1, 2)

    asyncio.run(scenario())


def test_scorecard_aggregation():
    async def scenario():
        engine, _ = make_fake([1, 2])
        await ball(engine, 1, 2, runs_batsman=1)
        await ball(engine, 2, 1, runs_batsman=4)
        await ball(engine, 2, 1, runs_batsman=6)
        await ball(engine, 2, 1, runs_extras=1, extra_type=ExtraType.WIDE)
        await ball(engine, 2, 1, runs_extras=1, extra_type=ExtraType.LEG_BYE)
        await ball(engine, 1, 2)

        card = await engine.get_scorecard(7)
        assert card.total == 13
        assert card.wickets == 0
        assert card.legal_balls == 5
        assert card.overs_bowled_str == "0.5"
        assert card.current_run_rate == 15.6

        b1 = next(c for c in card.batsmen if c.player_id == 1)
        assert (b1.runs, b1.balls_faced) == (1, 2)
        assert b1.did_not_bat is False
        b2 = next(c for c in card.batsmen if c.player_id == 2)
        assert (b2.runs, b2.balls_faced, b2.fours, b2.sixes) == (10, 3, 1, 1)
        assert b2.strike_rate == 333.33

        bowler = card.bowlers[0]
        assert bowler.balls_bowled == 6
        assert bowler.runs_conceded == 12
        assert bowler.wickets == 0
        assert bowler.economy == 12.0

    asyncio.run(scenario())


def test_maiden_counting():
    async def scenario():
        engine, _ = make_fake([1, 2])
        for _ in range(5):
            await ball(engine, 1, 2)
        await ball(engine, 1, 2)
        card = await engine.get_scorecard(7)
        bowler = card.bowlers[0]
        assert bowler.overs_str == "1.0"
        assert bowler.maidens == 1

    asyncio.run(scenario())


def test_invalid_inputs():
    async def scenario():
        engine, _ = make_fake([1, 2])
        with pytest.raises(InvalidDeliveryError):
            await ball(engine, 1, 2, bowler_id=1)
        with pytest.raises(InvalidBatsmanError):
            await ball(engine, 2, 1)
        with pytest.raises(InvalidDeliveryError):
            await ball(engine, 1, 2, runs_batsman=-1)
        with pytest.raises(InvalidDeliveryError):
            await ball(engine, 1, 2, runs_batsman=1, extra_type=ExtraType.WIDE)
        with pytest.raises(InvalidDeliveryError):
            await ball(engine, 1, 2, wicket_type=WicketType.CAUGHT)
        with pytest.raises(InvalidDeliveryError):
            await ball(engine, 1, 2, wicket_type=WicketType.CAUGHT, dismissed_player_id=99)

    asyncio.run(scenario())


def test_missing_innings_raises():
    async def scenario():
        engine, _ = make_fake([1, 2])
        with pytest.raises(InningsNotFoundError):
            await engine.apply_delivery(
                999, DeliveryInput(striker_id=1, non_striker_id=2, bowler_id=3)
            )
        with pytest.raises(InningsNotFoundError):
            await engine.get_scorecard(999)

    asyncio.run(scenario())


def test_delivery_record_is_append_only():
    async def scenario():
        engine, repo = make_fake([1, 2])
        d1 = await ball(engine, 1, 2)
        await ball(engine, 1, 2)
        d3 = await ball(engine, 1, 2)
        assert [d.id for d in repo.deliveries] == [1, 2, 3]
        assert repo.deliveries[0] is d1
        assert repo.deliveries[2] is d3
        assert all(isinstance(d, DeliveryRecord) for d in repo.deliveries)

    asyncio.run(scenario())
