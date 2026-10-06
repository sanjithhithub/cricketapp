from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.enums import (
    BATTING_POSITION_ALIASES,
    BOWLING_TYPE_ALIASES,
    HAND_ALIASES,
    BattingHand,
    BattingPosition,
    BowlingHand,
    BowlingType,
    DuplicateMatchedOn,
    DuplicateNextAction,
    Gender,
    SquadRole,
    coerce_enum,
)
from app.players.identity import normalize_country_code, normalize_mobile


def _coerce_mobile(value: Any) -> str | None:
    """Accept a number or a string and return the normalised digits.

    Older clients (and the seed scripts) send the number as a JSON number.
    Rejecting those would break them, so the value is coerced rather than
    rejected - but a phone number is never a number in this app again, so it is
    stored and compared as text.
    """
    if value is None or value == "":
        return None
    return normalize_mobile(value)


def _coerce_country_code(value: Any) -> str:
    if value is None or value == "":
        return ""
    return normalize_country_code(value)


def _optional_enum(enum_cls, value: Any, aliases: dict[str, str] | None = None) -> str | None:
    """``coerce_enum`` for an optional field: ``null`` passes through untouched."""
    if value is None:
        return None
    return coerce_enum(enum_cls, value, aliases)


class PlayerBase(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    date_of_birth: date
    gender: Gender
    profile_image: str | None = Field(None, max_length=500)
    batting_hand: BattingHand
    batting_position: BattingPosition
    bowling_hand: BowlingHand
    bowling_type: BowlingType
    country_id: int
    state_id: int
    city_id: int
    height: float = Field(..., gt=0, description="Height in centimetres.")
    weight: float = Field(..., gt=0, description="Weight in kilograms.")
    country_code: str = Field(..., max_length=5, description="Dial code, with the plus sign.")
    # Optional: a player may be registered before a number is known, and two
    # players may share one. Both cases are handled explicitly - see
    # app.players.identity and the duplicate-check endpoint - rather than by
    # pretending the number is a unique key.
    mobile_number: str | None = Field(None, max_length=20)
    email: str = Field(..., max_length=100)

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        return _coerce_mobile(v)

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        return _coerce_country_code(v)

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_name(cls, v):
        # Trimmed but never made unique: two players are allowed to share a name,
        # and the name is display data, not identity.
        v = " ".join(str(v or "").split())
        if not v:
            raise ValueError("name parts cannot be empty")
        return v

    # Every enum field normalises on the way in, before Pydantic picks the member,
    # so a client may send "left", "L" or "Left" and the row always stores one of
    # the documented values. Normalising after the fact would be too late: by then
    # the value is already an enum member and the aliases are gone.
    @field_validator("gender", mode="before")
    @classmethod
    def validate_gender(cls, v):
        return coerce_enum(Gender, v)

    @field_validator("batting_hand", mode="before")
    @classmethod
    def validate_batting_hand(cls, v):
        return coerce_enum(BattingHand, v, HAND_ALIASES)

    @field_validator("batting_position", mode="before")
    @classmethod
    def validate_batting_position(cls, v):
        return coerce_enum(BattingPosition, v, BATTING_POSITION_ALIASES)

    @field_validator("bowling_hand", mode="before")
    @classmethod
    def validate_bowling_hand(cls, v):
        return coerce_enum(BowlingHand, v, HAND_ALIASES)

    @field_validator("bowling_type", mode="before")
    @classmethod
    def validate_bowling_type(cls, v):
        return coerce_enum(BowlingType, v, BOWLING_TYPE_ALIASES)


class PlayerCreate(PlayerBase):
    team_id: int | None = None
    role: SquadRole = SquadRole.PLAYING_11
    # Set by the caller once a human has seen the duplicate candidates and
    # confirmed the registration anyway. Without it, a phone that is already
    # registered is reported back with the matching players instead of being
    # created - the server never silently merges or silently duplicates.
    duplicate_confirmed: bool = False
    # The player the user said this *is*. Used when the duplicate check finds the
    # same person already on file: the existing record is reused rather than a
    # second one being created.
    existing_player_id: int | None = None
    # A nickname / short form to attach to `existing_player_id` or to the new
    # player, so the short name resolves to the same player id.
    alias: str | None = Field(None, max_length=100)

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        return coerce_enum(SquadRole, v)

    @field_validator("alias", mode="before")
    @classmethod
    def validate_alias(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        if not v:
            return None
        if len(v) > 100:
            raise ValueError("alias cannot be longer than 100 characters")
        return v


class PlayerUpdate(BaseModel):
    """A partial player update, validated exactly as creation is.

    Every enum validator here used to be missing. That was not cosmetic: a PATCH
    could write "Leftt" or "middle-order" into a row that POST would have
    refused, and every later GET of that player then failed to serialise - the
    bad value was only ever rejected on the way out.
    """

    first_name: str | None = Field(None, min_length=1, max_length=50)
    last_name: str | None = Field(None, min_length=1, max_length=50)
    date_of_birth: date | None = None
    gender: Gender | None = None
    profile_image: str | None = Field(None, max_length=500)
    batting_hand: BattingHand | None = None
    batting_position: BattingPosition | None = None
    bowling_hand: BowlingHand | None = None
    bowling_type: BowlingType | None = None
    country_id: int | None = None
    state_id: int | None = None
    city_id: int | None = None
    height: float | None = Field(None, gt=0)
    weight: float | None = Field(None, gt=0)
    country_code: str | None = Field(None, max_length=5)
    mobile_number: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=100)

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        return _coerce_mobile(v)

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        if v is None:
            return None
        return _coerce_country_code(v)

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_name(cls, v):
        if v is None:
            return None
        v = " ".join(str(v or "").split())
        if not v:
            raise ValueError("name parts cannot be empty")
        return v

    @field_validator("gender", mode="before")
    @classmethod
    def validate_gender(cls, v):
        return _optional_enum(Gender, v)

    @field_validator("batting_hand", mode="before")
    @classmethod
    def validate_batting_hand(cls, v):
        return _optional_enum(BattingHand, v, HAND_ALIASES)

    @field_validator("batting_position", mode="before")
    @classmethod
    def validate_batting_position(cls, v):
        return _optional_enum(BattingPosition, v, BATTING_POSITION_ALIASES)

    @field_validator("bowling_hand", mode="before")
    @classmethod
    def validate_bowling_hand(cls, v):
        return _optional_enum(BowlingHand, v, HAND_ALIASES)

    @field_validator("bowling_type", mode="before")
    @classmethod
    def validate_bowling_type(cls, v):
        return _optional_enum(BowlingType, v, BOWLING_TYPE_ALIASES)


class PlayerAliasOut(BaseModel):
    alias: str

    class Config:
        from_attributes = True


class PlayerResponse(PlayerBase):
    id: int
    # Permanent, human-quotable identity. Every player has one for life.
    player_code: str = ""
    full_name: str = ""
    is_phone_verified: bool = False
    aliases: list[str] = []
    # Masked in listings and search results; the full number is only returned to
    # an admin or to the user who owns the record (see the routes).
    phone_display: str | None = None

    class Config:
        from_attributes = True


class PlayerTeamInfo(BaseModel):
    team_id: int
    team_name: str
    level_id: int
    level_name: str
    role: SquadRole

    class Config:
        from_attributes = True


class PlayerTeamAssignmentResponse(BaseModel):
    """201 from ``POST /players/{player_id}/teams``."""

    message: str
    assignment: PlayerTeamInfo


class PlayerTeamRoleResponse(BaseModel):
    """200 from ``PATCH /players/{player_id}/teams/{team_id}``."""

    message: str
    player_id: int
    team_id: int
    role: SquadRole


class PlayerProfileImageResponse(BaseModel):
    """200 from ``POST /players/{player_id}/upload-profile-image``.

    ``profile_image`` is a root-relative path (``/uploads/players/32.png``),
    routable as-is from any page and also the location the CDN redirect serves
    from.
    """

    player_id: int
    profile_image: str


class PlayerCreateResponse(PlayerResponse):
    message: str
    otp_sent: bool
    team_assignment: PlayerTeamInfo | None = None
    # True when the registration was recognised as an existing player and that
    # record was reused. The client should treat the returned id as the player's
    # permanent id and not create anything further.
    linked_existing: bool = False
    # Set when the name matches players who already exist and the number did not.
    duplicate_name_warning: str | None = None
    # The nickname that was accepted with this registration, echoed back. It was
    # previously accepted on input and then dropped from the response, so a client
    # could create a player with an alias and never learn which one it got; it is
    # also readable later from `aliases` on the player, or GET /players/{id}/aliases.
    alias: str | None = None


class ResendOTPResponse(BaseModel):
    message: str
    otp_sent: bool


class TeamAssignment(BaseModel):
    team_id: int
    level_id: int
    role: SquadRole = SquadRole.PLAYING_11

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        return coerce_enum(SquadRole, v)


class TeamAssignmentUpdate(BaseModel):
    role: SquadRole

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        return coerce_enum(SquadRole, v)


class PlayerDropdownItem(BaseModel):
    id: int
    player_code: str = ""
    first_name: str
    last_name: str
    full_name: str = ""
    date_of_birth: date
    gender: Gender
    batting_hand: BattingHand
    batting_position: BattingPosition
    bowling_type: BowlingType
    country_code: str
    # Always masked here. A picker has to be able to tell two same-named players
    # apart, and player_code + photo + club do that without handing out a full
    # number to everyone who can read the list.
    mobile_number: str | None = None
    country_name: str | None = None
    state_name: str | None = None
    city_name: str | None = None
    profile_image: str | None = None
    team_name: str | None = None

    class Config:
        from_attributes = True


class TeamPlayerByPhone(BaseModel):
    country_code: str = Field(..., max_length=5)
    mobile_number: str = Field(..., max_length=20)
    # Adding by number offers no bench bucket: the point of the picker is to put
    # a player on the field, and the bench is chosen from the squad afterwards.
    role: SquadRole = SquadRole.PLAYING_11

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        mobile = _coerce_mobile(v)
        if not mobile:
            raise ValueError("mobile_number is required")
        return mobile

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        return _coerce_country_code(v)

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        role = coerce_enum(SquadRole, v)
        if role not in (SquadRole.PLAYING_11.value, SquadRole.SUBSTITUTE.value):
            raise ValueError("role must be playing_11 or substitute")
        return role


# --- duplicate detection -------------------------------------------------


class PlayerDuplicateRequest(BaseModel):
    """What the onboarding form knows so far, for a pre-create duplicate check.

    Every field except the name is optional: the check is useful with as little
    as a name, and gets sharper as more is supplied.
    """

    first_name: str = ""
    last_name: str = ""
    country_code: str = ""
    mobile_number: str | None = None
    date_of_birth: date | None = None
    team_id: int | None = None
    # Advisory only: two players genuinely share an email (a parent's address for
    # two children), so a match is reported but never blocks registration.
    email: str = ""

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        return _coerce_mobile(v)

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        return _coerce_country_code(v)


class PlayerDuplicateMatch(BaseModel):
    """One already-registered player who might be the same person.

    Carries everything a user needs to decide *without* seeing the full mobile
    number of someone else's record: code, name, photo, club, date of birth and
    a masked number.
    """

    id: int
    player_code: str
    first_name: str
    last_name: str
    full_name: str
    profile_image: str | None = None
    date_of_birth: date | None = None
    country_code: str | None = None
    mobile_number: str | None = None
    is_phone_verified: bool = False
    team_name: str | None = None
    aliases: list[str] = []
    # Why this row was returned: "phone" (same normalised number), "name" (same
    # normalised full name) or "alias" (one name is a recorded nickname).
    matched_on: DuplicateMatchedOn


class PlayerDuplicateDetail(BaseModel):
    """The ``detail`` object of a 409 from ``POST /players``.

    The duplicate flow is a conversation, not a rejection: the server reports who
    it matched, says what the client should do next, and refuses to guess whether
    two records are the same person. The client resolves it by resubmitting with
    ``duplicate_confirmed: true`` (a different person who shares a name) or
    ``existing_player_id`` (the same person, whose record is reused).
    """

    message: str
    next_action: DuplicateNextAction
    # Populated for confirm_same_person: the players already registered to this
    # number, each carrying a masked number of their own.
    phone_matches: list[PlayerDuplicateMatch] = []
    # Populated for confirm_duplicate_name: players who share a normalised name.
    name_matches: list[PlayerDuplicateMatch] = []
    # Advisory only: players in this account who share the submitted email.
    # Populated for next_action "none" - the client may show it and continue.
    email_matches: list[PlayerDuplicateMatch] = []


class PlayerDuplicateCheckResponse(BaseModel):
    phone_matches: list[PlayerDuplicateMatch] = []
    name_matches: list[PlayerDuplicateMatch] = []
    # Advisory only: an email address already on file in this account. Reported
    # but never blocks - two players may legitimately share an address.
    email_matches: list[PlayerDuplicateMatch] = []
    # A normalised name that already exists. Warning only: the user is asked to
    # confirm and may proceed. Two people really can share a name.
    duplicate_name: bool = False
    # A phone number already on file. A human has to say whether this is the
    # same person; the server will not guess and will not merge.
    phone_taken: bool = False
    # A player with no number cannot be matched on one, so an explicit
    # confirmation is required before creating a second nameless record.
    requires_confirmation: bool = False
    # "none" | "confirm_same_person" | "confirm_duplicate_name" | "shared_phone"
    next_action: DuplicateNextAction = DuplicateNextAction.NONE
    message: str = ""


class PlayerLinkRequest(BaseModel):
    """Confirmation that an existing player is the one being registered."""

    existing_player_id: int
    # Fill in details the existing record is missing (photo, club, DOB) rather
    # than overwriting what it already has.
    update_existing: bool = True
    alias: str | None = Field(None, max_length=100)
    team_id: int | None = None
    role: SquadRole = SquadRole.PLAYING_11

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v):
        return coerce_enum(SquadRole, v)


class PlayerAliasCreate(BaseModel):
    alias: str = Field(..., max_length=100)

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, v):
        v = " ".join(str(v or "").split())
        if not v:
            raise ValueError("alias cannot be empty")
        if len(v) > 100:
            raise ValueError("alias cannot be longer than 100 characters")
        return v


class PlayerAliasListResponse(BaseModel):
    player_id: int
    player_code: str
    aliases: list[str] = []
