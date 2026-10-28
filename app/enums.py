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


class RefereeType(str, Enum):
    """What kind of official a person is.

    A slot and a type are different things, and the difference matters. The slots
    - "Referee 1", "Referee 2", "Match Referee" - belong to one match and say
    where that person stood in it. The type belongs to the person and says what
    they do: the same official can stand in the square leg for one match and
    referee the next, so the type cannot live on the match.
    """

    FIRST_UMPIRE = "first_umpire"
    SECOND_UMPIRE = "second_umpire"
    THIRD_UMPIRE = "third_umpire"
    FOURTH_UMPIRE = "fourth_umpire"
    MATCH_REFEREE = "match_referee"
    DEPUTY_MATCH_REFEREE = "deputy_match_referee"
    RESERVE_UMPIRE = "reserve_umpire"


class RefereeStatus(str, Enum):
    """Whether an official may be put on a match at all.

    Three states, because "not working right now" and "not to be used" are
    different facts. ``active`` officials can be appointed, ``inactive`` ones
    are on the register but not taking fixtures (a long break, a season
    between seasons), and ``suspended`` means something is wrong - a
    certification lapse, a complaint - and the official is off the panel until
    it is resolved.

    The distinction matters because an appointment to a match already on the
    calendar is not cancelled by a status change: a suspended official stays on
    the matches they have already officiated, because that match happened.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class AvailabilityStatus(str, Enum):
    """How free an official is to be booked right now.

    Separate from :class:`RefereeStatus` on purpose: an official can be active
    and still be unavailable for a fortnight, which is a booking question, not
    a question about whether they may officiate at all.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ASSIGNED = "assigned"


class ExperienceLevel(str, Enum):
    """The band an official's experience falls into.

    Derived from ``experience_years`` rather than stored, so the two can never
    disagree. See ``app.referees.crud.experience_level_for``.
    """

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    EXPERIENCED = "experienced"


class CertificationLevel(str, Enum):
    """How far up the certification ladder an official's badge goes.

    Ordered from local to international, but stored as its own name so a
    certificate can be recorded without pretending it is a national one.
    """

    LOCAL = "local"
    STATE = "state"
    NATIONAL = "national"
    INTERNATIONAL = "international"


class OwnerGender(str, Enum):
    """The gender a team owner records.

    Its own enum rather than a member added to :class:`Gender`, because the
    players' ``gender`` column is ``String(10)`` and "prefer_not_to_say" is
    seventeen characters long - widening the shared enum would have quietly
    produced values the players' column cannot hold. The owners' own column is
    ``String(20)``, so the value fits.
    """

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class OwnershipType(str, Enum):
    """How an owner holds their stake in a team.

    The vocabulary of ownership rather than of a person: the same individual
    may own one team outright and co-own another, so the type belongs to the
    ownership relationship as much as it does to the owner.
    """

    INDIVIDUAL_OWNER = "individual_owner"
    CO_OWNER = "co_owner"
    COMPANY_ORGANIZATION = "company_organization"
    FRANCHISE_OWNER = "franchise_owner"


class OwnershipStatus(str, Enum):
    """Whether an ownership is current or over.

    Two different questions, two different places to ask them: the owner-level
    status says whether this person is currently on the register as an owner,
    and the relationship-level status says whether this person currently holds
    *this* team. A retired owner can still be recorded as the owner of a team
    they ran for a decade, and that record is history, not a live claim.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"


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

# The spellings a client actually sends for an official. The umpire numbers are
# the ones that matter in practice: a scorecard says "Umpire 1" or "1st Umpire"
# as often as it says "first_umpire", and a rejected referee_type would mean the
# officials panel could not be filled in at all.
REFEREE_TYPE_ALIASES = {
    "umpire": "first_umpire",
    "umpire 1": "first_umpire",
    "umpire1": "first_umpire",
    "1st umpire": "first_umpire",
    "first umpire": "first_umpire",
    "umpire 2": "second_umpire",
    "umpire2": "second_umpire",
    "2nd umpire": "second_umpire",
    "second umpire": "second_umpire",
    "umpire 3": "third_umpire",
    "umpire3": "third_umpire",
    "3rd umpire": "third_umpire",
    "third umpire": "third_umpire",
    "tv umpire": "third_umpire",
    "umpire 4": "fourth_umpire",
    "umpire4": "fourth_umpire",
    "4th umpire": "fourth_umpire",
    "fourth umpire": "fourth_umpire",
    "match referee": "match_referee",
    "referee": "match_referee",
    "mr": "match_referee",
    "deputy match referee": "deputy_match_referee",
    "deputy referee": "deputy_match_referee",
    "reserve umpire": "reserve_umpire",
    "reserve": "reserve_umpire",
}

# The spellings a client sends for the two new closed value sets. The status
# words are the ones a club's own paperwork uses, so "suspended" and "on hold"
# both have to land on the same stored value.
REFEREE_STATUS_ALIASES = {
    "active": "active",
    "approved": "active",
    "current": "active",
    "inactive": "inactive",
    "on hold": "inactive",
    "on_hold": "inactive",
    "retired": "inactive",
    "suspended": "suspended",
    "banned": "suspended",
}

AVAILABILITY_STATUS_ALIASES = {
    "available": "available",
    "free": "available",
    "unavailable": "unavailable",
    "not available": "unavailable",
    "assigned": "assigned",
    "booked": "assigned",
}

EXPERIENCE_LEVEL_ALIASES = {
    "beginner": "beginner",
    "novice": "beginner",
    "intermediate": "intermediate",
    "experienced": "experienced",
    "expert": "experienced",
}

CERTIFICATION_LEVEL_ALIASES = {
    "local": "local",
    "club": "local",
    "district": "local",
    "state": "state",
    "regional": "state",
    "national": "national",
    "country": "national",
    "international": "international",
    "world": "international",
    "elite": "international",
}

# The spellings a client sends for the owner gender. "prefer not to say" is
# written with spaces in most places it appears, and with underscores in most
# places it is stored, so both have to land on the same value.
OWNER_GENDER_ALIASES = {
    "male": "male",
    "m": "male",
    "man": "male",
    "female": "female",
    "f": "female",
    "woman": "female",
    "other": "other",
    "non-binary": "other",
    "nonbinary": "other",
    "prefer not to say": "prefer_not_to_say",
    "prefer not to disclose": "prefer_not_to_say",
    "prefer_not_to_say": "prefer_not_to_say",
    "prefer not to say (other)": "prefer_not_to_say",
    "not disclosed": "prefer_not_to_say",
    "undisclosed": "prefer_not_to_say",
}

# The spellings a client sends for the kinds of ownership. A club's own
# paperwork says "company" or "organisation" as often as it says
# "company_organization", and the British spelling is common in India too.
OWNERSHIP_TYPE_ALIASES = {
    "individual owner": "individual_owner",
    "individual": "individual_owner",
    "individual_owner": "individual_owner",
    "owner": "individual_owner",
    "sole owner": "individual_owner",
    "co-owner": "co_owner",
    "co owner": "co_owner",
    "coowner": "co_owner",
    "co_owner": "co_owner",
    "partner": "co_owner",
    "company": "company_organization",
    "company / organization": "company_organization",
    "company organization": "company_organization",
    "company_organization": "company_organization",
    "organisation": "company_organization",
    "organization": "company_organization",
    "corporate": "company_organization",
    "franchise owner": "franchise_owner",
    "franchise": "franchise_owner",
    "franchise_owner": "franchise_owner",
}

OWNERSHIP_STATUS_ALIASES = {
    "active": "active",
    "current": "active",
    "live": "active",
    "inactive": "inactive",
    "ended": "inactive",
    "former": "inactive",
    "retired": "inactive",
}

# Which kinds of official may stand in which slot of a match.
#
# The roles are not interchangeable, and the point of recording the role of an
# official is precisely so that the panel can be read: a first umpire who has
# umpired for a decade is not a match referee, and putting one in the match
# referee's seat would put a name in a box that means something else.
#
# A reserve umpire may stand in the square leg - that is what a reserve is for
# - so the two on-field roles accept one another and the reserve. The
# off-field roles (TV umpire, fourth umpire) and the match referee's roles do
# not cross over.
OFFICIAL_ROLE_TYPES: dict[str, tuple[str, ...]] = {
    "first_umpire": ("first_umpire", "second_umpire", "reserve_umpire"),
    "second_umpire": ("first_umpire", "second_umpire", "reserve_umpire"),
    "third_umpire": ("third_umpire", "fourth_umpire"),
    "fourth_umpire": ("third_umpire", "fourth_umpire"),
    "match_referee": ("match_referee", "deputy_match_referee"),
    "deputy_match_referee": ("match_referee", "deputy_match_referee"),
    "reserve_umpire": ("reserve_umpire",),
}

# The role each of a match's three officials panel slots stands in. The panel
# has three seats and each seat is one of these roles; the roles that are not
# in this list (TV umpire, fourth umpire, reserve) are appointments the panel
# does not have a seat for, and are recorded on the match's assignment list.
SLOT_ROLES: dict[str, str] = {
    "referee_1": "first_umpire",
    "referee_2": "second_umpire",
    "match_referee": "match_referee",
}

# The reverse: the role a panel slot shows, for a given assignment role.
ROLE_SLOTS: dict[str, str] = {role: slot for slot, role in SLOT_ROLES.items()}


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
