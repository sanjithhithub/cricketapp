"""What a referee looks like on the wire, and what the officials panel shows.

The referee schemas mirror the player ones field for field where the two are
the same thing (name, date of birth, nationality, photo, phone, email) and drop
everything that is cricket-specific. Every enum is normalised on the way in, so
a client may send "Umpire 1" and the row always stores one of the documented
values - normalising after the fact would be too late, because by then the
aliases are gone.

Three rules run through the whole file:

* a value that validates here is a value the database can hold - the lengths
  are the column lengths, so the server never accepts a request Postgres would
  then truncate;
* a derived value is never stored twice. ``experience_level`` is computed from
  ``experience_years`` and ``certification_expired`` from the expiry date, so
  the response can carry them without the row being able to disagree with
  itself;
* the internal notes are only carried for a reader who is allowed to see them.
  The response model always has the field, so a client can rely on it existing;
  for anyone else it is null.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import (
    AVAILABILITY_STATUS_ALIASES,
    CERTIFICATION_LEVEL_ALIASES,
    EXPERIENCE_LEVEL_ALIASES,
    REFEREE_STATUS_ALIASES,
    REFEREE_TYPE_ALIASES,
    AvailabilityStatus,
    CertificationLevel,
    ExperienceLevel,
    RefereeStatus,
    RefereeType,
    coerce_enum,
)
from app.players.identity import normalize_country_code, normalize_mobile
from app.referees.identity import experience_level_for

# A phone number is text, and the separators people actually type - spaces,
# dashes, brackets - are not part of it. Letters are never part of one.
_MOBILE_SHAPE = re.compile(r"^[0-9\s\-().+]+$")
_COUNTRY_CODE_SHAPE = re.compile(r"^\+?[0-9\s\-()]{1,6}$")

# E.164 allows at most fifteen digits in total, country code included.
_E164_MAX_DIGITS = 15

# How many entries a list field may carry, and how long one entry may be.
_MAX_LIST_ITEMS = 20
_MAX_LIST_ITEM_LENGTH = 40


def _coerce_mobile(value: Any) -> str | None:
    """Accept a number or a string and return the normalised digits.

    Older clients send the number as a JSON number. Rejecting those would break
    them, so the value is coerced rather than rejected - but a phone number is
    never a number in this app, so it is stored and compared as text.
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


def _clean_list(value: Any, field: str) -> list[str]:
    """Normalise a list of short strings.

    Trimmed, de-duplicated case-insensitively (keeping the first spelling seen)
    and capped in both length and size. A list field that silently accepted
    "T20, t20, T20" would make two officials look differently equipped from
    two rows that say the same thing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        # A single string is read as a one-item list rather than rejected: a
        # client that sends "T20" meant one specialisation, not a character
        # array.
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{field} must be a list of strings")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if item is None:
            continue
        text = " ".join(str(item).split())
        if not text:
            continue
        if len(text) > _MAX_LIST_ITEM_LENGTH:
            raise ValueError(f"{field} entries must be {_MAX_LIST_ITEM_LENGTH} characters or fewer")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    if len(cleaned) > _MAX_LIST_ITEMS:
        raise ValueError(f"{field} may hold at most {_MAX_LIST_ITEMS} entries")
    return cleaned


class RefereeBase(BaseModel):
    # Lengths mirror the columns in app.referees.models, so a value that
    # validates here is a value the database can hold - the server no longer
    # accepts a request that Postgres would then truncate or reject.
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    date_of_birth: date
    # The country the official is from. `country_code` below is the dial code of
    # the phone number, which is not necessarily this country's. It also serves
    # as the country of the postal address, so no second country is stored.
    country_id: int = Field(..., description="Nationality: the country the official is from.")
    profile_image: str | None = Field(
        None,
        max_length=500,
        description=(
            "Path or URL of the profile photo. Set by the photo upload endpoint; "
            "a link may also be supplied directly."
        ),
    )
    referee_type: RefereeType = Field(
        ...,
        description="What kind of official this person is. Not interchangeable between roles.",
    )
    experience_years: int = Field(
        0,
        ge=0,
        le=100,
        description="Years officiating. 0 means a first-season official.",
    )
    country_code: str = Field(
        "",
        max_length=5,
        description="Dial code of the mobile number, with the plus sign.",
    )
    mobile_number: str | None = Field(None, max_length=20)
    email: str = Field(..., max_length=100)

    # --- professional --------------------------------------------------------
    experience_level: ExperienceLevel | None = Field(
        None,
        description=(
            "The band the experience falls into. Derived from `experience_years` "
            "(0-2 beginner, 3-7 intermediate, 8+ experienced); sending a value "
            "that contradicts the years is refused."
        ),
    )
    certification_level: CertificationLevel | None = Field(
        None, description="How far up the certification ladder the official's badge goes."
    )
    certifying_authority: str | None = Field(None, max_length=120)
    certification_number: str | None = Field(None, max_length=60)
    certification_expiry_date: date | None = Field(
        None, description="When the certification lapses. Null when it does not lapse."
    )
    specializations: list[str] = Field(
        default_factory=list,
        description="Formats and competitions the official knows, e.g. T20, ODI, Test, Domestic.",
    )
    languages: list[str] = Field(default_factory=list, description="Languages the official speaks.")

    # --- contact ------------------------------------------------------------
    address: str | None = Field(None, max_length=300)
    state_id: int | None = Field(None, description="State of the postal address.")
    city_id: int | None = Field(None, description="City of the postal address.")
    postal_code: str | None = Field(None, max_length=20)

    # --- administrative ------------------------------------------------------
    status: RefereeStatus = Field(
        RefereeStatus.ACTIVE,
        description="Whether the official may be appointed to a match at all.",
    )
    availability_status: AvailabilityStatus = Field(
        AvailabilityStatus.AVAILABLE,
        description="Whether the official is free to be booked right now.",
    )
    joining_date: date | None = Field(None, description="When the official joined the register.")
    notes: str | None = Field(
        None,
        max_length=2000,
        description="Internal administrative notes. Returned to administrators only.",
    )

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        # Checked before it is normalised, because normalisation throws the
        # offending characters away: "98123abc4567" normalises to a valid
        # eleven-digit number, and the typo would never be reported.
        if v is None or v == "":
            return None
        text = str(v).strip()
        if not text:
            return None
        if not _MOBILE_SHAPE.match(text):
            raise ValueError(
                "mobile_number must contain only digits, spaces and the usual separators"
            )
        digits = re.sub(r"\D", "", text)
        if len(digits) < 4:
            raise ValueError("mobile_number is too short")
        if len(digits) > _E164_MAX_DIGITS:
            raise ValueError(f"mobile_number must be at most {_E164_MAX_DIGITS} digits")
        return text

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        if v is None or v == "":
            return ""
        text = str(v).strip()
        if not text:
            return ""
        if not _COUNTRY_CODE_SHAPE.match(text):
            raise ValueError("country_code must be a calling code such as +91")
        digits = re.sub(r"\D", "", text)
        if not 1 <= len(digits) <= 4:
            raise ValueError("country_code must be a calling code such as +91")
        return text

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_name(cls, v):
        # Trimmed but never made unique: two officials are allowed to share a
        # name, and the name is display data, not identity.
        v = " ".join(str(v or "").split())
        if not v:
            raise ValueError("name parts cannot be empty")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, v):
        if v > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        # Deliberately lenient: an address is checked for the shape of one, not
        # for whether the mailbox exists. A club address like "umpires@" with a
        # real domain is fine, and a rule stricter than "there is a local part,
        # an @ and a domain" starts refusing addresses people really use.
        v = str(v or "").strip().lower()
        local, _, domain = v.partition("@")
        if not local or not domain or "." not in domain or " " in v:
            raise ValueError("email must be a valid address")
        return v

    @field_validator("referee_type", mode="before")
    @classmethod
    def validate_referee_type(cls, v):
        return coerce_enum(RefereeType, v, REFEREE_TYPE_ALIASES)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        return coerce_enum(RefereeStatus, v, REFEREE_STATUS_ALIASES)

    @field_validator("availability_status", mode="before")
    @classmethod
    def validate_availability_status(cls, v):
        return coerce_enum(AvailabilityStatus, v, AVAILABILITY_STATUS_ALIASES)

    @field_validator("certification_level", mode="before")
    @classmethod
    def validate_certification_level(cls, v):
        return _optional_enum(CertificationLevel, v, CERTIFICATION_LEVEL_ALIASES)

    @field_validator("experience_level", mode="before")
    @classmethod
    def validate_experience_level(cls, v):
        return _optional_enum(ExperienceLevel, v, EXPERIENCE_LEVEL_ALIASES)

    @field_validator("specializations", mode="before")
    @classmethod
    def validate_specializations(cls, v):
        return _clean_list(v, "specializations")

    @field_validator("languages", mode="before")
    @classmethod
    def validate_languages(cls, v):
        return _clean_list(v, "languages")

    @model_validator(mode="after")
    def validate_phone_against_country_code(self):
        """The number and the calling code have to make one number together.

        E.164 allows fifteen digits in total. A national number that is long
        enough on its own but too long once the country code is added is a
        number nobody can dial, so it is refused here rather than stored.
        """
        code_digits = len(re.sub(r"\D", "", self.country_code or ""))
        number_digits = len(re.sub(r"\D", "", self.mobile_number or ""))
        if code_digits and number_digits:
            if code_digits + number_digits > _E164_MAX_DIGITS:
                raise ValueError(
                    "mobile_number and country_code together must be at most "
                    f"{_E164_MAX_DIGITS} digits"
                )
        return self

    @model_validator(mode="after")
    def validate_experience_level_matches_years(self):
        """The band and the years must agree.

        The band is derived, so a request that states a different one is
        contradicting itself. It is refused rather than silently corrected: the
        client sent both numbers and one of them is wrong, and which one is not
        something the server can guess.
        """
        if self.experience_level is None:
            return self
        derived = experience_level_for(self.experience_years)
        if self.experience_level.value != derived:
            raise ValueError(
                f"experience_level must be {derived!r} for {self.experience_years} years of "
                "experience; it is derived from the years and cannot be set independently"
            )
        return self


class RefereeCreate(RefereeBase):
    """The body of ``POST /referees``.

    The mobile number is required here even though the column allows null:
    a column stays nullable because rows written before this field existed are
    still in the table, not because a new registration may leave it out.
    """

    mobile_number: str = Field(
        ...,
        max_length=20,
        description="The number as it should be dialled, without the country code.",
    )


class RefereeUpdate(BaseModel):
    """A partial referee update, validated exactly as creation is.

    Every enum validator here is present on purpose: a PATCH that could write
    "Umpire 1" into a row that POST would have refused would leave a referee
    whose type no longer matches anything the API documents.
    """

    first_name: str | None = Field(None, min_length=1, max_length=50)
    last_name: str | None = Field(None, min_length=1, max_length=50)
    date_of_birth: date | None = None
    country_id: int | None = None
    profile_image: str | None = Field(None, max_length=500)
    referee_type: RefereeType | None = None
    experience_years: int | None = Field(None, ge=0, le=100)
    experience_level: ExperienceLevel | None = None
    country_code: str | None = Field(None, max_length=5)
    mobile_number: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=100)

    certification_level: CertificationLevel | None = None
    certifying_authority: str | None = Field(None, max_length=120)
    certification_number: str | None = Field(None, max_length=60)
    certification_expiry_date: date | None = None
    specializations: list[str] | None = None
    languages: list[str] | None = None

    address: str | None = Field(None, max_length=300)
    state_id: int | None = None
    city_id: int | None = None
    postal_code: str | None = Field(None, max_length=20)

    status: RefereeStatus | None = None
    availability_status: AvailabilityStatus | None = None
    joining_date: date | None = None
    notes: str | None = Field(None, max_length=2000)

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        if not text:
            return None
        if not _MOBILE_SHAPE.match(text):
            raise ValueError(
                "mobile_number must contain only digits, spaces and the usual separators"
            )
        digits = re.sub(r"\D", "", text)
        if len(digits) < 4:
            raise ValueError("mobile_number is too short")
        if len(digits) > _E164_MAX_DIGITS:
            raise ValueError(f"mobile_number must be at most {_E164_MAX_DIGITS} digits")
        return text

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        if not text:
            return ""
        if not _COUNTRY_CODE_SHAPE.match(text):
            raise ValueError("country_code must be a calling code such as +91")
        digits = re.sub(r"\D", "", text)
        if not 1 <= len(digits) <= 4:
            raise ValueError("country_code must be a calling code such as +91")
        return text

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_name(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        if not v:
            raise ValueError("name parts cannot be empty")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, v):
        if v is None:
            return None
        if v > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if v is None:
            return None
        v = str(v).strip().lower()
        local, _, domain = v.partition("@")
        if not local or not domain or "." not in domain or " " in v:
            raise ValueError("email must be a valid address")
        return v

    @field_validator("referee_type", mode="before")
    @classmethod
    def validate_referee_type(cls, v):
        return _optional_enum(RefereeType, v, REFEREE_TYPE_ALIASES)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        return _optional_enum(RefereeStatus, v, REFEREE_STATUS_ALIASES)

    @field_validator("availability_status", mode="before")
    @classmethod
    def validate_availability_status(cls, v):
        return _optional_enum(AvailabilityStatus, v, AVAILABILITY_STATUS_ALIASES)

    @field_validator("certification_level", mode="before")
    @classmethod
    def validate_certification_level(cls, v):
        return _optional_enum(CertificationLevel, v, CERTIFICATION_LEVEL_ALIASES)

    @field_validator("experience_level", mode="before")
    @classmethod
    def validate_experience_level(cls, v):
        return _optional_enum(ExperienceLevel, v, EXPERIENCE_LEVEL_ALIASES)

    @field_validator("specializations", mode="before")
    @classmethod
    def validate_specializations(cls, v):
        if v is None:
            return None
        return _clean_list(v, "specializations")

    @field_validator("languages", mode="before")
    @classmethod
    def validate_languages(cls, v):
        if v is None:
            return None
        return _clean_list(v, "languages")


class RefereeResponse(RefereeBase):
    id: int
    # Permanent, human-quotable identity. Every official has one for life.
    referee_code: str = ""
    full_name: str = ""
    # The name of the country the official is from, so a client does not have to
    # join the reference data itself to label the nationality.
    nationality: str | None = None
    # The masked number, for a list or a picker. `mobile_number` above is the
    # number as it was typed.
    phone_display: str | None = None
    # True when the certification has a date and that date has passed. Derived
    # on the way out rather than stored, so it cannot go stale.
    certification_expired: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Who put the official on the register and who last touched the record.
    created_by: str | None = None
    updated_by: str | None = None

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        # Overridden deliberately. A response carries the number as it is: the
        # full one for an admin or the account that owns the record, the masked
        # one for everyone else. Re-normalising it here would strip the
        # asterisks out of a masked number and leave the digits on show - the
        # mask would be undone by the very schema meant to apply it.
        return v

    model_config = ConfigDict(from_attributes=True)


class RefereeOption(BaseModel):
    """One row of the referee picker.

    Small on purpose: a picker needs enough to tell two officials apart - the
    photo, the name and the type - and not enough to hand out contact details
    to everyone who can open the dropdown.
    """

    id: int
    referee_code: str = ""
    full_name: str = ""
    profile_image: str | None = None
    referee_type: RefereeType
    experience_years: int = 0
    # Carried so a picker can grey out an official who cannot be appointed.
    status: RefereeStatus = RefereeStatus.ACTIVE
    availability_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    model_config = ConfigDict(from_attributes=True)


class RefereeStats(BaseModel):
    """The counts the referee register's header shows.

    One query per count rather than a scan of the whole table in Python: the
    numbers are read on every load of the page.
    """

    total: int = 0
    active: int = 0
    inactive: int = 0
    suspended: int = 0
    available: int = 0
    unavailable: int = 0
    assigned: int = 0


class RefereeDeleteCheck(BaseModel):
    """What deleting an official would do.

    Answered before the delete is attempted, so the confirmation dialog can say
    what is going to happen instead of discovering it afterwards.
    """

    referee_id: int
    can_delete: bool
    # Matches this official is named on, split into the ones still to come and
    # the ones already played.
    upcoming_matches: int = 0
    historical_matches: int = 0
    # Why the delete is refused, in one sentence.
    reason: str | None = None


class RefereeProfileImageResponse(BaseModel):
    """200 from ``POST /referees/{referee_id}/profile-photo``.

    ``profile_image`` is a root-relative path (``/uploads/referees/32.png``),
    routable as-is from any page and also the location the CDN redirect serves
    from.
    """

    referee_id: int
    profile_image: str


class RefereeCreateResponse(RefereeResponse):
    message: str


# --- the officials panel of a match -----------------------------------------


class MatchOfficialInfo(BaseModel):
    """One slot of a match's officials panel.

    The slot is always present, whether or not an official has been linked to
    it. `name` is the name on the slot - the linked official's name when there
    is one, otherwise the free-text name the match has always been able to
    carry. Every other field describes the linked official and is null when
    there is none, so a client can render a half-filled panel without a
    special case for "name only".
    """

    slot: str
    label: str
    name: str | None = None
    referee_id: int | None = None
    referee_code: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    date_of_birth: date | None = None
    nationality: str | None = None
    profile_image: str | None = None
    referee_type: RefereeType | None = None
    experience_years: int | None = None
    mobile_number: str | None = None
    email: str | None = None
    status: RefereeStatus | None = None
    availability_status: AvailabilityStatus | None = None


class MatchRefereeAssignmentInfo(BaseModel):
    """One appointment: an official, a match and the role they stand in.

    The panel's three seats are a convenience; this is the whole list, and it
    carries the roles the panel has no seat for - the TV umpire, the fourth
    umpire, the reserve.
    """

    id: int
    match_id: int
    role: RefereeType
    referee_id: int
    referee_code: str | None = None
    full_name: str | None = None
    profile_image: str | None = None
    referee_type: RefereeType | None = None
    status: RefereeStatus | None = None
    assigned_at: datetime | None = None


class MatchOfficials(BaseModel):
    """The officials of a match.

    The three slots are always present, never null: a match with no officials
    yet reports three empty slots rather than no panel at all. `assignments` is
    the full appointment list behind them.
    """

    referee_1: MatchOfficialInfo | None = None
    referee_2: MatchOfficialInfo | None = None
    match_referee: MatchOfficialInfo | None = None
    assignments: list[MatchRefereeAssignmentInfo] = Field(default_factory=list)


class MatchOfficialAssignRequest(BaseModel):
    """The body of ``POST /matches/{match_id}/officials``."""

    referee_id: int = Field(..., description="The official to appoint.")
    role: RefereeType = Field(
        ...,
        description=(
            "The seat the official takes. Must be a role their own type can fill: "
            "an on-field umpire may not be appointed as the match referee."
        ),
    )


class MatchOfficialAssignResponse(BaseModel):
    """201 from ``POST /matches/{match_id}/officials``.

    `warnings` carries the things that were not refused but are worth saying
    out loud - an official who is already booked on another match at the same
    time, for instance. The appointment itself is the assignment.
    """

    assignment: MatchRefereeAssignmentInfo
    officials: MatchOfficials
    warnings: list[str] = Field(default_factory=list)


class RefereeMatchInfo(BaseModel):
    """One match an official has been appointed to."""

    assignment_id: int
    match_id: int
    role: RefereeType
    match_date: date
    match_time: str
    venue: str
    match_type: str
    status: str
    team_a: str | None = None
    team_b: str | None = None
    team_a_id: int | None = None
    team_b_id: int | None = None
    assigned_at: datetime | None = None
