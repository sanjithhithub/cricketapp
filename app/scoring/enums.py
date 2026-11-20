"""Enums that belong to scoring only.

The domain-wide ones (gender, hands, positions, squad roles, match type, toss,
status) live in :mod:`app.enums` so that matches and players can name them
without importing a scoring module. MatchStatus is re-exported here because this
is where the scoring code reads it from, and moving it should not mean editing
four modules to say the same thing.
"""

from enum import Enum

from app.enums import MatchStatus

__all__ = [
    "DEAD_BALL_WICKETS",
    "EITHER_BATTER_WICKETS",
    "ExtraType",
    "MatchFormat",
    "MatchStatus",
    "NON_BOWLER_WICKETS",
    "STRIKER_ONLY_WICKETS",
    "WICKETS_ON_NO_BALL",
    "WICKETS_ON_WIDE",
    "WicketType",
]


class MatchFormat(str, Enum):
    T20 = "T20"
    ODI = "ODI"
    TEST = "TEST"


class ExtraType(str, Enum):
    NONE = "none"
    WIDE = "wide"
    NO_BALL = "no_ball"
    BYE = "bye"
    LEG_BYE = "leg_bye"
    # An internal marker for a dismissal that happens between deliveries, when
    # no ball was bowled (retired out, timed out). It consumes no ball of the
    # over and carries no runs; it exists so those dismissals can live in the
    # same delivery log as everything else without inflating the score.
    DEAD_BALL = "dead_ball"


class WicketType(str, Enum):
    BOWLED = "bowled"
    CAUGHT = "caught"
    LBW = "lbw"
    RUN_OUT = "run_out"
    STUMPED = "stumped"
    HIT_WICKET = "hit_wicket"
    OBSTRUCTING_THE_FIELD = "obstructing_the_field"
    HIT_THE_BALL_TWICE = "hit_the_ball_twice"
    RETIRED_OUT = "retired_out"
    TIMED_OUT = "timed_out"


# Dismissals that can only remove the batter who faced the delivery. The
# delivery's ``striker_id`` is the authority for every one of them; a run out is
# deliberately absent because either batter may be out.
STRIKER_ONLY_WICKETS = (
    WicketType.BOWLED,
    WicketType.CAUGHT,
    WicketType.LBW,
    WicketType.STUMPED,
    WicketType.HIT_WICKET,
    WicketType.HIT_THE_BALL_TWICE,
)

# Dismissals where the scorer must say which of the two batters it was. Timed
# out is handled separately because the batter it removes is the incoming one,
# who is not at the crease until the scorer sends them in.
EITHER_BATTER_WICKETS = (
    WicketType.RUN_OUT,
    WicketType.OBSTRUCTING_THE_FIELD,
    WicketType.RETIRED_OUT,
)

# Dismissals that happen between deliveries. They are recorded as a dead ball:
# no ball of the over, no runs, no bowler credit.
DEAD_BALL_WICKETS = (
    WicketType.RETIRED_OUT,
    WicketType.TIMED_OUT,
)

# Dismissals the bowler is never credited with. Run out, obstruction, hitting
# the ball twice, retirement and a timed-out batter are not the bowler's doing.
NON_BOWLER_WICKETS = (
    WicketType.RUN_OUT,
    WicketType.OBSTRUCTING_THE_FIELD,
    WicketType.HIT_THE_BALL_TWICE,
    WicketType.RETIRED_OUT,
    WicketType.TIMED_OUT,
)

# A wide is a live ball for the fielding side: the keeper can stump and either
# batter can be run out or given out obstructing the field. A no-ball also
# removes the stumping and adds hit-the-ball-twice as a possible dismissal of
# the striker. Bowled, caught, LBW and hit wicket cannot happen on a penalty
# delivery.
WICKETS_ON_WIDE = (
    WicketType.RUN_OUT,
    WicketType.STUMPED,
    WicketType.OBSTRUCTING_THE_FIELD,
)

WICKETS_ON_NO_BALL = (
    WicketType.RUN_OUT,
    WicketType.OBSTRUCTING_THE_FIELD,
    WicketType.HIT_THE_BALL_TWICE,
)
