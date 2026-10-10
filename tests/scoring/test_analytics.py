"""Tests for the ball-by-ball analytics builder.

The builder is a pure function of the delivery log, so these tests feed it
synthetic deliveries directly and assert the chronological events, the
over-by-over totals, the scoring pattern, the extras breakdown and the run-rate
progression - including the winning-ball rule that must match the engine.
"""

from dataclasses import dataclass

from app.scoring.analytics import build_innings_analytics
from app.scoring.engine import DeliveryRecord
from app.scoring.enums import ExtraType, WicketType
from app.scoring.schemas import MatchAnalyticsResponse

IDENTITIES = {
    1: ("Virat", "Kohli", "VK"),
    2: ("Rohit", "Sharma", "RS"),
    3: ("Shubman", "Gill", "SG"),
    200: ("Jasprit", "Bumrah", "JB"),
    201: ("Mitch", "Starc", "MS"),
}


@dataclass
class FakeInnings:
    id: int = 7
    match_id: int = 99
    innings_number: int = 1
    batting_team_id: int = 11
    bowling_team_id: int = 22
    target: int | None = None
    is_super_over: bool = False


def delivery(
    over,
    ball,
    striker,
    non_striker,
    *,
    runs_batsman=0,
    runs_extras=0,
    extra=ExtraType.NONE,
    wicket=None,
    dismissed=None,
    bowler=200,
    did=1,
):
    return DeliveryRecord(
        innings_id=7,
        over_number=over,
        ball_number=ball,
        striker_id=striker,
        non_striker_id=non_striker,
        bowler_id=bowler,
        runs_batsman=runs_batsman,
        runs_extras=runs_extras,
        extra_type=extra.value,
        wicket_type=wicket.value if wicket else None,
        dismissed_player_id=dismissed,
        id=did,
    )


def sample_innings():
    deliveries = [
        delivery(1, 1, 1, 2, runs_batsman=1, did=1),
        delivery(1, 2, 2, 1, runs_batsman=4, did=2),
        delivery(1, 3, 1, 2, runs_extras=1, extra=ExtraType.WIDE, did=3),
        delivery(1, 3, 1, 2, did=4),
        delivery(1, 4, 1, 2, runs_batsman=6, did=5),
        delivery(1, 5, 1, 2, runs_extras=1, extra=ExtraType.NO_BALL, did=6),
        delivery(1, 5, 1, 2, runs_batsman=2, did=7),
        delivery(
            1,
            6,
            1,
            2,
            wicket=WicketType.BOWLED,
            dismissed=1,
            did=8,
        ),
        delivery(2, 1, 3, 2, runs_batsman=3, bowler=201, did=9),
        delivery(2, 2, 3, 2, runs_extras=2, extra=ExtraType.BYE, bowler=201, did=10),
    ]
    return build_innings_analytics(FakeInnings(), deliveries, IDENTITIES, max_overs=20)


def test_totals_and_extras_breakdown():
    data = sample_innings()
    assert data["total"] == 20
    assert data["wickets"] == 1
    assert data["legal_balls"] == 8
    assert data["overs_bowled_str"] == "1.2"
    assert data["run_rate"] == 15.0
    assert data["extras"] == {
        "wides": 1,
        "no_balls": 1,
        "byes": 2,
        "leg_byes": 0,
        "total": 4,
    }


def test_scoring_pattern():
    pattern = sample_innings()["scoring_pattern"]
    assert pattern["deliveries"] == 10
    assert pattern["legal_balls"] == 8
    assert pattern["dot_balls"] == 2
    assert pattern["singles"] == 1
    assert pattern["doubles"] == 1
    assert pattern["triples"] == 1
    assert pattern["fours"] == 1
    assert pattern["sixes"] == 1
    assert pattern["boundaries"] == 2
    assert pattern["boundary_runs"] == 10
    assert pattern["runs_off_bat"] == 16
    assert pattern["extras"] == 4


def test_chronological_events_and_running_state():
    events = sample_innings()["deliveries"]
    assert [e["display"] for e in events] == [
        "1",
        "4",
        "1wd",
        "0",
        "6",
        "1nb",
        "2",
        "W",
        "3",
        "2b",
    ]
    # Running total after each delivery.
    assert [e["team_total"] for e in events] == [1, 5, 6, 6, 12, 13, 15, 15, 18, 20]
    # The wicket is reported with the dismissed batsman.
    wicket_event = events[7]
    assert wicket_event["wicket_type"] == "bowled"
    assert wicket_event["dismissed_player_id"] == 1
    assert wicket_event["dismissed_player_name"] == "Virat Kohli"
    assert wicket_event["team_wickets"] == 1
    # Extras are flagged and named.
    assert events[2]["extra_type"] == "wide"
    assert events[2]["is_legal_ball"] is False
    assert events[5]["extra_type"] == "no_ball"
    assert events[9]["extra_type"] == "bye"
    # Names resolve from identities.
    assert events[0]["striker_name"] == "Virat Kohli"
    assert events[0]["bowler_name"] == "Jasprit Bumrah"


def test_over_by_over_and_run_rate_progression():
    data = sample_innings()
    overs = data["over_by_over"]
    assert [o["over_number"] for o in overs] == [1, 2]
    assert overs[0]["runs"] == 15
    assert overs[0]["legal_balls"] == 6
    assert overs[0]["wickets"] == 1
    assert overs[0]["extras"] == 2
    assert overs[0]["dot_balls"] == 2
    assert overs[0]["bowler_name"] == "Jasprit Bumrah"
    assert overs[1]["runs"] == 5
    assert overs[1]["legal_balls"] == 2
    assert overs[1]["bowler_name"] == "Mitch Starc"
    assert overs[1]["cumulative_runs"] == 20

    progression = data["run_rate_progression"]
    assert progression[0]["run_rate"] == 15.0
    assert progression[1]["cumulative_runs"] == 20
    assert progression[1]["run_rate"] == 15.0
    # Target none -> no required rate.
    assert progression[0]["required_run_rate"] is None


def test_required_run_rate_when_chasing():
    deliveries = [
        delivery(1, 1, 1, 2, runs_batsman=6, did=1),
    ]
    data = build_innings_analytics(FakeInnings(target=40), deliveries, IDENTITIES, max_overs=20)
    # 40-6 = 34 needed from (120-1) balls = 19.83 overs -> 1.71 rpo.
    assert data["run_rate_progression"][0]["required_run_rate"] == 1.71


def test_winning_ball_wicket_is_not_counted():
    # A six reaches the target of 5; the wicket on the same ball must not count.
    deliveries = [
        delivery(
            1,
            1,
            1,
            2,
            runs_batsman=6,
            wicket=WicketType.RUN_OUT,
            dismissed=1,
            did=1,
        ),
    ]
    data = build_innings_analytics(FakeInnings(target=5), deliveries, IDENTITIES, max_overs=20)
    assert data["total"] == 6
    assert data["wickets"] == 0
    event = data["deliveries"][0]
    assert event["wicket_type"] == "run_out"
    assert event["dismissed_player_id"] is None
    assert event["team_wickets"] == 0


def test_payload_matches_response_model():
    payload = {"match_id": 99, "innings": [sample_innings()]}
    model = MatchAnalyticsResponse.model_validate(payload)
    assert model.match_id == 99
    assert model.innings[0].scoring_pattern.sixes == 1
    assert model.innings[0].deliveries[2].display == "1wd"
