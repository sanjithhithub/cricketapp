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
    is_super_over: bool = False
    next_batsman_id: int | None = None


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

    async def player_identities(self, player_ids):
        # No players in the stub; names on the card are optional.
        return {}


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
        # A wide with 1 run: illegal ball, team +1, bowler +1, batter +0. The
        # single run is the wide penalty the batters never ran, so the ends stay.
        wide = await ball(engine, 1, 2, runs_extras=1, extra_type=ExtraType.WIDE)
        assert (wide.over_number, wide.ball_number) == (1, 1)
        assert engine.repo is repo
        card = await engine.get_scorecard(7)
        assert card.legal_balls == 0
        assert (card.striker_id, card.non_striker_id) == (1, 2)

        single = await ball(engine, 1, 2, runs_batsman=1)
        # The wide was not a ball of the over, so the single is still ball 1.1.
        assert (single.over_number, single.ball_number) == (1, 1)
        assert single.striker_id == 1
        assert single.non_striker_id == 2

        # No-ball + 4 off the bat: the penalty is a dead ball, so the completed
        # runs are the 4 off the bat and the ends do NOT change. (Player 2 is on
        # strike because the single above crossed.)
        no_ball_four = await ball(
            engine, 2, 1, runs_batsman=4, runs_extras=1, extra_type=ExtraType.NO_BALL
        )
        assert (no_ball_four.over_number, no_ball_four.ball_number) == (1, 2)
        assert no_ball_four.striker_id == 2

        card = await engine.get_scorecard(7)
        # 1 (wide) + 1 (single) + 4 (off bat) + 1 (no-ball penalty) = 7
        assert card.total == 1 + 1 + 4 + 1
        assert card.extras == 2
        assert card.legal_balls == 1
        b1 = next(c for c in card.batsmen if c.player_id == 1)
        b2 = next(c for c in card.batsmen if c.player_id == 2)
        # Player 1 faced the single (and crossed); player 2 came in and faced the
        # no-ball for 4. Neither is charged for the wide.
        assert (b1.runs, b1.balls_faced) == (1, 1)
        assert (b2.runs, b2.balls_faced) == (4, 1)
        bowler = card.bowlers[0]
        # Bowler concedes every run except none were byes: 1 + 1 + 5 = 7, but
        # only the 1 legal ball counts towards the over.
        assert bowler.runs_conceded == 7
        assert bowler.balls_bowled == 1
        assert bowler.overs_str == "0.1"

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
        # The wicket leaves the striker end vacant until a new batsman is picked.
        assert card.striker_id is None
        assert card.non_striker_id == 1
        # The scorer sends player 3 in; the delivery records the new crease.
        await ball(engine, 3, 1)

    asyncio.run(scenario())


def test_non_striker_out_keeps_striker_on_strike():
    async def scenario():
        engine, _ = make_fake([1, 2, 3, 4])
        await ball(engine, 1, 2)
        await ball(engine, 1, 2)
        d = await ball(engine, 1, 2, wicket_type=WicketType.RUN_OUT, dismissed_player_id=2)
        assert d.dismissed_player_id == 2
        card = await engine.get_scorecard(7)
        # Only the non-striker end is vacant; the striker stays on strike.
        assert card.striker_id == 1
        assert card.non_striker_id is None
        await ball(engine, 1, 3)

    asyncio.run(scenario())


def test_over_completion_swaps_ends():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        for _ in range(5):
            await ball(engine, 1, 2)
        d6 = await ball(engine, 1, 2)
        assert (d6.over_number, d6.ball_number) == (1, 6)
        await ball(engine, 2, 1, bowler_id=300)

    asyncio.run(scenario())


def test_odd_runs_on_last_ball_keep_strike_for_next_over():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        for _ in range(5):
            await ball(engine, 1, 2)
        d6 = await ball(engine, 1, 2, runs_batsman=1)
        assert (d6.over_number, d6.ball_number) == (1, 6)
        await ball(engine, 1, 2, bowler_id=300)

    asyncio.run(scenario())


def test_bowler_cannot_bowl_two_consecutive_overs():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        for _ in range(5):
            await ball(engine, 1, 2)
        d6 = await ball(engine, 1, 2)
        assert (d6.over_number, d6.ball_number) == (1, 6)

        with pytest.raises(InvalidDeliveryError, match="consecutive overs"):
            await ball(engine, 2, 1)

        # A different bowler can start the new over...
        d = await ball(engine, 2, 1, bowler_id=300)
        assert (d.over_number, d.ball_number) == (2, 1)
        # ...and bowl the rest of that same over.
        for _ in range(5):
            await ball(engine, 2, 1, bowler_id=300)

        # ...but the over after that must go to someone else again.
        with pytest.raises(InvalidDeliveryError, match="consecutive overs"):
            await ball(engine, 1, 2, bowler_id=300)

        card = await engine.get_scorecard(7)
        assert card.last_over_bowler_id == 300

    asyncio.run(scenario())


def test_bowler_max_over_limit():
    async def scenario():
        engine, _ = make_fake([1, 2, 3], fmt="T20")
        striker, non_striker = 1, 2
        bowlers = [200, 300]
        # Four alternate overs: 200 bowls over 1 & 3, 300 bowls over 2 & 4.
        for over in range(4):
            for _ in range(6):
                await ball(engine, striker, non_striker, bowler_id=bowlers[over % 2])
            striker, non_striker = non_striker, striker

        card = await engine.get_scorecard(7)
        b200 = next(b for b in card.bowlers if b.player_id == 200)
        assert b200.overs_str == "2.0"

        # Keep alternating: 200 bowls over 5 & 7 to reach his full 4 overs.
        for over in range(4, 8):
            for _ in range(6):
                await ball(engine, striker, non_striker, bowler_id=bowlers[over % 2])
            striker, non_striker = non_striker, striker

        card = await engine.get_scorecard(7)
        b200 = next(b for b in card.bowlers if b.player_id == 200)
        assert b200.overs_str == "4.0"

        # Bowler 200 has bowled his 4 overs; he must not start a 5th over.
        with pytest.raises(InvalidDeliveryError, match="maximum 4 overs"):
            await ball(engine, striker, non_striker, bowler_id=200)

        # A fresh bowler can still take the over.
        d = await ball(engine, striker, non_striker, bowler_id=400)
        assert (d.over_number, d.ball_number) == (9, 1)

    asyncio.run(scenario())


def test_innings_end_all_out():
    async def scenario():
        engine, _ = make_fake([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        order = list(range(1, 12))
        # A distinct bowler per delivery, so the consecutive-over rule never
        # interferes with what this test is about (the ten wickets).
        bowler = 200
        # Ten wickets, alternating which end is lost. The crease is read back
        # from the scorecard after every ball so the end-of-over swap and the
        # incoming batsman are handled by the engine, not re-simulated here.
        for i in range(10):
            card = await engine.get_scorecard(7)
            striker, non_striker = card.striker_id, card.non_striker_id
            assert striker is not None, f"wicket {i}: no crease to dismiss"
            dismissed = striker if i % 2 == 0 else non_striker
            await ball(
                engine,
                striker,
                non_striker,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=dismissed,
                bowler_id=bowler,
            )
            bowler += 1
            if i < 9:
                # The wicket vacates one end; the scorer sends in the next batter
                # and the following delivery records that crease.
                incoming = order[i + 2]
                card = await engine.get_scorecard(7)
                if card.striker_id is None:
                    await ball(engine, incoming, card.non_striker_id, bowler_id=bowler)
                else:
                    await ball(engine, card.striker_id, incoming, bowler_id=bowler)
                bowler += 1

        card = await engine.get_scorecard(7)
        assert card.completed is True
        assert card.end_reason == "all_out"
        assert card.wickets == 10
        assert card.order_exhausted is False

        with pytest.raises(InningsEndedError):
            await ball(engine, card.striker_id, card.non_striker_id)

    asyncio.run(scenario())


def test_innings_end_overs_complete():
    async def scenario():
        engine, _ = make_fake([1, 2], fmt="T20")
        striker, non_striker = 1, 2
        bowlers = [200, 300, 400, 500, 600]
        for over in range(20):
            for _ in range(6):
                await ball(engine, striker, non_striker, bowler_id=bowlers[over % len(bowlers)])
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
        await ball(engine, 1, 2, runs_batsman=1)  # odd -> ends change
        await ball(engine, 2, 1, runs_batsman=4)  # even -> stay
        await ball(engine, 2, 1, runs_batsman=6)  # even -> stay
        # Wide with 1 run: that run is the penalty, not a completed run, so the
        # ends stay put.
        await ball(engine, 2, 1, runs_extras=1, extra_type=ExtraType.WIDE)
        # Leg bye of 1: legal ball, odd completed runs, ends change.
        await ball(engine, 2, 1, runs_extras=1, extra_type=ExtraType.LEG_BYE)
        await ball(engine, 1, 2)

        card = await engine.get_scorecard(7)
        assert card.total == 13
        assert card.wickets == 0
        # The wide is the only non-legal delivery of the 6.
        assert card.legal_balls == 5
        assert card.extras == 2
        assert card.overs_bowled_str == "0.5"
        assert card.current_run_rate == 15.6

        b1 = next(c for c in card.batsmen if c.player_id == 1)
        assert (b1.runs, b1.balls_faced) == (1, 2)
        assert b1.did_not_bat is False
        b2 = next(c for c in card.batsmen if c.player_id == 2)
        assert (b2.runs, b2.balls_faced, b2.fours, b2.sixes) == (10, 3, 1, 1)
        assert b2.strike_rate == 333.33

        bowler = card.bowlers[0]
        assert bowler.balls_bowled == 5
        assert bowler.overs_str == "0.5"
        # Runs off bat + wides; the leg bye is not charged to the bowler.
        assert bowler.runs_conceded == 12
        assert bowler.wickets == 0
        assert bowler.economy == 14.4

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
            await ball(engine, 1, 2, runs_extras=3)
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


def test_order_exhaustion_leaves_a_vacant_slot_not_a_finished_innings():
    """A wicket with nobody left in the order pauses the innings.

    It used to end the innings as "all_out" while leaving striker_id pointing
    at the player who had just been dismissed. Now the vacant slot is reported
    as None so the client knows to ask for a new batsman.
    """

    async def scenario():
        engine, repo = make_fake([1, 2])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=2)

        card = await engine.get_scorecard(7)
        assert card.completed is False
        assert card.end_reason is None
        assert card.wickets == 1
        assert card.awaiting_batsman is True
        assert card.order_exhausted is True
        # The dismissed non-striker is gone, not still listed at the crease.
        assert card.striker_id == 1
        assert card.non_striker_id is None

    asyncio.run(scenario())


def test_dismissed_striker_leaves_striker_id_null():
    """The case the scoring UI keys off: striker out, no partner available."""

    async def scenario():
        engine, _ = make_fake([1, 2])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        card = await engine.get_scorecard(7)
        assert card.striker_id is None
        assert card.non_striker_id == 2
        assert card.completed is False
        assert card.awaiting_batsman is True
        assert card.wickets == 1

    asyncio.run(scenario())


def test_a_vacant_end_must_be_filled_by_the_delivery():
    """While a wicket is pending, the next delivery has to name the new batsman.

    Re-sending the dismissed player, or getting the surviving end wrong, is a
    client bug and is rejected.
    """

    async def scenario():
        engine, _ = make_fake([1, 2, 3])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        # The striker end is empty. Naming the dismissed player again is wrong.
        with pytest.raises(InvalidBatsmanError):
            await ball(engine, 1, 2)
        # So is dropping the surviving non-striker.
        with pytest.raises(InvalidBatsmanError):
            await ball(engine, 3, 4)
        # A real substitute filling the gap is accepted.
        await ball(engine, 3, 2)
        card = await engine.get_scorecard(7)
        assert (card.striker_id, card.non_striker_id) == (3, 2)

    asyncio.run(scenario())


def test_manual_pick_resumes_the_innings():
    async def scenario():
        engine, repo = make_fake([1, 2])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=2)

        repo.order.append(FakeBatsman(player_id=3, position=3))
        repo.innings.next_batsman_id = 3

        card = await engine.get_scorecard(7)
        assert card.completed is False
        assert card.awaiting_batsman is False
        assert card.order_exhausted is False
        assert (card.striker_id, card.non_striker_id) == (1, 3)

        await ball(engine, 1, 3, runs_batsman=1)
        card = await engine.get_scorecard(7)
        assert card.total == 1
        # The pick is one-shot: it is cleared once a delivery puts the player at
        # the crease, so a later wicket cannot send them in a second time.
        assert repo.innings.next_batsman_id is None

    asyncio.run(scenario())


def test_manual_pick_from_anywhere_in_the_order():
    """A lower-order batter can be chosen instead of the next one in line."""

    async def scenario():
        engine, repo = make_fake([1, 2, 3, 4, 5])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        # Normally player 3 would come in next. Pick 5 instead.
        repo.innings.next_batsman_id = 5
        card = await engine.get_scorecard(7)
        assert card.striker_id == 5
        assert card.non_striker_id == 2
        # 3 and 4 keep their order positions and read as did-not-bat.
        by_id = {b.player_id: b for b in card.batsmen}
        assert by_id[3].did_not_bat is True
        assert by_id[4].did_not_bat is True
        assert by_id[5].did_not_bat is False

    asyncio.run(scenario())


def test_manual_pick_does_not_resend_a_player_on_a_later_wicket():
    """A dismissed pick is not offered again by a stale pointer."""

    async def scenario():
        engine, repo = make_fake([1, 2, 3, 4])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        # Pick 3, who is exactly the next player in the order.
        repo.innings.next_batsman_id = 3
        assert (await engine.get_scorecard(7)).striker_id == 3

        # The pick is consumed once a delivery puts player 3 at the crease.
        await ball(engine, 3, 2)
        assert repo.innings.next_batsman_id is None

        # Player 3 is now dismissed. The next wicket leaves a fresh vacancy that
        # the scorer must fill - it never silently re-sends the dismissed 3.
        await ball(engine, 3, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=3)
        card = await engine.get_scorecard(7)
        assert card.wickets == 2
        assert card.striker_id is None
        assert card.awaiting_batsman is True

    asyncio.run(scenario())


def test_manual_pick_as_a_substitute_who_never_batted():
    async def scenario():
        engine, repo = make_fake([1, 2])
        await ball(engine, 1, 2, wicket_type=WicketType.BOWLED, dismissed_player_id=1)

        # A squad player who was never in the XI is appended to the order.
        repo.order.append(FakeBatsman(player_id=9, position=3))
        repo.innings.next_batsman_id = 9

        card = await engine.get_scorecard(7)
        assert card.striker_id == 9
        assert card.batsmen[-1].player_id == 9
        assert card.batsmen[-1].position == 3
        assert card.batsmen[-1].did_not_bat is False

    asyncio.run(scenario())


def test_scorecard_reports_current_crease():
    async def scenario():
        engine, _ = make_fake([1, 2, 3])

        card = await engine.get_scorecard(7)
        assert (card.striker_id, card.non_striker_id) == (1, 2)

        await ball(engine, 1, 2, runs_batsman=1)
        card = await engine.get_scorecard(7)
        assert (card.striker_id, card.non_striker_id) == (2, 1)

        await ball(engine, 2, 1, runs_batsman=2)
        card = await engine.get_scorecard(7)
        assert (card.striker_id, card.non_striker_id) == (2, 1)

        await ball(engine, 2, 1, wicket_type=WicketType.CAUGHT, dismissed_player_id=2)
        card = await engine.get_scorecard(7)
        # A wicket vacates the striker end until the scorer picks who comes in.
        assert card.striker_id is None
        assert card.non_striker_id == 1

    asyncio.run(scenario())
