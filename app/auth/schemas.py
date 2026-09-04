from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: Optional[str] = Field(None, max_length=100)
    country_code: Optional[str] = Field(None, max_length=5)
    mobile_number: Optional[int] = None


class RegisterSendOTPRequest(BaseModel):
    email: EmailStr


class RegisterVerifyOTPRequest(BaseModel):
    email: EmailStr
    otp_code: str


class ForgotPasswordSendOTPRequest(BaseModel):
    email: EmailStr


class ForgotPasswordVerifyOTPRequest(BaseModel):
    email: EmailStr
    otp_code: str


class ForgotPasswordResetRequest(BaseModel):
    email: EmailStr
    reset_token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class OTPResponse(BaseModel):
    message: str
    sent: bool


class OTPVerifyResponse(BaseModel):
    message: str
    verified: bool


class ForgotPasswordVerifyResponse(BaseModel):
    message: str
    reset_token: str


class PasswordResetResponse(BaseModel):
    message: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    id_token: str
    full_name: Optional[str] = None
    profile_picture: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    profile_picture: Optional[str] = None
    country_code: Optional[str] = None
    mobile_number: Optional[int] = None
    auth_provider: str
    is_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(TokenOut):
    user: UserOut
