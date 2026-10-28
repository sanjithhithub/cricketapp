"""Referee identity: the permanent code an official is known by.

The rules are the ones players use, with a different prefix. Both kinds of code
appear on a scorecard - the officials panel names the umpires and the referee,
and the batting card names the players - so a prefix of its own is what keeps a
referee code from ever being read as a player code.

A code is random rather than derived from the name: an official may be renamed,
and two officials may share a name, and neither may break a scorecard that
already names them.
"""

import secrets

from app.enums import ExperienceLevel
from app.players.identity import CODE_ALPHABET, CODE_LENGTH

CODE_PREFIX = "REF"

# The bands an official's experience falls into, and the years each one covers.
#
# The rule lives in one place, and the stored value is always derived from it,
# so `experience_years` and `experience_level` can never contradict one
# another. There is no "expert" band above "experienced": a panel does not
# need to tell a 40-year veteran from a 20-year one, and a rule with no end
# would invite an argument.
EXPERIENCE_BANDS: tuple[tuple[str, int], ...] = (
    (ExperienceLevel.BEGINNER.value, 2),
    (ExperienceLevel.INTERMEDIATE.value, 7),
)


def generate_referee_code() -> str:
    """Return a fresh random code, e.g. ``REF-7F3KQ2ZM``.

    Random rather than derived from the name or the row id: a code must survive
    a rename, must not leak the name, and must not be guessable from one
    official's code to the next. Uniqueness is enforced by the unique index, and
    the caller retries on the (vanishingly rare) collision.
    """
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{CODE_PREFIX}-{body}"


def experience_level_for(years: int | None) -> str:
    """The band an official with ``years`` of experience falls into.

    The rule, in one place so the schema, the model and the API all agree:

    * 0-2 years -> ``beginner``
    * 3-7 years -> ``intermediate``
    * 8 years and above -> ``experienced``

    Negative years are treated as none, which lands in ``beginner`` - a value
    that cannot happen through the API, since the schema refuses it.
    """
    value = max(int(years or 0), 0)
    for level, upper in EXPERIENCE_BANDS:
        if value <= upper:
            return level
    return ExperienceLevel.EXPERIENCED.value
