"""Official match-result rules.

Covers the win/tie determination performed by `_compute_match_result` /
`set_match_completed` / `sync_match_result`:

- 71 -> 72/0  => chasing team won by 10 wickets
- 71 -> 72/2  => chasing team won by 8 wickets
- 71 -> 70    => defending team won by 1 run
- 72 -> 72    => match tied (target was 73; 72 with target 72 is a WIN)
- target reached before overs complete => innings completes immediately
- all out before target => defending team wins by runs
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.main  # noqa: F401  (registers every model on Base.metadata)
from app.auth.models import User  # noqa: F401
from app.database import Base, async_session, engine
from app.levels.models import TeamLevel  # noqa: F401
from app.matches.models import Match
from app.models import City, Country, State  # noqa: F401
from app.players.models import Player  # noqa: F401
from app.scoring.crud import (
    ScoringRepository,
    advance_match_after_innings,
    get_innings_by_number,
    set_match_completed,
    sync_match_result,
)
from app.scoring.engine import DeliveryInput, InningsEndedError, ScoreEngine
from app.scoring.enums import ExtraType, WicketType
from app.scoring.models import Delivery, Innings, InningsBatsman
from app.teams.models import Team

TEAM_A_NAME = "Team Alpha"
TEAM_B_NAME = "Team Bravo"

# One persistent event loop for the whole module. SQLAlchemy's async engine
# makes aiosqlite connections bound to the loop that created them, so using a
# fresh loop per test (asyncio.run) deadlocks the connection pool.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def _close_engine():
    yield
    _loop.run_until_complete(engine.dispose())


def _run_batch(total: int) -> list[int]:
    runs = []
    remaining = total
    while remaining > 6:
        runs.append(6)
        remaining -= 6
    if remaining:
        runs.append(remaining)
    return runs


def _gen_deliveries(total: int, wickets: int, order_size: int = 11) -> list[dict]:
    """Build a legal ball-by-ball sequence that reaches `total` runs with
    `wickets` wickets, exactly as the replay engine expects."""
    run_balls = _run_batch(total)
    # The 10th wicket must be the final ball (no batsman comes in after it).
    in_loop_wickets = wickets - 1 if wickets >= 10 else wickets
    gap = max(1, len(run_balls) // (in_loop_wickets + 1)) if in_loop_wickets else None

    events: list[tuple[int, bool]] = []
    placed = 0
    since_wicket = 0
    for runs in run_balls:
        events.append((runs, False))
        since_wicket += 1
        if gap is not None and placed < in_loop_wickets and since_wicket >= gap:
            events.append((0, True))
            placed += 1
            since_wicket = 0
    while placed < wickets:
        events.append((0, True))
        placed += 1

    order_ids = list(range(1, order_size + 1))
    striker_i, non_striker_i = 0, 1
    next_pos = 2
    balls_in_over = 0
    over = 1
    wickets_so_far = 0
    deliveries = []
    for runs, is_wicket in events:
        dismissed = order_ids[striker_i] if is_wicket else None
        deliveries.append(
            {
                "over_number": over,
                "ball_number": balls_in_over + 1,
                "striker_id": order_ids[striker_i],
                "non_striker_id": order_ids[non_striker_i],
                "bowler_id": 900,
                "runs_batsman": runs,
                "runs_extras": 0,
                "extra_type": ExtraType.NONE.value,
                "wicket_type": WicketType.BOWLED.value if is_wicket else None,
                "dismissed_player_id": dismissed,
            }
        )
        if is_wicket:
            wickets_so_far += 1
            if wickets_so_far < 10:
                striker_i = next_pos
                next_pos += 1
        elif runs % 2 == 1:
            striker_i, non_striker_i = non_striker_i, striker_i
        balls_in_over += 1
        if balls_in_over == 6:
            balls_in_over = 0
            over += 1
            striker_i, non_striker_i = non_striker_i, striker_i
    return deliveries


async def _ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_team(name: str) -> Team:
    return Team(
        name=name,
        short_name=name.split()[-1][:10].upper(),
        homeground="Ground",
        founder="Founder",
        founded_year=2000,
        owner="Owner",
        country_id=1,
        state_id=1,
        city_id=1,
        level_id=1,
    )


async def _attach_innings_content(
    db,
    innings: Innings,
    total: int,
    wickets: int = 0,
) -> Innings:
    """Add batting order + deliveries to an EXISTING innings (used for Super
    Over innings already opened by advance_match_after_innings, which must not
    be duplicated)."""
    for pos in range(1, 12):
        db.add(InningsBatsman(innings_id=innings.id, player_id=pos, position=pos))
    for d in _gen_deliveries(total, wickets):
        db.add(Delivery(innings_id=innings.id, **d))
    await db.commit()
    await db.refresh(innings)
    return innings


async def _seed_innings(
    db,
    match: Match,
    innings_number: int,
    batting_team_id: int,
    bowling_team_id: int,
    total: int,
    wickets: int = 0,
    target: int | None = None,
    is_super_over: bool = False,
) -> Innings:
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
    return await _attach_innings_content(db, innings, total, wickets)


async def _play_result_scenario(
    first_total: int,
    second_total: int,
    second_wickets: int = 0,
    first_wickets: int = 0,
    stale_result: str | None = None,
    heal: bool = False,
) -> str | None:
    await _ensure_tables()
    async with async_session() as db:
        team_a = _make_team(TEAM_A_NAME)
        team_b = _make_team(TEAM_B_NAME)
        db.add_all([team_a, team_b])
        await db.flush()

        match = Match(
            match_date=date(2026, 9, 23),
            match_time="19:30",
            venue="Test Ground",
            match_type="T20",
            result=stale_result,
            team_a_id=team_a.id,
            team_b_id=team_b.id,
            toss_winner_id=team_a.id,
            toss_decision="bat",
            status="live",
            current_innings_number=2,
        )
        db.add(match)
        await db.commit()
        await db.refresh(match)

        await _seed_innings(db, match, 1, team_a.id, team_b.id, first_total, wickets=first_wickets)
        await _seed_innings(
            db,
            match,
            2,
            team_b.id,
            team_a.id,
            second_total,
            wickets=second_wickets,
            target=first_total + 1,
        )

        if heal:
            return await sync_match_result(db, match)
        await set_match_completed(db, match)
        await db.refresh(match)
        return match.result


def test_reaching_target_wins_by_10_wickets():
    # Team A 71, target 72, Team B 72/0 -> Team B won by 10 wickets (NOT a tie)
    result = _run(_play_result_scenario(71, 72, second_wickets=0))
    assert result == f"{TEAM_B_NAME} won by 10 wickets"


def test_reaching_target_with_two_wickets_lost_wins_by_8_wickets():
    # Team A 71, target 72, Team B 72/2 -> Team B won by 8 wickets
    result = _run(_play_result_scenario(71, 72, second_wickets=2))
    assert result == f"{TEAM_B_NAME} won by 8 wickets"


def test_single_wicket_win_is_singular():
    # 72/9 -> 10 - 9 = 1 wicket remaining -> "won by 1 wicket" (singular)
    result = _run(_play_result_scenario(71, 72, second_wickets=9))
    assert result == f"{TEAM_B_NAME} won by 1 wicket"


@pytest.mark.parametrize(
    "chase_wickets,expected",
    [
        (0, f"{TEAM_B_NAME} won by 10 wickets"),
        (2, f"{TEAM_B_NAME} won by 8 wickets"),
        (9, f"{TEAM_B_NAME} won by 1 wicket"),
    ],
)
def test_target_of_first_innings_plus_one_win_margins(chase_wickets, expected):
    # 116 -> target 117. 117/0 = 10 wickets, 117/2 = 8, 117/9 = 1.
    result = _run(_play_result_scenario(116, 117, second_wickets=chase_wickets))
    assert result == expected


def test_never_reports_won_by_zero_wickets():
    # 116 -> 117/10: the last wicket fell on the winning delivery, so the
    # innings ended at 9 wickets. The margin must never read "0 wickets".
    result = _run(_play_result_scenario(116, 117, second_wickets=10))
    assert result == f"{TEAM_B_NAME} won by 1 wicket"
    assert "0 wickets" not in result


def test_tie_only_when_the_chase_equals_the_first_innings():
    # 116 -> 116 finished: same score, so a tie (target was 117).
    assert _run(_play_result_scenario(116, 116, second_wickets=10)) == "Match tied"
    # One run either side of the first-innings total is NOT a tie.
    assert _run(_play_result_scenario(116, 117, second_wickets=10)) == (
        f"{TEAM_B_NAME} won by 1 wicket"
    )
    assert _run(_play_result_scenario(116, 115, second_wickets=10)) == (
        f"{TEAM_A_NAME} won by 1 run"
    )


def test_defending_team_wins_by_one_run():
    # Team A 71, target 72, Team B 70 all out -> Team A won by 1 run
    result = _run(_play_result_scenario(71, 70, second_wickets=10))
    assert result == f"{TEAM_A_NAME} won by 1 run"


def test_equal_totals_is_a_tie():
    # Team A 72, target 73, Team B 72 all out -> Match tied
    result = _run(_play_result_scenario(72, 72, second_wickets=10))
    assert result == "Match tied"


def test_all_out_before_target_defending_team_wins_by_runs():
    # Team A 100, target 101, Team B 90/10 -> Team A won by 10 runs
    result = _run(_play_result_scenario(100, 90, second_wickets=10, first_wickets=7))
    assert result == f"{TEAM_A_NAME} won by 10 runs"


def test_tenth_wicket_on_winning_ball_still_wins_by_one_wicket():
    # Team A 71, target 72, Team B 72/10 with the 10th wicket falling on the
    # ball that reaches the target. The match ends the instant 72 is reached,
    # so the wicket on the winning delivery is NOT counted: 10 - 9 = 1 wicket.
    result = _run(_play_result_scenario(71, 72, second_wickets=10))
    assert result == f"{TEAM_B_NAME} won by 1 wicket"


def test_sync_heals_stale_tie_result():
    # A completed match whose stored result is a leftover manual 'tie' must be
    # corrected to the official result derived from the scorecards.
    result = _run(_play_result_scenario(71, 72, second_wickets=0, stale_result="tie", heal=True))
    assert result == f"{TEAM_B_NAME} won by 10 wickets"


# --- engine-level: innings ends the moment the target is reached ------------


class _FakeMatch:
    id = 1
    match_type = "T20"


class _FakeInnings:
    id = 1
    match_id = 1
    innings_number = 2
    batting_team_id = 11
    bowling_team_id = 22
    is_super_over = False

    def __init__(self, target, is_super_over=False):
        self.target = target
        self.is_super_over = is_super_over


class _FakeBatsman:
    def __init__(self, player_id):
        self.player_id = player_id
        self.position = player_id


class _FakeRepo:
    def __init__(self, target):
        self.match = _FakeMatch()
        self.innings = _FakeInnings(target)
        self.order = [_FakeBatsman(p) for p in range(1, 12)]
        self.deliveries = []

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


async def _ball(engine, striker, non_striker, *, runs=0, bowler=200):
    return await engine.apply_delivery(
        engine.repo.innings.id,
        DeliveryInput(
            striker_id=striker,
            non_striker_id=non_striker,
            bowler_id=bowler,
            runs_batsman=runs,
            extra_type=ExtraType.NONE,
        ),
    )


def test_target_reached_completes_innings_before_overs_end():
    # 20-over match, target 72: reaching 72 mid-over must end the innings
    # immediately (Cases E and G), not wait for 20 overs.
    async def scenario():
        repo = _FakeRepo(target=72)
        engine = ScoreEngine(repo)

        # Over 1 (bowler 200): six sixes -> 36. Strike swaps at over end.
        for _ in range(6):
            await _ball(engine, 1, 2, runs=6, bowler=200)

        # Over 2 (bowler 300): 5 sixes + 2 -> 68. Even runs keep striker.
        for _ in range(5):
            await _ball(engine, 2, 1, runs=6, bowler=300)
        await _ball(engine, 2, 1, runs=2, bowler=300)

        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 68
        assert card.completed is False

        # Case G: a boundary takes 68 to the target 72 -> immediate win.
        await _ball(engine, 1, 2, runs=4, bowler=400)
        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 72
        assert card.completed is True
        assert card.end_reason == "target_chased"
        assert card.overs_bowled_str == "2.1"
        assert card.overs_bowled_str != "20.0"

        with pytest.raises(InningsEndedError):
            await _ball(engine, 1, 2, runs=0, bowler=500)

    _run(scenario())


def test_wicket_on_winning_delivery_not_credited_to_bowler_or_batsman():
    # A six that also takes a wicket ends the chase immediately. The wicket
    # must not appear on the scorecard: the bowler gets no wicket, the batsman
    # is not marked out, and the card shows 0 wickets.
    async def scenario():
        repo = _FakeRepo(target=6)
        engine = ScoreEngine(repo)

        await engine.apply_delivery(
            repo.innings.id,
            DeliveryInput(
                striker_id=1,
                non_striker_id=2,
                bowler_id=200,
                runs_batsman=6,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=1,
            ),
        )

        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 6
        assert card.wickets == 0
        assert card.completed is True
        assert card.end_reason == "target_chased"
        assert len(card.bowlers) == 1
        assert card.bowlers[0].player_id == 200
        assert card.bowlers[0].wickets == 0
        assert card.bowlers[0].runs_conceded == 6

        striker_card = next(b for b in card.batsmen if b.player_id == 1)
        assert striker_card.runs == 6
        assert striker_card.out is False
        assert striker_card.dismissal is None

        with pytest.raises(InningsEndedError):
            await _ball(engine, 1, 2, runs=0, bowler=300)

    _run(scenario())


# --- Super Over -----------------------------------------------------------
#
# A Super Over reuses the same Innings/Delivery/engine, flagged with
# is_super_over. It is capped at 6 legal balls (wides/no-balls don't count),
# ends the moment the target is reached, and is tracked separately from the
# normal innings.


def test_super_over_ends_immediately_on_two_wickets():
    # A Super Over is at most 6 legal balls AND at most 2 wickets. The second
    # wicket ends the innings right away, even with legal balls still in hand,
    # and no further batting is allowed.
    async def scenario():
        repo = _FakeRepo(target=None)
        repo.innings.is_super_over = True
        engine = ScoreEngine(repo)

        # Ball 1: wicket (player 1 out, player 3 comes in).
        await engine.apply_delivery(
            repo.innings.id,
            DeliveryInput(
                striker_id=1,
                non_striker_id=2,
                bowler_id=200,
                runs_batsman=0,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=1,
            ),
        )
        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 0
        assert card.wickets == 1
        assert card.completed is False

        # Ball 2: the second wicket ends the innings with 4 legal balls unused.
        await engine.apply_delivery(
            repo.innings.id,
            DeliveryInput(
                striker_id=3,
                non_striker_id=2,
                bowler_id=200,
                runs_batsman=0,
                wicket_type=WicketType.BOWLED,
                dismissed_player_id=3,
            ),
        )
        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 0
        assert card.wickets == 2
        assert card.legal_balls == 2
        assert card.completed is True
        assert card.end_reason == "wickets_exhausted"
        assert card.overs_bowled_str == "0.2"

        with pytest.raises(InningsEndedError):
            await _ball(engine, 1, 2, runs=0)

    _run(scenario())


def test_super_over_two_wickets_ends_even_mid_over_with_runs():
    # Two wickets with runs scored still ends the innings immediately (the
    # innings does not wait for the 6th legal ball).
    async def scenario():
        repo = _FakeRepo(target=None)
        repo.innings.is_super_over = True
        engine = ScoreEngine(repo)

        await _ball(engine, 1, 2, runs=4, bowler=200)
        await engine.apply_delivery(
            repo.innings.id,
            DeliveryInput(
                striker_id=1,
                non_striker_id=2,
                bowler_id=200,
                runs_batsman=0,
                wicket_type=WicketType.CAUGHT,
                dismissed_player_id=1,
            ),
        )
        await engine.apply_delivery(
            repo.innings.id,
            DeliveryInput(
                striker_id=3,
                non_striker_id=2,
                bowler_id=200,
                runs_batsman=2,
                wicket_type=WicketType.RUN_OUT,
                dismissed_player_id=3,
            ),
        )
        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 6
        assert card.wickets == 2
        assert card.legal_balls == 3
        assert card.completed is True
        assert card.end_reason == "wickets_exhausted"

    _run(scenario())


def test_super_over_ends_after_six_legal_balls():
    async def scenario():
        repo = _FakeRepo(target=None)
        repo.innings.is_super_over = True
        engine = ScoreEngine(repo)

        striker, non_striker = 1, 2
        for _ in range(6):
            await _ball(engine, striker, non_striker, runs=1)
            striker, non_striker = non_striker, striker

        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 6
        assert card.legal_balls == 6
        assert card.completed is True
        assert card.end_reason == "overs_complete"
        assert card.is_super_over is True
        assert card.max_overs == 1
        assert card.overs_bowled_str == "1.0"

        with pytest.raises(InningsEndedError):
            await _ball(engine, striker, non_striker, runs=0)

    _run(scenario())


def test_super_over_wides_and_no_balls_do_not_count():
    async def scenario():
        repo = _FakeRepo(target=None)
        repo.innings.is_super_over = True
        engine = ScoreEngine(repo)

        # 5 wides + 1 legal run: over NOT complete (only 1 legal ball). A wide of
        # 1 is the penalty alone, which the batters never ran, so the ends never
        # change — the engine must be fed the real crease.
        striker, non_striker = 1, 2
        for _ in range(5):
            card = await engine.get_scorecard(repo.innings.id)
            assert (card.striker_id, card.non_striker_id) == (striker, non_striker)
            await engine.apply_delivery(
                repo.innings.id,
                DeliveryInput(
                    striker_id=striker,
                    non_striker_id=non_striker,
                    bowler_id=200,
                    runs_extras=1,
                    extra_type=ExtraType.WIDE,
                ),
            )
        await _ball(engine, striker, non_striker, runs=1)

        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 6
        assert card.legal_balls == 1
        assert card.extras == 5
        assert card.completed is False
        # The 5 wides are dead balls and the single crossed once: back to square one.
        assert (card.striker_id, card.non_striker_id) == (2, 1)
        # The bowler conceded all 6 runs but has bowled only 1 legal ball.
        assert card.bowlers[0].runs_conceded == 6
        assert card.bowlers[0].balls_bowled == 1
        assert card.bowlers[0].overs_str == "0.1"

    _run(scenario())


def test_super_over_target_reached_ends_immediately():
    async def scenario():
        repo = _FakeRepo(target=11)
        repo.innings.is_super_over = True
        engine = ScoreEngine(repo)

        await _ball(engine, 1, 2, runs=6)
        await _ball(engine, 1, 2, runs=4)
        await _ball(engine, 1, 2, runs=1)

        card = await engine.get_scorecard(repo.innings.id)
        assert card.total == 11
        assert card.legal_balls == 3
        assert card.completed is True
        assert card.end_reason == "target_chased"

        with pytest.raises(InningsEndedError):
            await _ball(engine, 1, 2, runs=0)

    _run(scenario())


def _scorecard_fake(total: int, innings_number: int) -> SimpleNamespace:
    return SimpleNamespace(
        completed=True,
        total=total,
        innings_number=innings_number,
    )


async def _setup_tied_match(
    db,
    team_a: Team,
    team_b: Team,
    *,
    super_over_enabled=True,
    super_over_repeat=True,
    first_total=72,
    second_total=72,
):
    match = Match(
        match_date=date(2026, 9, 23),
        match_time="19:30",
        venue="Test Ground",
        match_type="T20",
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        toss_winner_id=team_a.id,
        toss_decision="bat",
        status="live",
        current_innings_number=2,
        super_over_enabled=super_over_enabled,
        super_over_repeat=super_over_repeat,
    )
    db.add(match)
    await db.commit()
    await db.refresh(match)

    first = await _seed_innings(db, match, 1, team_a.id, team_b.id, first_total, wickets=8)
    second = await _seed_innings(
        db, match, 2, team_b.id, team_a.id, second_total, wickets=0, target=first_total + 1
    )
    return match, first, second


def test_tie_with_super_over_opens_super_over_innings():
    async def scenario():
        await _ensure_tables()
        async with async_session() as db:
            team_a = _make_team(TEAM_A_NAME)
            team_b = _make_team(TEAM_B_NAME)
            db.add_all([team_a, team_b])
            await db.flush()

            match, _, second = await _setup_tied_match(db, team_a, team_b)
            await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))

            innings3 = await db.execute(select(Innings).where(Innings.match_id == match.id))
            innings_numbers = sorted(i.innings_number for i in innings3.scalars().all())
            assert innings_numbers == [1, 2, 3]
            third = await get_innings_by_number(db, match.id, 3)
            assert third is not None
            assert third.is_super_over is True
            assert third.target is None
            # Team A (first innings batting side) bats first in the Super Over.
            assert third.batting_team_id == team_a.id
            assert match.current_innings_number == 3
            assert match.status == "live"

    _run(scenario())


async def _finish_super_over(
    *,
    super_over_enabled=True,
    super_over_repeat=True,
    setter_runs=10,
    chase_runs=11,
):
    """Play a full Super Over on top of a 72->72 level main match.

    Returns (match, innings_numbers_created, result) after the chase innings
    advanced the match."""
    await _ensure_tables()
    async with async_session() as db:
        team_a = _make_team(TEAM_A_NAME)
        team_b = _make_team(TEAM_B_NAME)
        db.add_all([team_a, team_b])
        await db.flush()

        match, _, second = await _setup_tied_match(
            db,
            team_a,
            team_b,
            super_over_enabled=super_over_enabled,
            super_over_repeat=super_over_repeat,
        )
        await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))
        third = await get_innings_by_number(db, match.id, 3)
        assert third is not None and third.is_super_over
        assert third.batting_team_id == team_a.id

        # Setter (Team A) bats its Super Over in the innings that was already
        # opened — never create a duplicate innings row.
        await _attach_innings_content(db, third, setter_runs)
        await advance_match_after_innings(db, match, third, _scorecard_fake(setter_runs, 3))
        fourth = await get_innings_by_number(db, match.id, 4)
        assert fourth is not None
        assert fourth.is_super_over is True
        assert fourth.target == setter_runs + 1
        assert fourth.batting_team_id == team_b.id

        # Chaser (Team B) bats its Super Over.
        await _attach_innings_content(db, fourth, chase_runs)
        await advance_match_after_innings(db, match, fourth, _scorecard_fake(chase_runs, 4))

        await db.refresh(match)
        return match


def test_super_over_10_vs_11_decides_winner():
    # Level main match 72 -> 72, then Super Over Team A 10, Team B 11.
    # Team B (the chaser) wins the Super Over.
    match = _run(_finish_super_over(setter_runs=10, chase_runs=11))
    assert match.status == "completed"
    assert match.result == f"{TEAM_B_NAME} won the Super Over"


def test_super_over_setter_win_11_vs_10():
    # Team A setter makes 11; Team B chaser manages only 10 -> Team A wins.
    match = _run(_finish_super_over(setter_runs=11, chase_runs=10))
    assert match.status == "completed"
    assert match.result == f"{TEAM_A_NAME} won the Super Over"


def test_super_over_tied_10_vs_10_without_repeat_is_tie():
    # Super Over 10 -> 10 and repeats are NOT configured: match tied.
    match = _run(_finish_super_over(super_over_repeat=False, setter_runs=10, chase_runs=10))
    assert match.status == "completed"
    assert match.result == "Match tied"


def test_super_over_tied_10_vs_10_starts_another_super_over_when_configured():
    # Super Over 10 -> 10 with repeats enabled: another Super Over must start
    # (innings 5), with Team B (who chased last) batting first.
    async def scenario():
        await _ensure_tables()
        async with async_session() as db:
            team_a = _make_team(TEAM_A_NAME)
            team_b = _make_team(TEAM_B_NAME)
            db.add_all([team_a, team_b])
            await db.flush()

            match, _, second = await _setup_tied_match(db, team_a, team_b)
            await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))
            third = await get_innings_by_number(db, match.id, 3)
            assert third is not None and third.is_super_over
            assert third.batting_team_id == team_a.id

            await _attach_innings_content(db, third, 10)
            await advance_match_after_innings(db, match, third, _scorecard_fake(10, 3))
            fourth = await get_innings_by_number(db, match.id, 4)
            assert fourth is not None and fourth.is_super_over
            assert fourth.target == 11

            await _attach_innings_content(db, fourth, 10)
            await advance_match_after_innings(db, match, fourth, _scorecard_fake(10, 4))

            fifth = await get_innings_by_number(db, match.id, 5)
            assert fifth is not None
            assert fifth.is_super_over is True
            assert fifth.target is None
            # The side that chased last (Team B) bats first in the next one.
            assert fifth.batting_team_id == team_b.id
            assert match.current_innings_number == 5
            assert match.status == "live"

    _run(scenario())


def test_super_over_cannot_start_when_disabled():
    # Level main match with super_over_enabled=False must finish as a tie.
    async def scenario():
        await _ensure_tables()
        async with async_session() as db:
            team_a = _make_team(TEAM_A_NAME)
            team_b = _make_team(TEAM_B_NAME)
            db.add_all([team_a, team_b])
            await db.flush()

            match = Match(
                match_date=date(2026, 9, 23),
                match_time="19:30",
                venue="Test Ground",
                match_type="T20",
                team_a_id=team_a.id,
                team_b_id=team_b.id,
                toss_winner_id=team_a.id,
                toss_decision="bat",
                status="live",
                current_innings_number=2,
                super_over_enabled=False,
                super_over_repeat=True,
            )
            db.add(match)
            await db.commit()
            await db.refresh(match)

            await _seed_innings(db, match, 1, team_a.id, team_b.id, 72, wickets=8)
            second = await _seed_innings(
                db,
                match,
                2,
                team_b.id,
                team_a.id,
                72,
                wickets=0,
                target=73,
            )
            await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))

            assert match.status == "completed"
            assert match.result == "Match tied"
            assert await get_innings_by_number(db, match.id, 3) is None

    _run(scenario())


def test_super_over_setter_two_wickets_ends_then_chase_target_wins():
    # Full flow: Team A 10/2 in the Super Over ends immediately on the second
    # wicket -> Team B chases 11 -> reaching 11 wins the Super Over.
    async def scenario():
        await _ensure_tables()
        async with async_session() as db:
            team_a = _make_team(TEAM_A_NAME)
            team_b = _make_team(TEAM_B_NAME)
            db.add_all([team_a, team_b])
            await db.flush()

            match, _, second = await _setup_tied_match(db, team_a, team_b)
            await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))
            third = await get_innings_by_number(db, match.id, 3)
            assert third is not None and third.is_super_over
            assert third.batting_team_id == team_a.id

            engine = ScoreEngine(ScoringRepository(db))
            await _attach_innings_content(db, third, 10, wickets=2)
            third_card = await engine.get_scorecard(third.id)
            assert third_card.total == 10
            assert third_card.wickets == 2
            assert third_card.completed is True
            assert third_card.end_reason == "wickets_exhausted"

            # Team A's over is over at 10/2: the chase opens automatically.
            await advance_match_after_innings(db, match, third, third_card)
            fourth = await get_innings_by_number(db, match.id, 4)
            assert fourth is not None and fourth.is_super_over
            assert fourth.target == 11
            assert fourth.batting_team_id == team_b.id

            # Team B reaches 11 and wins immediately.
            await _attach_innings_content(db, fourth, 11)
            fourth_card = await engine.get_scorecard(fourth.id)
            assert fourth_card.total == 11
            assert fourth_card.completed is True
            assert fourth_card.end_reason == "target_chased"
            await advance_match_after_innings(db, match, fourth, fourth_card)

            await db.refresh(match)
            assert match.status == "completed"
            assert match.result == f"{TEAM_B_NAME} won the Super Over"

    _run(scenario())


def test_super_over_chase_two_wickets_loses_to_setter():
    # Team A sets 10. Team B loses 2 wickets chasing 11 (9/2): the chase ends
    # immediately on the second wicket, the setter wins the Super Over.
    async def scenario():
        await _ensure_tables()
        async with async_session() as db:
            team_a = _make_team(TEAM_A_NAME)
            team_b = _make_team(TEAM_B_NAME)
            db.add_all([team_a, team_b])
            await db.flush()

            match, _, second = await _setup_tied_match(db, team_a, team_b)
            await advance_match_after_innings(db, match, second, _scorecard_fake(72, 2))
            third = await get_innings_by_number(db, match.id, 3)
            assert third is not None and third.is_super_over

            engine = ScoreEngine(ScoringRepository(db))
            await _attach_innings_content(db, third, 10)
            await advance_match_after_innings(db, match, third, _scorecard_fake(10, 3))
            fourth = await get_innings_by_number(db, match.id, 4)
            assert fourth is not None and fourth.is_super_over
            assert fourth.target == 11

            await _attach_innings_content(db, fourth, 9, wickets=2)
            fourth_card = await engine.get_scorecard(fourth.id)
            assert fourth_card.total == 9
            assert fourth_card.wickets == 2
            assert fourth_card.completed is True
            assert fourth_card.end_reason == "wickets_exhausted"
            await advance_match_after_innings(db, match, fourth, fourth_card)

            await db.refresh(match)
            assert match.status == "completed"
            assert match.result == f"{TEAM_A_NAME} won the Super Over"

    _run(scenario())
