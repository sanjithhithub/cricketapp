"""Enums that belong to scoring only.

The domain-wide ones (gender, hands, positions, squad roles, match type, toss,
status) live in :mod:`app.enums` so that matches and players can name them
without importing a scoring module. MatchStatus is re-exported here because this
is where the scoring code reads it from, and moving it should not mean editing
four modules to say the same thing.
"""

from enum import Enum

from app.enums import MatchStatus

__all__ = ["ExtraType", "MatchFormat", "MatchStatus", "WicketType"]


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


class WicketType(str, Enum):
    BOWLED = "bowled"
    CAUGHT = "caught"
    LBW = "lbw"
    RUN_OUT = "run_out"
    STUMPED = "stumped"
    HIT_WICKET = "hit_wicket"
