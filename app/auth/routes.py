from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crud import (
    authenticate_user,
    login_or_create_google_user,
    register_user,
    reset_password,
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
    OTPResponse,
    OTPVerifyResponse,
    PasswordResetResponse,
    RegisterSendOTPRequest,
    RegisterVerifyOTPRequest,
    UserLogin,
    UserOut,
    UserRegister,
)
from app.auth.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token, get_current_user
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register/send-otp", response_model=OTPResponse)
async def register_send_otp(
    data: RegisterSendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    await send_register_otp(db, data.email)
    return OTPResponse(message="OTP sent to your email", sent=True)


@router.post("/register/verify-otp", response_model=OTPVerifyResponse)
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


@router.post("/forgot-password/verify-otp", response_model=ForgotPasswordVerifyResponse)
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
    token = create_access_token(user.id)
    return LoginResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    data: UserLogin,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(db, data.email, data.password)
    token = create_access_token(user.id)
    return LoginResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
    )


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
    token = create_access_token(user.id)
    return LoginResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user
