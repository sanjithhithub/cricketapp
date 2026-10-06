from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_docs import BAD_REQUEST
from app.auth.crud import (
    authenticate_user,
    issue_refresh_token,
    login_or_create_google_user,
    register_user,
    reset_password,
    revoke_refresh_token,
    rotate_refresh_token,
    send_forgot_password_otp,
    send_register_otp,
    verify_forgot_password_otp,
    verify_google_id_token,
    verify_register_otp,
)
from app.auth.models import User
from app.auth.schemas import (
    ForgotPasswordResetRequest,
    ForgotPasswordSendOTPRequest,
    ForgotPasswordVerifyOTPRequest,
    ForgotPasswordVerifyResponse,
    GoogleAuthRequest,
    LoginResponse,
    LogoutResponse,
    OTPResponse,
    OTPVerifyResponse,
    PasswordResetResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RegisterSendOTPRequest,
    RegisterVerifyOTPRequest,
    UserLogin,
    UserOut,
    UserRegister,
)
from app.auth.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    get_current_user,
    refresh_token_expires_in,
)
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


async def _login_response(db: AsyncSession, user: User) -> LoginResponse:
    """Issue the pair every sign-in path returns.

    One place, because the access token and the refresh token are issued as a unit
    and a path that returned only one of them would leave a client with an access
    token it could never refresh.
    """
    refresh_token, _ = await issue_refresh_token(db, user.id)
    return LoginResponse(
        access_token=create_access_token(user.id),
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
        refresh_token=refresh_token,
        refresh_expires_in=refresh_token_expires_in(),
    )


@router.post("/register/send-otp", response_model=OTPResponse)
async def register_send_otp(
    data: RegisterSendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    await send_register_otp(db, data.email)
    return OTPResponse(message="OTP sent to your email", sent=True)


@router.post(
    "/register/verify-otp", response_model=OTPVerifyResponse, responses={"400": BAD_REQUEST}
)
async def register_verify_otp(
    data: RegisterVerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    verified = await verify_register_otp(db, data.email, data.otp_code)
    if not verified:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    return OTPVerifyResponse(message="Email verified successfully", verified=True)


@router.post("/forgot-password/send-otp", response_model=OTPResponse)
async def forgot_password_send_otp(
    data: ForgotPasswordSendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    await send_forgot_password_otp(db, data.email)
    return OTPResponse(message="OTP sent to your email", sent=True)


@router.post(
    "/forgot-password/verify-otp",
    response_model=ForgotPasswordVerifyResponse,
    responses={"400": BAD_REQUEST},
)
async def forgot_password_verify_otp(
    data: ForgotPasswordVerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    reset_token = await verify_forgot_password_otp(db, data.email, data.otp_code)
    if not reset_token:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    return ForgotPasswordVerifyResponse(
        message="OTP verified successfully", reset_token=reset_token
    )


@router.post("/forgot-password/reset", response_model=PasswordResetResponse)
async def forgot_password_reset(
    data: ForgotPasswordResetRequest,
    db: AsyncSession = Depends(get_db),
):
    await reset_password(db, data.email, data.reset_token, data.new_password)
    return PasswordResetResponse(message="Password reset successfully")


@router.post("/register", response_model=LoginResponse, status_code=201)
async def register(
    data: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    user = await register_user(db, data)
    return await _login_response(db, user)


@router.post("/login", response_model=LoginResponse)
async def login(
    data: UserLogin,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(db, data.email, data.password)
    return await _login_response(db, user)


@router.post("/google", response_model=LoginResponse)
async def google_auth(
    data: GoogleAuthRequest,
    db: AsyncSession = Depends(get_db),
):
    profile = await verify_google_id_token(data.id_token)
    if data.full_name and not profile.get("full_name"):
        profile["full_name"] = data.full_name
    if data.profile_picture and not profile.get("profile_picture"):
        profile["profile_picture"] = data.profile_picture

    user = await login_or_create_google_user(db, profile)
    return await _login_response(db, user)


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    summary="Exchange a refresh token for a new access token",
    description=(
        "The normal way to recover from a 401. Send the `refresh_token` from a "
        "previous login and get a fresh pair back - no password, no redirect.\n\n"
        "**The refresh token is single use.** Every call rotates it: the token sent "
        "here is revoked and a new one is returned. Store the new one over the old "
        "one, including when two requests race and only the later one wins - a "
        "client that keeps the old token will have its next refresh rejected.\n\n"
        "A 401 here means the token is unknown, expired, revoked, or belongs to a "
        "deactivated account. All four are reported the same way, so the client "
        "cannot probe which. On a 401 the only recovery is to send the user back "
        "to the sign-in form."
    ),
    responses={"401": {"description": "The refresh token is not usable."}},
)
async def refresh_tokens(
    data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rotate a refresh token. Deliberately unauthenticated.

    It cannot require a bearer token: the reason a client is here is that its
    bearer token expired.
    """
    user, new_refresh_token = await rotate_refresh_token(db, data.refresh_token)
    return RefreshTokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=new_refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        refresh_expires_in=refresh_token_expires_in(),
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Revoke a refresh token",
    description=(
        "Ends the session the refresh token represents. The access token is **not** "
        "revoked - it is a signed JWT and stays valid until it expires; what this "
        "prevents is minting another access token after the fact. Other devices' "
        "sessions are untouched.\n\n"
        "Idempotent, and answers 200 with `revoked: false` for a token that was "
        "already gone. A client calling this without a token should call it anyway."
    ),
)
async def logout(
    data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke one refresh token. Best effort, and says so in the response."""
    revoked = await revoke_refresh_token(db, data.refresh_token)
    return LogoutResponse(message="Signed out", revoked=revoked)


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user
