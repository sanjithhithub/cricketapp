from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import MatchStatus, MatchType, TossDecision, coerce_enum
from app.referees.schemas import MatchOfficials


class MatchBase(BaseModel):
    match_date: date
    # 24-hour local start time as "HH:MM". A string rather than a time because the
    # venue's timezone is not recorded on the match, so a bare "14:30" is what
    # actually gets displayed next to the date.
    match_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=5)
    venue: str = Field(..., min_length=1, max_length=200)
    match_type: MatchType
    result: str | None = Field(None, max_length=500)
    team_a_id: int
    team_b_id: int
    toss_winner_id: int
    toss_decision: TossDecision
    # The three official slots. An id links a registered match official; the
    # name beside it is a display mirror, kept for accounts that name their
    # officials in plain text. An official must belong to this account, and the
    # same official cannot fill two slots of one match.
    referee_1_id: int | None = Field(
        None, description="The referee standing in the first umpire's slot."
    )
    referee_2_id: int | None = Field(
        None, description="The referee standing in the second umpire's slot."
    )
    match_referee_id: int | None = Field(None, description="The referee in charge of the match.")
    referee_1_name: str | None = Field(None, max_length=100)
    referee_2_name: str | None = Field(None, max_length=100)
    match_referee_name: str | None = Field(None, max_length=100)
    # Player of the match on the completed-match summary. Never derived, may be
    # set to a player from either side, and optional until a match is done.
    player_of_match_id: int | None = None
    # Overs per innings, overriding the format default (T20 20, ODI 50, Test
    # unlimited). Omit or send null to keep the format default; set it for a
    # junior or reduced-overs game. Editable while the match is live.
    total_overs: int | None = Field(
        None,
        ge=1,
        le=200,
        description=(
            "Overs per innings. Null/omitted uses the format default "
            "(T20 20, ODI 50, Test unlimited)."
        ),
    )
    # A level main match goes to a one-over-per-side Super Over instead of
    # a tied result. super_over_repeat keeps playing extra Super Overs (batting
    # order swapped each time) when the Super Over is itself level.
    super_over_enabled: bool = False
    super_over_repeat: bool = True

    @field_validator("toss_decision", mode="before")
    @classmethod
    def validate_toss_decision(cls, v):
        return coerce_enum(TossDecision, v)


class MatchCreate(MatchBase):
    @field_validator("match_type", mode="before")
    @classmethod
    def validate_match_type(cls, v):
        return coerce_enum(MatchType, v)


class MatchUpdate(BaseModel):
    match_date: date | None = None
    match_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=5)
    venue: str | None = Field(None, min_length=1, max_length=200)
    match_type: MatchType | None = None
    result: str | None = Field(None, max_length=500)
    team_a_id: int | None = None
    team_b_id: int | None = None
    toss_winner_id: int | None = None
    toss_decision: TossDecision | None = None
    referee_1_id: int | None = Field(
        None, description="The referee standing in the first umpire's slot."
    )
    referee_2_id: int | None = Field(
        None, description="The referee standing in the second umpire's slot."
    )
    match_referee_id: int | None = Field(None, description="The referee in charge of the match.")
    referee_1_name: str | None = Field(None, max_length=100)
    referee_2_name: str | None = Field(None, max_length=100)
    match_referee_name: str | None = Field(None, max_length=100)
    # Set to raise or lower the over count mid-match, or to null to fall back to
    # the format default. A value below the overs already bowled is refused.
    total_overs: int | None = Field(
        None,
        ge=1,
        le=200,
        description="Overs per innings; null falls back to the format default.",
    )
    super_over_enabled: bool | None = None
    super_over_repeat: bool | None = None
    player_of_match_id: int | None = None

    @field_validator("match_type", mode="before")
    @classmethod
    def validate_match_type(cls, v):
        if v is None:
            return None
        return coerce_enum(MatchType, v)

    @field_validator("toss_decision", mode="before")
    @classmethod
    def validate_toss_decision(cls, v):
        if v is None:
            return None
        return coerce_enum(TossDecision, v)


class MatchTeamInfo(BaseModel):
    id: int
    name: str
    short_name: str

    model_config = ConfigDict(from_attributes=True)


class MatchTournamentInfo(BaseModel):
    """The competition a match belongs to, when it was played as a fixture.

    A match is part of a tournament only if a fixture links to it via
    ``TournamentFixture.match_id``. Standalone matches (created directly from
    the Create Match form) have no tournament, so this stays ``None``.
    """

    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class MatchResponse(MatchBase):
    # Responses echo the stored value rather than re-validating it against the
    # set accepted on input. Older rows may carry a format this build no longer
    # offers (for example "limited_overs"); refusing to serialise one of them
    # used to fail the whole match list with a 500, hiding every other match.
    # Input is still closed: MatchCreate and MatchUpdate validate match_type.
    match_type: str

    id: int
    status: MatchStatus = MatchStatus.SCHEDULED
    current_innings_number: int = 0
    team_a: MatchTeamInfo
    team_b: MatchTeamInfo
    toss_winner: MatchTeamInfo
    player_of_match_id: int | None = None
    # The competition this match belongs to, if any. Populated when a tournament
    # fixture links to this match; ``None`` for a standalone match.
    tournament: MatchTournamentInfo | None = None
    # The officials panel: the three slots, each carrying the official's
    # details when one is linked. Always present, so a client renders the
    # panel without checking for a missing key.
    officials: MatchOfficials = MatchOfficials()

    model_config = ConfigDict(from_attributes=True)
