"""What a team owner looks like on the wire.

The owner schemas follow the same three rules as the rest of the app:

* a value that validates here is a value the database can hold - the lengths
  are the column lengths, so the server never accepts a request Postgres would
  then truncate;
* an enum is normalised on the way in, so a client may send "Co Owner" and the
  row always stores one of the documented values - normalising after the fact
  would be too late, because by then the aliases are gone;
* the internal notes are only carried for a reader who is allowed to see them.
  The response model always has the field, so a client can rely on it
  existing; for anyone else it is null.

The ownership fields are split the way the tables are split. The owner carries
its own standing - the kind of owner it is, whether it is on the register, the
organisation it owns through - and the *stakes* it holds are a list, one entry
per team, each with its own type, percentage, dates and status.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from app.enums import (
    OWNER_GENDER_ALIASES,
    OWNERSHIP_STATUS_ALIASES,
    OWNERSHIP_TYPE_ALIASES,
    OwnerGender,
    OwnershipStatus,
    OwnershipType,
    coerce_enum,
)
from app.players.identity import normalize_country_code, normalize_mobile

# A phone number is text, and the separators people actually type - spaces,
# dashes, brackets - are not part of it. Letters are never part of one.
_MOBILE_SHAPE = re.compile(r"^[0-9\s\-().+]+$")
_COUNTRY_CODE_SHAPE = re.compile(r"^\+?[0-9\s\-()]{1,6}$")

# E.164 allows at most fifteen digits in total, country code included.
_E164_MAX_DIGITS = 15

# The ownership types that are held through a registered organisation. These
# are the ones that have to name the organisation and its registration number.
_ORGANISATION_TYPES = (
    OwnershipType.COMPANY_ORGANIZATION.value,
    OwnershipType.FRANCHISE_OWNER.value,
)


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


def _raise_field_errors(title: str, errors: list[tuple[str, str]]) -> None:
    """Report a failure against the fields that caused it, not against the body.

    A model validator that raises a plain ``ValueError`` is reported against
    the whole request, which tells the client nothing about what to change.
    This builds the error the way Pydantic's own field validators do, so the
    response names the field that is wrong - ``organization_name`` rather
    than ``body`` - and the front end can put the message next to the input
    it belongs to.
    """
    raise ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(
                type=PydanticCustomError("value_error", message),
                loc=(field,),
                input=None,
            )
            for field, message in errors
        ],
    )


class TeamOwnershipInput(BaseModel):
    """One stake, as a client describes it: a team and the terms of the stake.

    Every field except the team is optional. A stake with no stated type takes
    the owner's own type, and a stake with no stated status is live - which is
    what a client means when it hands over a team and an owner.
    """

    team_id: int = Field(..., description="The team being taken on.")
    ownership_type: OwnershipType | None = Field(
        None, description="The kind of stake. Defaults to the owner's own type."
    )
    ownership_percentage: float | None = Field(
        None,
        ge=0,
        le=100,
        description="How much of the team this stake is, as a percentage.",
    )
    ownership_start_date: date | None = Field(None, description="When the stake began.")
    ownership_end_date: date | None = Field(None, description="When the stake ended.")
    ownership_status: OwnershipStatus | None = Field(
        None, description="Whether the stake is live. Defaults to active."
    )

    @field_validator("ownership_type", mode="before")
    @classmethod
    def validate_ownership_type(cls, v):
        return _optional_enum(OwnershipType, v, OWNERSHIP_TYPE_ALIASES)

    @field_validator("ownership_status", mode="before")
    @classmethod
    def validate_ownership_status(cls, v):
        return _optional_enum(OwnershipStatus, v, OWNERSHIP_STATUS_ALIASES)

    @model_validator(mode="after")
    def validate_dates(self):
        if (
            self.ownership_start_date
            and self.ownership_end_date
            and self.ownership_end_date < self.ownership_start_date
        ):
            raise ValueError("ownership_end_date cannot be before ownership_start_date")
        return self


class TeamOwnerBase(BaseModel):
    # Lengths mirror the columns in app.team_owners.models, so a value that
    # validates here is a value the database can hold.
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    date_of_birth: date = Field(..., description="Never in the future.")
    gender: OwnerGender = Field(..., description="The gender the owner records.")
    country_id: int = Field(..., description="Nationality: the country the owner is from.")
    profile_image: str | None = Field(
        None,
        max_length=500,
        description=(
            "Path or URL of the profile photo. Set by the photo upload endpoint; "
            "a link may also be supplied directly."
        ),
    )

    email: str = Field(..., max_length=100)
    country_code: str = Field(
        "",
        max_length=5,
        description="Dial code of the mobile number, with the plus sign.",
    )
    mobile_number: str | None = Field(None, max_length=20)

    ownership_type: OwnershipType = Field(
        ...,
        description="The kind of owner this person generally is, and the default for a new stake.",
    )
    ownership_status: OwnershipStatus = Field(
        OwnershipStatus.ACTIVE,
        description="Whether this person is currently on the register as an owner.",
    )
    organization_name: str | None = Field(
        None,
        max_length=150,
        description="The organisation the ownership is held through. Required for a company or a franchise.",
    )
    registration_number: str | None = Field(
        None,
        max_length=80,
        description="The organisation's registration number. Required for a company or a franchise.",
    )

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
        # Trimmed but never made unique: two owners are allowed to share a
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
        # for whether the mailbox exists. A club address like "owners@" with a
        # real domain is fine, and a rule stricter than "there is a local part,
        # an @ and a domain" starts refusing addresses people really use.
        v = str(v or "").strip().lower()
        local, _, domain = v.partition("@")
        if not local or not domain or "." not in domain or " " in v:
            raise ValueError("email must be a valid address")
        return v

    @field_validator("gender", mode="before")
    @classmethod
    def validate_gender(cls, v):
        return coerce_enum(OwnerGender, v, OWNER_GENDER_ALIASES)

    @field_validator("ownership_type", mode="before")
    @classmethod
    def validate_ownership_type(cls, v):
        return coerce_enum(OwnershipType, v, OWNERSHIP_TYPE_ALIASES)

    @field_validator("ownership_status", mode="before")
    @classmethod
    def validate_ownership_status(cls, v):
        return coerce_enum(OwnershipStatus, v, OWNERSHIP_STATUS_ALIASES)

    @field_validator("organization_name", "registration_number")
    @classmethod
    def strip_optional_text(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        return v or None

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
    def validate_organisation_details(self):
        """A company or a franchise has to say which company it is.

        The registration number is what a league checks before it hands over a
        franchise, so it is required alongside the name rather than instead of
        it. A person who owns a team in their own name has neither.

        The failure is reported against the fields that are missing, so a
        client is told which input to fill in rather than that "something
        about the body is wrong".
        """
        if self.ownership_type.value in _ORGANISATION_TYPES:
            errors: list[tuple[str, str]] = []
            if not self.organization_name:
                errors.append(
                    (
                        "organization_name",
                        "organization_name is required for a company or franchise ownership",
                    )
                )
            if not self.registration_number:
                errors.append(
                    (
                        "registration_number",
                        "registration_number is required for a company or franchise ownership",
                    )
                )
            if errors:
                _raise_field_errors(type(self).__name__, errors)
        return self


class TeamOwnerCreate(TeamOwnerBase):
    """The body of ``POST /team-owners``.

    The mobile number is required here even though the column allows null:
    a column stays nullable because rows written before this field existed are
    still in the table, not because a new registration may leave it out.
    """

    mobile_number: str = Field(
        ...,
        max_length=20,
        description="The subscriber's number, without the country code.",
    )
    ownerships: list[TeamOwnershipInput] = Field(
        default_factory=list,
        description=(
            "The teams this owner takes on at the same time. Optional: an owner "
            "may be registered before any team is assigned to them."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "A person who owns a club in their own name",
                    "value": {
                        "first_name": "Anil",
                        "last_name": "Sharma",
                        "date_of_birth": "1975-06-02",
                        "gender": "male",
                        "country_id": 1,
                        "email": "anil.sharma@example.com",
                        "country_code": "+91",
                        "mobile_number": "9812345670",
                        "ownership_type": "individual_owner",
                        "ownership_status": "active",
                        "notes": "Took over from the founding family in 2019.",
                        "ownerships": [
                            {
                                "team_id": 1,
                                "ownership_type": "individual_owner",
                                "ownership_percentage": 100,
                                "ownership_start_date": "2019-04-01",
                                "ownership_status": "active",
                            }
                        ],
                    },
                },
                {
                    "summary": "A company that owns a franchise",
                    "value": {
                        "first_name": "Meera",
                        "last_name": "Iyer",
                        "date_of_birth": "1980-11-19",
                        "gender": "female",
                        "country_id": 1,
                        "email": "meera.iyer@example.com",
                        "country_code": "+91",
                        "mobile_number": "9822004455",
                        "ownership_type": "company_organization",
                        "ownership_status": "active",
                        "organization_name": "Iyer Sports Ventures Pvt Ltd",
                        "registration_number": "U74999MH2021PTC000000",
                    },
                },
            ]
        }
    )


class TeamOwnerUpdate(BaseModel):
    """The body of ``PATCH /team-owners/{id}``.

    Every field is optional, and a field left out is left as it is. A field
    sent as null is a field the caller wants cleared - except for the ones the
    record cannot do without, which are left alone rather than destroyed.
    """

    first_name: str | None = Field(None, min_length=1, max_length=50)
    last_name: str | None = Field(None, min_length=1, max_length=50)
    date_of_birth: date | None = None
    gender: OwnerGender | None = None
    country_id: int | None = None
    profile_image: str | None = Field(None, max_length=500)
    email: str | None = Field(None, max_length=100)
    country_code: str | None = Field(None, max_length=5)
    mobile_number: str | None = Field(None, max_length=20)
    ownership_type: OwnershipType | None = None
    ownership_status: OwnershipStatus | None = None
    organization_name: str | None = Field(None, max_length=150)
    registration_number: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=2000)
    ownerships: list[TeamOwnershipInput] | None = Field(
        None,
        description=(
            "The owner's stakes, as the caller wants them to be. A stake that is "
            "new is created, a stake that is already live is changed, and a live "
            "stake that is missing from this list is ended rather than deleted."
        ),
    )

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
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
        if v is None:
            return None
        v = " ".join(str(v or "").split())
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

    @field_validator("gender", mode="before")
    @classmethod
    def validate_gender(cls, v):
        return _optional_enum(OwnerGender, v, OWNER_GENDER_ALIASES)

    @field_validator("ownership_type", mode="before")
    @classmethod
    def validate_ownership_type(cls, v):
        return _optional_enum(OwnershipType, v, OWNERSHIP_TYPE_ALIASES)

    @field_validator("ownership_status", mode="before")
    @classmethod
    def validate_ownership_status(cls, v):
        return _optional_enum(OwnershipStatus, v, OWNERSHIP_STATUS_ALIASES)

    @field_validator("organization_name", "registration_number")
    @classmethod
    def strip_optional_text(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        return v or None

    @model_validator(mode="after")
    def validate_phone_against_country_code(self):
        code_digits = len(re.sub(r"\D", "", self.country_code or ""))
        number_digits = len(re.sub(r"\D", "", self.mobile_number or ""))
        if code_digits and number_digits:
            if code_digits + number_digits > _E164_MAX_DIGITS:
                raise ValueError(
                    "mobile_number and country_code together must be at most "
                    f"{_E164_MAX_DIGITS} digits"
                )
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "Change the phone number and the notes",
                    "value": {
                        "country_code": "+44",
                        "mobile_number": "7700900123",
                        "notes": "Now based in London.",
                    },
                },
                {
                    "summary": "Change a stake and end the other",
                    "value": {
                        "ownerships": [
                            {
                                "team_id": 1,
                                "ownership_type": "co_owner",
                                "ownership_percentage": 35,
                                "ownership_start_date": "2024-01-01",
                                "ownership_status": "active",
                            }
                        ]
                    },
                    "description": (
                        "A live stake that is missing from this list is ended, "
                        "not deleted: the team's history keeps it."
                    ),
                },
            ]
        }
    )


class TeamOwnershipResponse(BaseModel):
    """One stake, as it is read back.

    The team's name and logo travel with it so the details screen does not
    have to join the teams table itself to label a stake.
    """

    id: int
    team_id: int
    owner_id: int
    team_name: str = ""
    team_short_name: str = ""
    team_logo: str | None = None
    ownership_type: OwnershipType
    ownership_percentage: float | None = None
    ownership_start_date: date | None = None
    ownership_end_date: date | None = None
    ownership_status: OwnershipStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TeamOwnerTeamSummary(BaseModel):
    """A team the owner is connected to, without the terms of the stake."""

    id: int
    name: str
    short_name: str = ""
    logo: str | None = None
    ownership_type: OwnershipType | None = None
    ownership_status: OwnershipStatus | None = None


class TeamOwnerResponse(TeamOwnerBase):
    id: int
    full_name: str = ""
    # The name of the country the owner is from, so a client does not have to
    # join the reference data itself to label the nationality.
    nationality: str | None = None
    # The masked number, for a list or a picker. `mobile_number` above is the
    # number as it was typed.
    phone_display: str | None = None
    # The stakes this owner holds, one per team.
    ownerships: list[TeamOwnershipResponse] = []
    # The teams the owner is connected to, without repeating the terms.
    teams: list[TeamOwnerTeamSummary] = []
    # The owner's share of their primary stake - the live one they held
    # longest - carried up from the stake list so a list row can show it
    # without walking the stakes itself.
    ownership_percentage: float | None = None
    ownership_start_date: date | None = None
    ownership_end_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
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

    @model_validator(mode="after")
    def validate_organisation_details(self):
        """A response is not a request.

        The rule that a company names its organisation is enforced on the way
        in. On the way out the record is reported as it is stored, so an old
        row that predates the rule is still readable rather than unsendable.
        """
        return self

    model_config = ConfigDict(from_attributes=True)


class TeamOwnerOption(BaseModel):
    """One row of the owner picker.

    Small on purpose: a picker needs enough to tell two owners apart - the
    photo, the name and the kind of owner - and not enough to hand out contact
    details to everyone who can open the dropdown.
    """

    id: int
    full_name: str = ""
    profile_image: str | None = None
    ownership_type: OwnershipType
    ownership_status: OwnershipStatus = OwnershipStatus.ACTIVE

    model_config = ConfigDict(from_attributes=True)


class TeamOwnerStats(BaseModel):
    """The counts the owners register's header shows.

    One query per count rather than a scan of the whole table in Python: the
    numbers are read on every load of the page.
    """

    total: int = 0
    active: int = 0
    inactive: int = 0
    # Owners who hold at least one live stake.
    with_teams: int = 0
    # The account's teams that have no live owner at all.
    teams_without_owners: int = 0


class TeamOwnerDeleteCheck(BaseModel):
    """What deleting an owner would do.

    Answered before the delete is attempted, so the confirmation dialog can say
    what is going to happen instead of discovering it afterwards.
    """

    owner_id: int
    can_delete: bool
    # The stakes this owner holds, split into the live ones and the historical
    # ones.
    active_ownerships: int = 0
    historical_ownerships: int = 0
    # Why the delete is refused, in one sentence.
    reason: str | None = None


class TeamOwnerProfileImageResponse(BaseModel):
    """200 from ``POST /team-owners/{owner_id}/profile-photo``.

    ``profile_image`` is a root-relative path (``/uploads/team_owners/32.png``),
    routable as-is from any page and also the location the CDN redirect serves
    from.
    """

    owner_id: int
    profile_image: str


class TeamOwnerCreateResponse(TeamOwnerResponse):
    message: str


class TeamOwnerAssignRequest(BaseModel):
    """The body of ``POST /teams/{team_id}/owners``.

    The team is in the path, so it is not repeated in the body; the owner is
    named by id.
    """

    owner_id: int = Field(..., description="The owner taking the team on.")
    ownership_type: OwnershipType | None = Field(
        None, description="The kind of stake. Defaults to the owner's own type."
    )
    ownership_percentage: float | None = Field(
        None,
        ge=0,
        le=100,
        description="How much of the team this stake is, as a percentage.",
    )
    ownership_start_date: date | None = Field(None, description="When the stake began.")
    ownership_end_date: date | None = Field(None, description="When the stake ended.")
    ownership_status: OwnershipStatus | None = Field(
        None, description="Whether the stake is live. Defaults to active."
    )

    @field_validator("ownership_type", mode="before")
    @classmethod
    def validate_ownership_type(cls, v):
        return _optional_enum(OwnershipType, v, OWNERSHIP_TYPE_ALIASES)

    @field_validator("ownership_status", mode="before")
    @classmethod
    def validate_ownership_status(cls, v):
        return _optional_enum(OwnershipStatus, v, OWNERSHIP_STATUS_ALIASES)

    @model_validator(mode="after")
    def validate_dates(self):
        if (
            self.ownership_start_date
            and self.ownership_end_date
            and self.ownership_end_date < self.ownership_start_date
        ):
            raise ValueError("ownership_end_date cannot be before ownership_start_date")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "A co-owner taking 40% of the club",
                    "value": {
                        "owner_id": 1,
                        "ownership_type": "co_owner",
                        "ownership_percentage": 40,
                        "ownership_start_date": "2026-04-01",
                    },
                }
            ]
        }
    )


class TeamOwnerRelationshipUpdate(BaseModel):
    """The body of ``PATCH /teams/{team_id}/owners/{owner_id}``.

    The terms of one stake. The owner and the team are in the path, so they
    are not repeated here.
    """

    ownership_type: OwnershipType | None = None
    ownership_percentage: float | None = Field(None, ge=0, le=100)
    ownership_start_date: date | None = None
    ownership_end_date: date | None = None
    ownership_status: OwnershipStatus | None = None

    @field_validator("ownership_type", mode="before")
    @classmethod
    def validate_ownership_type(cls, v):
        return _optional_enum(OwnershipType, v, OWNERSHIP_TYPE_ALIASES)

    @field_validator("ownership_status", mode="before")
    @classmethod
    def validate_ownership_status(cls, v):
        return _optional_enum(OwnershipStatus, v, OWNERSHIP_STATUS_ALIASES)

    @model_validator(mode="after")
    def validate_dates(self):
        if (
            self.ownership_start_date
            and self.ownership_end_date
            and self.ownership_end_date < self.ownership_start_date
        ):
            raise ValueError("ownership_end_date cannot be before ownership_start_date")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "Reduce a stake to 25%",
                    "value": {"ownership_percentage": 25},
                }
            ]
        }
    )


class TeamOwnerOnTeam(BaseModel):
    """One owner as a team's panel shows them.

    The team screen needs the photo, the name, the kind of stake and whether
    it is live - and nothing more. The owner record itself is not copied into
    the team: it lives once, in the owners register, and is read from there.
    """

    owner_id: int
    full_name: str = ""
    profile_image: str | None = None
    ownership_type: OwnershipType
    ownership_percentage: float | None = None
    ownership_status: OwnershipStatus
    ownership_start_date: date | None = None
    ownership_end_date: date | None = None
    ownership_id: int
