from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current UTC time as a *naive* datetime.

    Replaces the deprecated ``datetime.utcnow()``. Naive on purpose: every
    timestamp column in this project is a naive ``DateTime`` (see
    :class:`~app.auth.models.RefreshToken`), and comparing a naive column
    against an aware ``now`` raises ``TypeError`` - which would turn every
    token refresh and OTP lookup into a 500.

    The value is computed timezone-aware first and then stripped, so it is
    genuinely UTC rather than server-local time the way ``utcnow()`` was
    documented but (in CPython <= 3.12) not actually guaranteed.
    """
    return datetime.now(UTC).replace(tzinfo=None)
