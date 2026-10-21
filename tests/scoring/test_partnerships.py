"""Unit tests for the partnership calculator.

The calculator is pure: it reads a delivery log and returns standings. Every
expected number below is worked out by hand from the delivery list so the test
does not just read the code's own answer back.

The delivery objects are :class:`types.SimpleNamespace` stand-ins carrying only
the fields the calculator reads, which keeps the test focused on the cricket
rules rather than on the ORM.
"""

from types import SimpleNamespace

from app.scoring.partnerships import compute_partnerships, current_partnership


def ball(
    delivery_id,
    striker,
    non_striker,
    runs_batsman=0,
    runs_extras=0,
    extra_type="none",
    wicket_type=None,
    dismissed_player_id=None,
):
    return SimpleNamespace(
        id=delivery_id,
        striker_id=striker,
        non_striker_id=non_striker,
        runs_batsman=runs_batsman,
        runs_extras=runs_extras,
        extra_type=extra_type,
        wicket_type=wicket_type,
        dismissed_player_id=dismissed_player_id,
    )


# --- the illustrative example from the specification ------------------------


def test_the_spec_example_reproduces_exactly():
    """A stand of 57 off 37 balls split 32 (22) and 23 (15), with 2 extras.

    Contribution is measured against the pair's own runs (55), not the stand
    total (57), which is why the percentages are 58.18 / 41.82.
    """
    deliveries = []
    nid = 0

    def add(striker, non_striker, runs_batsman=0, runs_extras=0, extra_type="none"):
        nonlocal nid
        nid += 1
        deliveries.append(ball(nid, striker, non_striker, runs_batsman, runs_extras, extra_type))

    # Batter 1 (id 1): 4,4,6,6 then twelve singles then six dots = 32 off 22.
    for runs in (4, 4, 6, 6):
        add(1, 2, runs_batsman=runs)
    for _ in range(12):
        add(1, 2, runs_batsman=1)
    for _ in range(6):
        add(1, 2, runs_batsman=0)
    # Batter 2 (id 2): 6,6,4,4 then three singles then eight dots = 23 off 15.
    for runs in (6, 6, 4, 4):
        add(2, 1, runs_batsman=runs)
    for _ in range(3):
        add(2, 1, runs_batsman=1)
    for _ in range(8):
        add(2, 1, runs_batsman=0)
    # Two wides: team runs, no ball, faced by nobody.
    add(1, 2, runs_extras=1, extra_type="wide")
    add(2, 1, runs_extras=1, extra_type="wide")

    stands = compute_partnerships(deliveries)

    assert len(stands) == 1
    stand = stands[0]
    assert stand.total_runs == 57
    assert stand.total_balls == 37
    assert stand.run_rate == 9.24
    assert stand.batter1.player_id == 1
    assert stand.batter1.runs == 32
    assert stand.batter1.balls == 22
    assert stand.batter2.player_id == 2
    assert stand.batter2.runs == 23
    assert stand.batter2.balls == 15
    assert stand.contribution_of(1) == 58.18
    assert stand.contribution_of(2) == 41.82


# --- extras -----------------------------------------------------------------


def test_a_wide_is_a_run_but_not_a_ball_and_is_not_faced():
    stand = compute_partnerships([ball(1, 1, 2, runs_extras=1, extra_type="wide")])[0]

    assert stand.total_runs == 1
    assert stand.total_balls == 0
    assert stand.run_rate == 0.0
    assert stand.batter1.runs == 0
    assert stand.batter1.balls == 0


def test_a_no_ball_is_a_run_and_a_ball_faced_but_not_a_legal_ball():
    # A no-ball with two off the bat: the batter keeps his two runs and faces the
    # ball, but the delivery is not a ball of the over.
    stand = compute_partnerships(
        [ball(1, 1, 2, runs_batsman=2, runs_extras=1, extra_type="no_ball")]
    )[0]

    assert stand.total_runs == 3
    assert stand.total_balls == 0
    assert stand.batter1.runs == 2
    assert stand.batter1.balls == 1


def test_byes_and_leg_byes_are_legal_balls_that_credit_the_striker_with_none():
    deliveries = [
        ball(1, 1, 2, runs_extras=2, extra_type="bye"),
        ball(2, 1, 2, runs_extras=3, extra_type="leg_bye"),
    ]
    stand = compute_partnerships(deliveries)[0]

    assert stand.total_runs == 5
    assert stand.total_balls == 2
    assert stand.batter1.runs == 0
    assert stand.batter1.balls == 2


# --- wickets and new partnerships -------------------------------------------


def test_a_wicket_closes_the_stand_and_the_next_ball_opens_a_new_one():
    deliveries = [
        ball(1, 1, 2, runs_batsman=4),
        ball(2, 1, 2, runs_batsman=1),
        # The striker falls; the incoming batter (3) replaces him.
        ball(3, 2, 1, wicket_type="bowled", dismissed_player_id=2),
        ball(4, 1, 3, runs_batsman=2),
    ]
    stands = compute_partnerships(deliveries)

    assert len(stands) == 2
    first, second = stands
    assert first.unbroken is False
    assert first.player_out_id == 2
    assert first.total_runs == 5
    assert {first.batter1.player_id, first.batter2.player_id} == {1, 2}
    assert second.unbroken is True
    assert second.player_out_id is None
    assert second.total_runs == 2
    assert {second.batter1.player_id, second.batter2.player_id} == {1, 3}


def test_current_partnership_is_none_once_a_wicket_has_fallen():
    deliveries = [
        ball(1, 1, 2, runs_batsman=4),
        ball(2, 1, 2, wicket_type="bowled", dismissed_player_id=1),
    ]
    assert current_partnership(deliveries) is None


def test_current_partnership_is_the_unbroken_trailing_stand():
    deliveries = [ball(1, 1, 2, runs_batsman=4), ball(2, 1, 2, runs_batsman=6)]
    current = current_partnership(deliveries)
    assert current is not None
    assert current.unbroken is True
    assert current.total_runs == 10


# --- the winning ball of a chase --------------------------------------------


def test_the_winning_ball_never_breaks_the_stand():
    """The chase-winning delivery must not open a new partnership.

    Even when that ball also carries a wicket, the engine does not register it,
    so the stand that reached the target is reported unbroken.
    """
    deliveries = [
        ball(1, 1, 2, runs_batsman=6),
        ball(2, 1, 2, runs_batsman=6, wicket_type="run_out", dismissed_player_id=1),
    ]
    stands = compute_partnerships(deliveries, target=12)

    assert len(stands) == 1
    assert stands[0].total_runs == 12
    assert stands[0].unbroken is True
    assert stands[0].player_out_id is None


# --- zero-run / empty stands ------------------------------------------------


def test_a_zero_run_stand_is_safe_and_reports_zero_run_rate():
    stand = compute_partnerships([ball(1, 1, 2), ball(2, 1, 2), ball(3, 1, 2)])[0]

    assert stand.total_runs == 0
    assert stand.total_balls == 3
    assert stand.run_rate == 0.0
    # No runs means no contribution to split; it must not divide by zero.
    assert stand.contribution_of(1) == 0.0
    assert stand.contribution_of(2) == 0.0


def test_an_empty_log_has_no_stand():
    assert compute_partnerships([]) == []
    assert current_partnership([]) is None
