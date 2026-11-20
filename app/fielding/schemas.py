"""Request and response shapes for the fielding endpoints.

The response is deliberately self-contained: it carries the eligible playing
XI, the reserve players who may come on as substitutes, the position catalog
with its anchors, the current assignments, the substitution history and the
substitution rules in force, so the Field Setup screen can be drawn from one
call and never has to guess which positions exist, which players are legal or
what this competition's rules allow.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums import XIRole
from app.fielding.positions import MAX_ACTIVE_FIELDERS, POSITION_KEYS


class FieldingEligiblePlayer(BaseModel):
    """A player the fielding side may name.

    Used for both buckets: the starting XI (``is_substitute`` false) and the
    reserves who may come on (``is_substitute`` true). The panel renders the
    two lists separately, but the shape is identical so a name, a shirt code
    and the captaincy marks look the same in both.
    """

    model_config = {"populate_by_name": True}

    player_id: int
    first_name: str
    last_name: str
    full_name: str
    player_code: str | None = None
    # The player's photo, when one has been uploaded - a root-relative path
    # such as ``/uploads/players/17.png``. Null for the many squads that
    # never uploaded one, so the panel falls back to initials.
    profile_image: str | None = None
    is_captain: bool = False
    is_vice_captain: bool = False
    # The job this player was picked for - wicketkeeper, bowler or fielder -
    # so the panel can label the keeper and show which of the eleven is
    # which instead of listing eleven bare names. Null when the side's XI
    # was assembled before field jobs were recorded, and always null for a
    # reserve (a substitute has no field job).
    xi_role: XIRole | None = None
    # Which squad bucket the player sits in: "playing_11", "substitute" or
    # "bench". The reserves list shows every non-XI squad member, so the
    # panel never has to assume a squad of 11 or 15.
    squad_role: str = "playing_11"
    # True when the player is one of the eleven *on the field* right now.
    # Distinct from on_field: a member who has not been given a position yet
    # is active but not placed, and an XI player who was substituted off is
    # neither.
    is_active: bool = True
    # True when the player is currently on the field as a substitute, i.e.
    # came on through the substitution workflow rather than starting.
    is_substitute: bool = False
    # True when this player currently holds a position on the field. Drives
    # the "placed / not yet placed" marker in the list.
    on_field: bool = False
    # The position the player currently holds, or null when not placed.
    position_key: str | None = None


class FieldingPositionOut(BaseModel):
    """One stored fielder assignment, with where the diagram draws it."""

    player_id: int
    player_name: str
    position_key: str
    position_label: str
    anchor_x: float
    anchor_y: float
    # True when this fielder is on the field as a substitute.
    is_substitute: bool = False
    # Free-text label for a custom position; null for catalog positions.
    custom_label: str | None = None


class FieldingPositionCatalogEntry(BaseModel):
    key: str
    label: str
    anchor_x: float
    anchor_y: float
    depth: str
    # The category the picker groups under: close, off, leg, boundary,
    # special. Drives the grouped dropdown.
    group: str = "special"


class FieldingSubstitutionOut(BaseModel):
    """One recorded substitution - match history, never rewritten."""

    id: int
    match_id: int
    innings_number: int
    team_id: int
    outgoing_player_id: int
    outgoing_player_name: str
    incoming_player_id: int
    incoming_player_name: str
    reason: str
    # "normal" | "special". A normal substitute may not bowl, keep wicket or
    # captain; a special replacement may, when the rules permit it.
    replacement_type: str
    authorized_by_user_id: int | None = None
    created_at: datetime


class SubstitutionReasonOption(BaseModel):
    value: str
    # "normal" or "special" - what the substitution grants.
    type: str = "normal"


class SubstitutionRulesOut(BaseModel):
    """The rules this deployment is running with, for the UI to explain."""

    enabled: bool = True
    reasons: list[SubstitutionReasonOption] = Field(default_factory=list)
    max_per_match: int = 0
    ordinary_can_bowl: bool = False
    ordinary_can_keep: bool = False
    ordinary_can_captain: bool = False


class FieldingSetupResponse(BaseModel):
    """The whole field setup for one innings of one match."""

    match_id: int
    innings_number: int
    fielding_team_id: int
    fielding_team_name: str
    batting_team_id: int
    # Null until a keeper is named; there is never more than one.
    wicketkeeper_player_id: int | None = None
    assignments: list[FieldingPositionOut] = Field(default_factory=list)
    eligible_players: list[FieldingEligiblePlayer] = Field(default_factory=list)
    # Same-team squad members who are NOT in the XI - the reserves who may
    # be brought onto the field. Empty list (not an error) when the squad has
    # no reserves at all.
    eligible_substitutes: list[FieldingEligiblePlayer] = Field(default_factory=list)
    position_catalog: list[FieldingPositionCatalogEntry] = Field(default_factory=list)
    # Every substitution recorded in this innings, newest last.
    substitutions: list[FieldingSubstitutionOut] = Field(default_factory=list)
    # The competition's substitution rules, so the panel can offer exactly
    # the reasons allowed and say which of them are approved replacements.
    substitution_rules: SubstitutionRulesOut = Field(default_factory=SubstitutionRulesOut)
    # A side never fields more than this many active players.
    max_active_fielders: int = MAX_ACTIVE_FIELDERS
    # Who is bowling right now (the last delivery's bowler), so the XI list
    # can mark the current bowler. Null before the first ball.
    current_bowler_player_id: int | None = None
    # False when the match has no innings of this number yet (the toss decided
    # the sides, but scoring has not started). The client may still show the
    # panel; saving is refused until the innings exists.
    innings_started: bool = True


class FieldingAssignmentIn(BaseModel):
    """One fielder/position pair in a save request."""

    player_id: int = Field(..., ge=1)
    position_key: str
    # Where the diagram should draw the marker, 0-100 normalized. When the
    # client has dragged a marker it sends the dropped coordinates; when it
    # has only picked a position from the list it omits them and the server
    # falls back to the catalog anchor. Values are clamped into range rather
    # than rejected, because a marker dragged hard against the boundary may
    # land a hair outside.
    anchor_x: float | None = Field(default=None, ge=-25.0, le=125.0)
    anchor_y: float | None = Field(default=None, ge=-25.0, le=125.0)
    # The scorer's own name for a custom position; only meaningful when
    # position_key == "custom_position".
    custom_label: str | None = Field(default=None, max_length=60)

    @field_validator("position_key")
    @classmethod
    def _known_position(cls, v: str) -> str:
        if v not in POSITION_KEYS:
            raise ValueError(
                f"unknown fielding position '{v}'; valid positions are "
                + ", ".join(sorted(POSITION_KEYS))
            )
        return v

    @field_validator("anchor_x", "anchor_y")
    @classmethod
    def _clamp_coordinate(cls, v: float | None) -> float | None:
        if v is None:
            return None
        return max(0.0, min(100.0, v))

    @field_validator("custom_label")
    @classmethod
    def _strip_label(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class FieldingSetupUpsert(BaseModel):
    """Replace the field setup for one innings.

    Full-replace rather than patch-on-patch: the caller sends the field it
    wants, and the server makes the stored setup match it inside one
    transaction. That is what makes "move a fielder" and "swap two fielders"
    safe - there is no intermediate state where both hold one position.
    """

    innings_number: int = Field(..., ge=1, le=10)
    wicketkeeper_player_id: int | None = None
    assignments: list[FieldingAssignmentIn] = Field(default_factory=list)

    @field_validator("assignments")
    @classmethod
    def _no_duplicate_players(cls, v: list[FieldingAssignmentIn]) -> list[FieldingAssignmentIn]:
        # Players must be unique - one person cannot stand in two places.
        # Positions deliberately are NOT: two fielders may share a named
        # area (two slips, two men at point) when their coordinates differ.
        players = [a.player_id for a in v]
        if len(set(players)) != len(players):
            raise ValueError("a player can hold only one fielding position at a time")
        if len(v) > MAX_ACTIVE_FIELDERS:
            raise ValueError(
                f"a side fields at most {MAX_ACTIVE_FIELDERS} players; {len(v)} were sent"
            )
        return v


class FieldingSubstitutionIn(BaseModel):
    """Bring a reserve onto the field in place of an active player."""

    innings_number: int = Field(..., ge=1, le=10)
    # The active player leaving the field.
    outgoing_player_id: int = Field(..., ge=1)
    # The reserve coming on. Must be a same-team squad member who is not
    # already active; validated in the crud layer where team and XI are known.
    incoming_player_id: int = Field(..., ge=1)
    # One of the competition's permitted reasons (see app.fielding.rules).
    reason: str = Field(..., min_length=1, max_length=60)
    # Optional position to give the incoming substitute straight away. When
    # omitted the substitute comes onto the field unplaced, and the scorer
    # places them like any other fielder.
    position_key: str | None = Field(default=None, max_length=40)
    anchor_x: float | None = Field(default=None, ge=-25.0, le=125.0)
    anchor_y: float | None = Field(default=None, ge=-25.0, le=125.0)
    custom_label: str | None = Field(default=None, max_length=60)

    @field_validator("position_key")
    @classmethod
    def _known_position(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in POSITION_KEYS:
            raise ValueError(
                f"unknown fielding position '{v}'; valid positions are "
                + ", ".join(sorted(POSITION_KEYS))
            )
        return v

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("a substitution needs a reason")
        return v

    @model_validator(mode="after")
    def _no_same_player(self) -> FieldingSubstitutionIn:
        if self.outgoing_player_id == self.incoming_player_id:
            raise ValueError("a player cannot replace themselves")
        return self
