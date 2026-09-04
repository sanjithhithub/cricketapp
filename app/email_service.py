import os
import logging
import random
import smtplib
import string
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

logger = logging.getLogger("app.email")

load_dotenv()

EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "test")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() == "true"
SMTP_SSL = os.getenv("SMTP_SSL", "false").lower() == "true"
TEST_EMAIL_OTP_CODE = os.getenv("TEST_EMAIL_OTP_CODE", "654321")


def generate_otp(length: int = 6) -> str:
    if EMAIL_PROVIDER == "test":
        return TEST_EMAIL_OTP_CODE
    return "".join(random.choices(string.digits, k=length))


async def send_email_otp(to_email: str, otp_code: str) -> tuple[bool, str | None]:
    """Send an OTP email. Returns (success, error_message)."""
    if EMAIL_PROVIDER == "test":
        logger.info(
            "TEST MODE: email OTP for %s is %s", to_email, TEST_EMAIL_OTP_CODE
        )
        return True, None

    if EMAIL_PROVIDER in ("smtp", "email"):
        return await _send_smtp_otp(to_email, otp_code)

    logger.error("Unknown EMAIL_PROVIDER: %s", EMAIL_PROVIDER)
    return False, "EMAIL_PROVIDER is not configured"


async def _send_smtp_otp(to_email: str, otp_code: str) -> tuple[bool, str | None]:
    if not SMTP_HOST or not SMTP_USER:
        logger.error("SMTP is not configured (SMTP_HOST/SMTP_USER missing)")
        return False, "SMTP is not configured"

    subject = "Your CricketApp OTP"
    body = (
        f"Hello,\n\n"
        f"Your verification code is: {otp_code}\n\n"
        f"This code is valid for 5 minutes. Do not share it with anyone.\n\n"
        f"Regards,\nCricketApp Team"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        if SMTP_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        with server:
            if SMTP_TLS and not SMTP_SSL:
                server.starttls()
            if SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM or SMTP_USER, to_email, msg.as_string())
        logger.info("Email OTP sent to %s", to_email)
        return True, None
    except Exception:
        logger.exception("Email OTP send failed for %s", to_email)
        return False, "Failed to send email"
