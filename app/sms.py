import os
import logging

import httpx

logger = logging.getLogger("app.sms")

TWO_FACTOR_API_KEY = os.getenv("TWO_FACTOR_API_KEY", "")
TWO_FACTOR_OTP_TEMPLATE = os.getenv("TWO_FACTOR_OTP_TEMPLATE", "")
BASE_URL = "https://2factor.in/API/V1"


def _format_phone(country_code: str, mobile_number: int) -> str:
    digits = "".join(c for c in country_code if c.isdigit())
    return f"{digits}{mobile_number}"


async def send_otp(country_code: str, mobile_number: int, otp_code: str) -> bool:
    if not TWO_FACTOR_API_KEY:
        logger.error("send_otp skipped: TWO_FACTOR_API_KEY is not configured")
        return False

    phone = _format_phone(country_code, mobile_number)
    url = f"{BASE_URL}/{TWO_FACTOR_API_KEY}/SMS/{phone}/{otp_code}"
    if TWO_FACTOR_OTP_TEMPLATE:
        url = f"{url}/{TWO_FACTOR_OTP_TEMPLATE}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("Status")
            details = data.get("Details")
            logger.info("2factor SMS response for %s: Status=%s Details=%s", phone, status, details)
            if status == "Success":
                return True
            logger.error("2factor SMS failed for %s: %s", phone, data)
            return False
    except Exception as exc:
        logger.exception("send_otp raised for %s", phone)
        return False
