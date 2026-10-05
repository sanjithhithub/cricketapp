"""Regression tests for the Google sign-in and CORS allow-list.

Both bugs here were reported as "OAuth is blocked" in the browser:

1. CORS_ORIGINS pinned the dev regex to port 5173, so a Vite dev server that
   auto-incremented to 5174+ was rejected at preflight.
2. A Google sign-in for an email that already had a password account returned
   409 instead of signing the user in.
"""

import asyncio
import re

import pytest

import app.main as main_module
from app.auth import crud

ORIGIN_CASES = [
    # (configured CORS_ORIGINS, origin, expected)
    ("https://cricketapp.in", "https://cricketapp.in", True),
    ("https://cricketapp.in", "https://evil.com", False),
    # Trailing slash and stray space must still match the browser's Origin.
    ("https://cricketapp.in/ , https://www.cricketapp.in", "https://cricketapp.in", True),
    ("https://www.cricketapp.in/", "https://www.cricketapp.in", True),
    # A scheme-less entry is assumed https rather than silently never matching.
    ("app.cricketapp.in", "https://app.cricketapp.in", True),
    ("https://cricketapp.in", "https://app.cricketapp.in", False),
    # Wildcard is preserved verbatim for the no-credentials case.
    ("*", "*", True),
    # AWS Amplify gives every branch its own hostname, so the allow-list has to
    # cover the branch pattern rather than one literal host.
    (
        "https://main.d3hykm7kymvqjc.amplifyapp.com",
        "https://pr42.d3hykm7kymvqjc.amplifyapp.com",
        False,
    ),
    # Non-default dev ports must be accepted.
    (None, "http://localhost:5174", True),
    (None, "http://127.0.0.1:5175", True),
    (None, "http://192.168.1.99:5173", True),
    (None, "http://10.0.0.5:3000", True),
    # A public host must never be matched by the dev fallback.
    (None, "https://evil.com", False),
    (None, "http://evil.com:5173", False),
    # HSTS protects the prod hosts, so http must not be silently allowed.
    (None, "https://cricketapp.in.evil.com", False),
]


@pytest.mark.parametrize("configured, origin, expected", ORIGIN_CASES)
def test_origin_allow_list(monkeypatch, configured, origin, expected):
    if configured is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", configured)
    monkeypatch.delenv("CORS_ORIGINS_REGEX", raising=False)

    origins = main_module._cors_origins()
    regex = main_module._cors_origin_regex()
    allowed = origin in origins or bool(regex is not None and re.match(regex, origin))
    assert allowed is expected


AMPLIFY_REGEX = r"^https://[a-z0-9-]+\.d3hykm7kymvqjc\.amplifyapp\.com$"

# The frontend is hosted on AWS Amplify, which assigns a distinct hostname per
# branch. The allow-list must therefore cover the branch pattern, not one host.
AMPLIFY_CASES = [
    # Branches of this app are allowed.
    ("https://main.d3hykm7kymvqjc.amplifyapp.com", True),
    ("https://pr42.d3hykm7kymvqjc.amplifyapp.com", True),
    ("https://feature-login.d3hykm7kymvqjc.amplifyapp.com", True),
    # A different Amplify app must not be admitted: any matched origin can read
    # credentialed responses from our users.
    ("https://main.someoneelse.amplifyapp.com", False),
    ("https://evil.amplifyapp.com", False),
    # The bare app-id host is not a branch and stays out.
    ("https://d3hykm7kymvqjc.amplifyapp.com", False),
    # Amplify serves over https; http must not match.
    ("http://main.d3hykm7kymvqjc.amplifyapp.com", False),
    # Suffix trick on our own apex.
    ("https://cricketapp.in.evil.com", False),
]


@pytest.mark.parametrize("origin, expected", AMPLIFY_CASES)
def test_amplify_branch_origins(monkeypatch, origin, expected):
    monkeypatch.setenv("CORS_ORIGINS", "https://cricketapp.in")
    monkeypatch.setenv("CORS_ORIGINS_REGEX", AMPLIFY_REGEX)

    origins = main_module._cors_origins()
    regex = main_module._cors_origin_regex()
    allowed = origin in origins or bool(regex is not None and re.match(regex, origin))
    assert allowed is expected


def test_explicit_regex_overrides_dev_default(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://cricketapp.in")
    monkeypatch.setenv("CORS_ORIGINS_REGEX", r"^https://[a-z]+\.cricketapp\.in$")

    regex = main_module._cors_origin_regex()
    assert re.match(regex, "https://app.cricketapp.in")
    # The configured regex replaces the default rather than adding to it.
    assert not re.match(regex, "http://localhost:5174")


class _FakeDB:
    """Minimal AsyncSession stand-in for login_or_create_google_user."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return obj


class _FakeResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeSelect:
    def where(self, *args, **kwargs):
        return self


class _FakeDb2(_FakeDB):
    def __init__(self, user):
        super().__init__()
        self._user = user

    async def execute(self, stmt):
        return _FakeResult(self._user)


class _User:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", 1)
        self.email = kwargs.pop("email")
        self.auth_provider = kwargs.pop("auth_provider", "email")
        self.full_name = kwargs.pop("full_name", None)
        self.profile_picture = kwargs.pop("profile_picture", None)
        self.is_verified = kwargs.pop("is_verified", False)
        self.is_active = kwargs.pop("is_active", True)
        self.hashed_password = kwargs.pop("hashed_password", None)
        for key, value in kwargs.items():
            setattr(self, key, value)


async def _run(profile, existing):
    db = _FakeDb2(existing)

    async def get_user_by_email(_db, email):
        return existing

    crud.get_user_by_email = get_user_by_email
    try:
        return await crud.login_or_create_google_user(db, profile)
    finally:
        crud.get_user_by_email = _original


_original = crud.get_user_by_email


def test_google_links_existing_password_account():
    """The reported bug: an existing password account must not 409."""
    existing = _User(
        id=7,
        email="user@test.com",
        auth_provider="email",
        hashed_password="hashed",
    )
    profile = {
        "email": "user@test.com",
        "full_name": "Real Name",
        "profile_picture": "http://img",
        "email_verified": True,
    }

    user = asyncio.run(_run(profile, existing))

    assert user.auth_provider == "google"
    assert user.is_verified is True
    assert user.full_name == "Real Name"
    # Password login must keep working for the linked account.
    assert user.hashed_password == "hashed"


def test_google_does_not_overwrite_user_edited_fields():
    existing = _User(
        id=8,
        email="user@test.com",
        auth_provider="email",
        full_name="Chosen Name",
        profile_picture="http://mine",
        is_verified=True,
    )
    profile = {
        "email": "user@test.com",
        "full_name": "Google Name",
        "profile_picture": "http://google",
        "email_verified": True,
    }

    user = asyncio.run(_run(profile, existing))

    assert user.full_name == "Chosen Name"
    assert user.profile_picture == "http://mine"


def test_deactivated_account_is_refused():
    existing = _User(id=9, email="u@test.com", is_active=False)
    profile = {"email": "u@test.com", "email_verified": True}

    with pytest.raises(Exception) as exc:
        asyncio.run(_run(profile, existing))

    assert getattr(exc.value, "status_code", None) == 403
