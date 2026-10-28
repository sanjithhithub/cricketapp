"""The closed value sets a tournament is described with.

A tournament's ``format`` decides how its fixtures are generated, its ``status``
is how a client tells a finished competition from one still being played, and a
fixture's ``stage`` says which part of a knockout bracket it belongs to. They are
enums for the same reason the domain-wide ones are: so the specification lists
the accepted values instead of leaving a client to guess from a typo.
"""

from enum import Enum


class TournamentFormat(str, Enum):
    LEAGUE = "league"
    KNOCKOUT = "knockout"
    LEAGUE_KNOCKOUT = "league_knockout"


class TournamentStatus(str, Enum):
    UPCOMING = "upcoming"
    ONGOING = "ongoing"
    COMPLETED = "completed"


class FixtureStage(str, Enum):
    LEAGUE = "league"
    QUALIFIER = "qualifier"
    ELIMINATOR = "eliminator"
    QUARTERFINAL = "quarterfinal"
    SEMIFINAL = "semifinal"
    FINAL = "final"


# The stages a generated knockout bracket is made of, in playing order. The
# league stage is deliberately absent: regenerating a knockout draw must never
# delete the league fixtures of a league-plus-knockout competition.
KNOCKOUT_STAGES: tuple[FixtureStage, ...] = (
    FixtureStage.QUALIFIER,
    FixtureStage.ELIMINATOR,
    FixtureStage.QUARTERFINAL,
    FixtureStage.SEMIFINAL,
    FixtureStage.FINAL,
)
