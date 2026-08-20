import os
import logging

import firebase_admin
from firebase_admin import credentials
import httpx

logger = logging.getLogger("app.firebase")

_firebase_app = None

SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "firebase-service-account.json"),
)


def _get_app():
    global _firebase_app
    if _firebase_app is None:
        if not os.path.exists(SERVICE_ACCOUNT_PATH):
            raise FileNotFoundError(
                f"Firebase service account file not found: {SERVICE_ACCOUNT_PATH}"
            )
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialized")
    return _firebase_app


def _get_access_token():
    app = _get_app()
    cred = app.credential.get_credential()
    token = cred.token
    if token is None or cred.expired:
        import google.auth.transport.requests

        cred.refresh(google.auth.transport.requests.Request())
        token = cred.token
    return token


def _format_phone(country_code: str, mobile_number: int) -> str:
    digits = "".join(c for c in country_code if c.isdigit())
    return f"+{digits}{mobile_number}"


async def send_firebase_otp(country_code: str, mobile_number: int) -> tuple[bool, str | None]:
    phone = _format_phone(country_code, mobile_number)
    access_token = _get_access_token()

    url = "https://identitytoolkit.googleapis.com/v1/accounts:sendVerificationCode"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "phoneNumber": phone,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            session_info = data.get("sessionInfo")
            logger.info("Firebase sendVerificationCode success for %s", phone)
            return True, session_info
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Firebase sendVerificationCode failed for %s: %s %s",
            phone,
            exc.response.status_code,
            exc.response.text,
        )
        return False, None
    except Exception:
        logger.exception("send_firebase_otp raised for %s", phone)
        return False, None


async def verify_firebase_otp(session_info: str, code: str) -> bool:
    access_token = _get_access_token()

    url = "https://identitytoolkit.googleapis.com/v1/accounts:verifyPhoneNumber"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "sessionInfo": session_info,
        "code": code,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Firebase verifyPhoneNumber success: idToken present=%s", "idToken" in data)
            return True
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Firebase verifyPhoneNumber failed: %s %s",
            exc.response.status_code,
            exc.response.text,
        )
        return False
    except Exception:
        logger.exception("verify_firebase_otp raised")
        return False
