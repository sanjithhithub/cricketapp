"""The closed value sets this API accepts, as enums.

Every one of these fields used to be a bare ``str`` in the schema, which left the
specification unable to say what the server would accept. A generated client
could not tell a typo from a valid value, Swagger showed no list, and the only
sign the constraint existed was a 422 body carrying "Value error, ..." - the
prefix a Pydantic validator prepends when it raises.

The values are unchanged: each enum holds the exact strings the previous
validators produced, so existing rows and existing clients keep working. What
changes is that the contract is now written down, and that a value outside the
set is rejected on the way *in* rather than only on the way out.

One module because players, teams and matches share a vocabulary. ``role``
meaning one thing on a player and another on a squad is exactly the drift these
enums exist to prevent.
"""

from enum import Enum


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class BattingHand(str, Enum):
    LEFT = "Left"
    RIGHT = "Right"


class BattingPosition(str, Enum):
    OPENING = "Opening"
    MIDDLE_ORDER = "Middle Order"
    TAIL_ENDER = "Tail Ender"
    WICKET_KEEPER = "Wicket Keeper"


class BowlingHand(str, Enum):
    LEFT = "Left"
    RIGHT = "Right"


class BowlingType(str, Enum):
    FAST = "Fast"
    MEDIUM_FAST = "Medium Fast"
    SPIN = "Spin"


class SquadRole(str, Enum):
    """Which bucket of a squad a player sits in.

    ``playing_11`` is the eleven that takes the field, and is the only bucket
    with a cap (11). ``substitute`` and ``bench`` may exceed it.
    """

    PLAYING_11 = "playing_11"
    SUBSTITUTE = "substitute"
    BENCH = "bench"


class MatchType(str, Enum):
    T20 = "T20"
    ODI = "ODI"
    TEST = "Test"


class TossDecision(str, Enum):
    BAT = "bat"
    BOWL = "bowl"


class MatchStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"


class DuplicateNextAction(str, Enum):
    """What the client is expected to do after a duplicate check or a 409.

    A duplicate is never resolved by the server: it is reported, a human decides,
    and the same request is resubmitted with ``duplicate_confirmed`` or
    ``existing_player_id`` set.
    """

    NONE = "none"
    CONFIRM_SAME_PERSON = "confirm_same_person"
    CONFIRM_DUPLICATE_NAME = "confirm_duplicate_name"
    SHARED_PHONE = "shared_phone"


class DuplicateMatchedOn(str, Enum):
    """Why a candidate came back from a duplicate check."""

    PHONE = "phone"
    NAME = "name"
    ALIAS = "alias"
    EMAIL = "email"


# Spellings accepted on input that map onto a canonical member. Kept as data
# rather than as enum members so the schema advertises one value per concept
# while the server stays liberal about what a client sends.
HAND_ALIASES = {"left": "Left", "right": "Right", "l": "Left", "r": "Right"}

BATTING_POSITION_ALIASES = {
    "opening": "Opening",
    "op": "Opening",
    "middle": "Middle Order",
    "mid": "Middle Order",
    "middle order": "Middle Order",
    "tail": "Tail Ender",
    "tail ender": "Tail Ender",
    "wk": "Wicket Keeper",
    "wicket keeper": "Wicket Keeper",
}

BOWLING_TYPE_ALIASES = {
    "fast": "Fast",
    "medium fast": "Medium Fast",
    "mfast": "Medium Fast",
    "medium": "Medium Fast",
    "spin": "Spin",
}


def allowed(enum_cls: type[Enum]) -> str:
    """The members of an enum, comma separated, for an error message."""
    return ", ".join(member.value for member in enum_cls)


def coerce_enum(enum_cls: type[Enum], value: object, aliases: dict[str, str] | None = None) -> str:
    """Return the canonical string for ``enum_cls`` given whatever the client sent.

    Normalises case and surrounding whitespace, resolves the shorthand spellings in
    ``aliases`` ("L" for a left-handed bat, "mid" for middle order), and falls
    back to a case-insensitive match on the members themselves. Raises
    ``ValueError`` naming the accepted set otherwise, which Pydantic reports as a
    422 against the field.

    Deliberately liberal on input: an old client that sends "left" or "TEST" must
    keep working, and the point of the enum is that the *stored* and *documented*
    value is one of a known set - not that the server rejects a synonym.
    """
    if not isinstance(value, str):
        raise ValueError(f"must be one of {allowed(enum_cls)}")
    key = " ".join(value.split()).lower()
    canonical = (aliases or {}).get(key)
    if canonical is None:
        for member in enum_cls:
            if member.value.lower() == key:
                canonical = member.value
                break
    if canonical is None:
        raise ValueError(f"must be one of {allowed(enum_cls)}")
    return canonical
