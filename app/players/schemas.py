from datetime import date

from pydantic import BaseModel, field_validator


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
    mobile_number: int
    email: str

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

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute", "bench"):
            raise ValueError("role must be playing_11, substitute, or bench")
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
    mobile_number: int | None = None
    email: str | None = None


class PlayerResponse(PlayerBase):
    id: int
    is_phone_verified: bool = False

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
    first_name: str
    last_name: str
    date_of_birth: date
    gender: str
    batting_hand: str
    batting_position: str
    bowling_type: str
    country_code: str
    mobile_number: int
    country_name: str | None = None
    state_name: str | None = None
    city_name: str | None = None
    profile_image: str | None = None

    class Config:
        from_attributes = True


class TeamPlayerByPhone(BaseModel):
    country_code: str
    mobile_number: int
    role: str = "playing_11"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("playing_11", "substitute"):
            raise ValueError("role must be playing_11 or substitute")
        return v
