from pydantic import BaseModel


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
    mobile_number: int
    otp_code: str


class OTPVerifyResponse(BaseModel):
    message: str
    verified: bool
