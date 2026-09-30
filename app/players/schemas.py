from datetime import date
from typing import Any

from pydantic import BaseModel, field_validator

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


class PlayerBase(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: date
    gender: str
    profile_image: str | None = None
    batting_hand: str
    batting_position: str
    bowling_hand: str
    bowling_type: str
    country_id: int
    state_id: int
    city_id: int
    height: float
    weight: float
    country_code: str
    # Optional: a player may be registered before a number is known, and two
    # players may share one. Both cases are handled explicitly - see
    # app.players.identity and the duplicate-check endpoint - rather than by
    # pretending the number is a unique key.
    mobile_number: str | None = None
    email: str

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

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v):
        v = v.lower()
        if v not in ("male", "female", "other"):
            raise ValueError("gender must be male, female, or other")
        return v

    @field_validator("batting_hand")
    @classmethod
    def validate_batting_hand(cls, v):
        mapping = {
            "left": "Left",
            "right": "Right",
            "l": "Left",
            "r": "Right",
        }
        if v.lower() in mapping:
            return mapping[v.lower()]
        raise ValueError("batting_hand must be Left or Right")

    @field_validator("batting_position")
    @classmethod
    def validate_batting_position(cls, v):
        mapping = {
            "opening": "Opening",
            "op": "Opening",
            "middle": "Middle Order",
            "mid": "Middle Order",
            "tail": "Tail Ender",
            "wk": "Wicket Keeper",
            "wicket keeper": "Wicket Keeper",
            "tail ender": "Tail Ender",
            "middle order": "Middle Order",
        }
        if v.lower() in mapping:
            return mapping[v.lower()]
        raise ValueError(
            "batting_position must be Opening, Middle Order, Tail Ender, or Wicket Keeper"
        )

    @field_validator("bowling_hand")
    @classmethod
    def validate_bowling_hand(cls, v):
        mapping = {
            "left": "Left",
            "right": "Right",
            "l": "Left",
            "r": "Right",
        }
        if v.lower() in mapping:
            return mapping[v.lower()]
        raise ValueError("bowling_hand must be Left or Right")

    @field_validator("bowling_type")
    @classmethod
    def validate_bowling_type(cls, v):
        mapping = {
            "fast": "Fast",
            "medium fast": "Medium Fast",
            "mfast": "Medium Fast",
            "medium": "Medium Fast",
            "spin": "Spin",
        }
        if v.lower() in mapping:
            return mapping[v.lower()]
        raise ValueError("bowling_type must be Fast, Medium Fast, or Spin")


class PlayerCreate(PlayerBase):
    team_id: int | None = None
    role: str = "playing_11"
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
    alias: str | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute", "bench"):
            raise ValueError("role must be playing_11, substitute, or bench")
        return v

    @field_validator("alias")
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
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    profile_image: str | None = None
    batting_hand: str | None = None
    batting_position: str | None = None
    bowling_hand: str | None = None
    bowling_type: str | None = None
    country_id: int | None = None
    state_id: int | None = None
    city_id: int | None = None
    height: float | None = None
    weight: float | None = None
    country_code: str | None = None
    mobile_number: str | None = None
    email: str | None = None

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
        v = " ".join(str(v).split())
        if not v:
            raise ValueError("name parts cannot be empty")
        return v


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
    role: str

    class Config:
        from_attributes = True


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


class ResendOTPResponse(BaseModel):
    message: str
    otp_sent: bool


class TeamAssignment(BaseModel):
    team_id: int
    level_id: int
    role: str = "playing_11"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute", "bench"):
            raise ValueError("role must be playing_11, substitute, or bench")
        return v


class TeamAssignmentUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute", "bench"):
            raise ValueError("role must be playing_11, substitute, or bench")
        return v


class PlayerDropdownItem(BaseModel):
    id: int
    player_code: str = ""
    first_name: str
    last_name: str
    full_name: str = ""
    date_of_birth: date
    gender: str
    batting_hand: str
    batting_position: str
    bowling_type: str
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
    country_code: str
    mobile_number: str
    role: str = "playing_11"

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

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute"):
            raise ValueError("role must be playing_11 or substitute")
        return v


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
    matched_on: str


class PlayerDuplicateCheckResponse(BaseModel):
    phone_matches: list[PlayerDuplicateMatch] = []
    name_matches: list[PlayerDuplicateMatch] = []
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
    next_action: str = "none"
    message: str = ""


class PlayerLinkRequest(BaseModel):
    """Confirmation that an existing player is the one being registered."""

    existing_player_id: int
    # Fill in details the existing record is missing (photo, club, DOB) rather
    # than overwriting what it already has.
    update_existing: bool = True
    alias: str | None = None
    team_id: int | None = None
    role: str = "playing_11"


class PlayerAliasCreate(BaseModel):
    alias: str

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
