from typing import Literal

from pydantic import BaseModel, field_validator

from app.players.identity import normalize_country_code


class LocationOut(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


class StateOut(BaseModel):
    id: int
    name: str
    cities: list[LocationOut] = []

    class Config:
        from_attributes = True


class CountryOut(BaseModel):
    id: int
    name: str
    states: list[StateOut] = []

    class Config:
        from_attributes = True


class CountryCodeOut(BaseModel):
    code: str
    name: str


class OTPVerifyRequest(BaseModel):
    country_code: str
    # Text: a number may keep its leading zeros, and the value is compared
    # against players.mobile_number, which is text for the same reason.
    mobile_number: str
    otp_code: str

    @field_validator("mobile_number", mode="before")
    @classmethod
    def validate_mobile_number(cls, v):
        return str(v) if v is not None else v

    @field_validator("country_code", mode="before")
    @classmethod
    def validate_country_code(cls, v):
        return normalize_country_code(v)


class OTPVerifyResponse(BaseModel):
    message: str
    verified: bool


class HealthResponse(BaseModel):
    """The liveness probe's body.

    A literal rather than ``str`` so a monitor that polls this can be generated
    against the spec and told, at compile time, that anything other than ``"ok"``
    is not a healthy response.
    """

    status: Literal["ok"] = "ok"
