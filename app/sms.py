import logging
import os
import random
import string

import httpx
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

load_dotenv()

logger = logging.getLogger("app.sms")

SMS_PROVIDER = os.getenv("SMS_PROVIDER", "test")
TEST_OTP_CODE = os.getenv("TEST_OTP_CODE", "123456")
TWO_FACTOR_API_KEY = os.getenv("TWO_FACTOR_API_KEY", "")
MSG91_AUTH_KEY = os.getenv("MSG91_AUTH_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID", "")


def _format_phone(country_code: str, mobile_number: str | int) -> str:
    """Build the dialable number the gateway expects.

    The number arrives as text (see app.players.identity) and may still carry
    formatting the user typed, so the digits are pulled out here rather than
    trusting the caller to have normalised it. A number that is already in
    international form is left alone instead of being prefixed twice.
    """
    cc_digits = "".join(c for c in str(country_code or "") if c.isdigit())
    raw = str(mobile_number or "")
    national = "".join(c for c in raw if c.isdigit())
    if not national:
        return ""
    # "+919812345678" typed into a field that already has a country code would
    # otherwise become "+91+919812345678".
    if cc_digits and national.startswith(cc_digits) and len(national) > len(cc_digits):
        return f"+{national}"
    return f"+{cc_digits}{national.lstrip('0')}"


async def send_otp(country_code: str, mobile_number: str | int) -> tuple[bool, str | None]:
    if SMS_PROVIDER == "test":
        phone = _format_phone(country_code, mobile_number)
        logger.info("TEST MODE: OTP for +%s is %s", phone, TEST_OTP_CODE)
        return True, f"test-session-{phone}"

    if SMS_PROVIDER == "msg91":
        return await _send_msg91_otp(country_code, mobile_number)

    if SMS_PROVIDER == "twilio":
        return await _send_twilio_otp(country_code, mobile_number)

    if SMS_PROVIDER == "2factor":
        return await _send_2factor_otp(country_code, mobile_number)

    logger.error("Unknown SMS_PROVIDER: %s", SMS_PROVIDER)
    return False, None


async def verify_otp(session_info: str, code: str) -> bool:
    if SMS_PROVIDER == "test":
        valid = code == TEST_OTP_CODE
        logger.info("TEST MODE: verify code=%s valid=%s", code, valid)
        return valid

    if SMS_PROVIDER == "msg91":
        return await _verify_msg91_otp(session_info, code)

    if SMS_PROVIDER == "twilio":
        return await _verify_twilio_otp(session_info, code)

    if SMS_PROVIDER == "2factor":
        return await _verify_2factor_otp(session_info, code)

    logger.error("Unknown SMS_PROVIDER: %s", SMS_PROVIDER)
    return False


async def _send_msg91_otp(country_code: str, mobile_number: str | int) -> tuple[bool, str | None]:
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


async def _send_twilio_otp(country_code: str, mobile_number: str | int) -> tuple[bool, str | None]:
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


async def _send_2factor_otp(country_code: str, mobile_number: str | int) -> tuple[bool, str | None]:
    phone = _format_phone(country_code, mobile_number)
    phone_no_plus = phone.lstrip("+")

    otp_code = "".join(random.choices(string.digits, k=6))
    url = f"https://2factor.in/API/V1/{TWO_FACTOR_API_KEY}/SMS/{phone_no_plus}/{otp_code}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, timeout=15)
            data = resp.json()
            if data.get("Status") == "Success":
                logger.info("2Factor OTP sent to %s, otp=%s", phone, otp_code)
                return True, otp_code
            logger.error("2Factor send OTP failed: %s", data)
            return False, None
    except Exception:
        logger.exception("2Factor send_otp raised for %s", phone)
        return False, None


async def _verify_2factor_otp(stored_otp: str, code: str) -> bool:
    valid = stored_otp == code
    logger.info("2Factor verify: stored=%s code=%s valid=%s", stored_otp, code, valid)
    return valid
