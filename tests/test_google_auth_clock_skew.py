"""Regression tests for the clock-skew tolerance on Google ID tokens.

The bug: google-auth stamps the ``iat`` claim of a Google ID token with
Google's own clock and rejects the token when that timestamp is in the future
relative to the machine doing the verification. A machine whose clock has
drifted - or was never synced with NTP, which is the state this repo's dev box
was in - therefore rejected every perfectly valid token, and every Google
sign-in failed with "Invalid Google ID token". That message points at the
token instead of at the clock, which is where the debugging time went.

The fix: pass a clock-skew tolerance to ``verify_oauth2_token``. Nothing else
moves. The signature is still verified against Google's certificates, the
audience still has to match, and the issuer is still checked.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.oauth2 import id_token as google_id_token

from app.auth import crud

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
KID = "test-key-id"
ISSUER = "https://accounts.google.com"


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


@pytest.fixture
def signing_key(monkeypatch):
    """A self-signed cert standing in for Google's, wired into google-auth.

    google-auth fetches Google's x509 certificates over the network and looks
    the right one up by the token's ``kid``. The fixture swaps the fetch for a
    locally generated key so the rest of the verification path - signature,
    timestamps, audience - runs for real.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    # The legacy x509 mapping, the same shape Google's certs endpoint returns.
    monkeypatch.setattr(google_id_token, "_fetch_certs", lambda request, certs_url: {KID: cert_pem})
    monkeypatch.setattr(crud, "GOOGLE_CLIENT_ID", CLIENT_ID)
    return key


def _id_token(key, issued_at, audience=CLIENT_ID):
    return pyjwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": "1234567890",
            "email": "user@test.com",
            "email_verified": True,
            "name": "Test User",
            "iat": issued_at,
            "exp": _now() + 3600,
        },
        key,
        algorithm="RS256",
        headers={"kid": KID},
    )


def _verify(token):
    return asyncio.run(crud.verify_google_id_token(token))


def test_token_issued_a_few_seconds_in_the_future_is_accepted(signing_key):
    """The reported bug: a token 5s ahead of a drifting clock must verify."""
    token = _id_token(signing_key, _now() + 5)

    profile = _verify(token)

    assert profile["email"] == "user@test.com"
    assert profile["full_name"] == "Test User"


def test_without_a_tolerance_the_same_token_is_rejected(signing_key, monkeypatch):
    """Guard the guard: with no tolerance this token really does fail.

    If google-auth ever stops comparing `iat` against the local clock this
    test would pass for the wrong reason, and the tolerance would look like it
    was doing nothing. Pin it to the old behaviour.
    """
    monkeypatch.setattr(crud, "GOOGLE_CLOCK_SKEW_SECONDS", 0)
    token = _id_token(signing_key, _now() + 5)

    with pytest.raises(Exception) as exc:
        _verify(token)

    # A clock problem, not a bad token: the client is told to retry and the
    # message names the clock.
    assert getattr(exc.value, "status_code", None) == 503
    assert "clock" in str(exc.value.detail).lower()


def test_a_token_far_in_the_future_is_still_rejected(signing_key):
    """The tolerance is a skew allowance, not a blanket pass.

    5 minutes ahead is not clock drift, it is a forged or replayed token.
    """
    token = _id_token(signing_key, _now() + 300)

    with pytest.raises(Exception) as exc:
        _verify(token)

    assert getattr(exc.value, "status_code", None) == 503


def test_a_token_with_the_wrong_audience_is_rejected(signing_key):
    """The tolerance must not soften the audience check."""
    token = _id_token(signing_key, _now(), audience="someone-else.apps.googleusercontent.com")

    with pytest.raises(Exception) as exc:
        _verify(token)

    assert getattr(exc.value, "status_code", None) == 401
    assert exc.value.detail == "Invalid Google ID token"


def test_a_token_signed_by_the_wrong_key_is_rejected(signing_key, monkeypatch):
    """The tolerance must not soften the signature check."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _id_token(other_key, _now())

    with pytest.raises(Exception) as exc:
        _verify(token)

    assert getattr(exc.value, "status_code", None) == 401
    assert exc.value.detail == "Invalid Google ID token"
