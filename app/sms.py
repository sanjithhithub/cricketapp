import os
import logging

import httpx
from twilio.rest import Client as TwilioClient

logger = logging.getLogger("app.sms")

SMS_PROVIDER = os.getenv("SMS_PROVIDER", "test")
TEST_OTP_CODE = os.getenv("TEST_OTP_CODE", "123456")
MSG91_AUTH_KEY = os.getenv("MSG91_AUTH_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID", "")


def _format_phone(country_code: str, mobile_number: int) -> str:
    digits = "".join(c for c in country_code if c.isdigit())
    return f"+{digits}{mobile_number}"


async def send_otp(country_code: str, mobile_number: int) -> tuple[bool, str | None]:
    if SMS_PROVIDER == "test":
        phone = _format_phone(country_code, mobile_number)
        logger.info("TEST MODE: OTP for +%s is %s", phone, TEST_OTP_CODE)
        return True, f"test-session-{phone}"

    if SMS_PROVIDER == "msg91":
        return await _send_msg91_otp(country_code, mobile_number)

    if SMS_PROVIDER == "twilio":
        return await _send_twilio_otp(country_code, mobile_number)

    from app.firebase import send_firebase_otp
    return await send_firebase_otp(country_code, mobile_number)


async def verify_otp(session_info: str, code: str) -> bool:
    if SMS_PROVIDER == "test":
        valid = code == TEST_OTP_CODE
        logger.info("TEST MODE: verify code=%s valid=%s", code, valid)
        return valid

    if SMS_PROVIDER == "msg91":
        return await _verify_msg91_otp(session_info, code)

    if SMS_PROVIDER == "twilio":
        return await _verify_twilio_otp(session_info, code)

    from app.firebase import verify_firebase_otp
    return await verify_firebase_otp(session_info, code)


async def _send_msg91_otp(country_code: str, mobile_number: int) -> tuple[bool, str | None]:
    phone = _format_phone(country_code, mobile_number)

    url = "https://api.msg91.com/api/sendotp.php"
    payload = {
        "authkey": MSG91_AUTH_KEY,
        "mobile": phone,
        "otp_length": "6",
        "otp_expiry": "5",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload, timeout=15)
            data = resp.json()
            if data.get("type") == "success":
                logger.info("MSG91 OTP sent to %s", phone)
                return True, phone
            logger.error("MSG91 send OTP failed: %s", data)
            return False, None
    except Exception:
        logger.exception("MSG91 send_otp raised for %s", phone)
        return False, None


async def _verify_msg91_otp(phone: str, code: str) -> bool:
    url = "https://api.msg91.com/api/verifyRequestOTP.php"
    payload = {
        "authkey": MSG91_AUTH_KEY,
        "mobile": phone,
        "otp": code,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload, timeout=15)
            data = resp.json()
            if data.get("type") == "success":
                logger.info("MSG91 OTP verified for %s", phone)
                return True
            logger.error("MSG91 verify OTP failed: %s", data)
            return False
    except Exception:
        logger.exception("MSG91 verify_otp raised for %s", phone)
        return False


async def _send_twilio_otp(country_code: str, mobile_number: int) -> tuple[bool, str | None]:
    phone = _format_phone(country_code, mobile_number)

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        verification = client.verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verifications.create(
            to=phone, channel="sms"
        )
        session_info = f"{verification.sid}:{phone}"
        logger.info("Twilio OTP sent to %s", phone)
        return True, session_info
    except Exception:
        logger.exception("Twilio send_otp raised for %s", phone)
        return False, None


async def _verify_twilio_otp(session_info: str, code: str) -> bool:
    parts = session_info.split(":")
    if len(parts) != 2:
        logger.error("Invalid Twilio session_info format: %s", session_info)
        return False

    verification_sid, phone = parts

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        check = client.verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verification_checks.create(
            to=phone, code=code
        )
        if check.status == "approved":
            logger.info("Twilio OTP verified for %s", phone)
            return True
        logger.error("Twilio verify OTP failed: status=%s", check.status)
        return False
    except Exception:
        logger.exception("Twilio verify_otp raised for %s", phone)
        return False
