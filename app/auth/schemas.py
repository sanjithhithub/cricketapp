from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(None, max_length=100)
    country_code: str | None = Field(None, max_length=5)
    mobile_number: int | None = None


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
    full_name: str | None = None
    profile_picture: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshTokenRequest(BaseModel):
    """Exchange a refresh token for a new access token.

    The refresh token travels in the body rather than the Authorization header,
    because the header is where the *expired* access token goes. Reusing it would
    mean a client has to strip the bad credential before it can ask for a good
    one.
    """

    refresh_token: str = Field(
        ...,
        min_length=1,
        description=(
            "The `refresh_token` from a previous login. Single use: each call "
            "returns a new one, and the token sent here cannot be used again."
        ),
    )


class RefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int = Field(
        ...,
        description="Seconds until the new refresh token expires.",
    )


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    profile_picture: str | None = None
    country_code: str | None = None
    mobile_number: int | None = None
    auth_provider: str
    role: str = "admin"
    is_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(TokenOut):
    """The body of every operation that signs a user in.

    Carries both tokens. ``access_token`` goes in the Authorization header and is
    good for ``expires_in`` seconds; ``refresh_token`` is exchanged at
    ``POST /v1/auth/refresh`` once the access token expires, without asking for the
    password again. Without it a client can only react to a 401 by sending the
    user back to the sign-in form, even though it is holding a credential that
    would have worked.
    """

    user: UserOut
    refresh_token: str = Field(
        ...,
        description="Single-use. Store it wherever the access token is stored.",
    )
    refresh_expires_in: int = Field(
        ...,
        description="Seconds until `refresh_token` expires.",
    )


class LogoutResponse(BaseModel):
    message: str
    revoked: bool = Field(
        ...,
        description=(
            "False when the token was already gone - expired, logged out, or "
            "never issued. Logout is idempotent, so this is informational."
        ),
    )
