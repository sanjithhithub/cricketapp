"""Player identity: permanent codes, phone normalisation, duplicate matching.

Three rules drive everything in this module.

1. A player's identity is ``players.id`` plus the permanent ``player_code`` it is
   born with. A name is display data: two different players may share one, and a
   player may be renamed freely without breaking matches, scorecards or stats.

2. Mobile numbers are strings, never integers. An integer column cannot hold a
   leading zero ("098123 45678" became 9812345678) and cannot hold a country code
   alongside the national number. The stored form is what the user should see
   back; comparison happens on ``phone_e164``, a canonical string built from the
   country code so that "+91 98123 45678", "098123 45678" and "919812345678" all
   collapse to the same key.

3. Nothing is ever merged automatically. A shared name or a shared number is a
   signal to *ask*, never to decide: the caller gets the candidates back and a
   human confirms.
"""

import re
import secrets

# Crockford-ish base32: no I, L, O, U, so codes survive being read aloud or
# copied off a screen without turning into each other.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PREFIX = "CKP"
CODE_LENGTH = 8

_NON_DIGITS = re.compile(r"\D")


def normalize_country_code(raw: str | None) -> str:
    """Return a country code as ``+<digits>``; empty string when nothing usable.

    Accepts "+91", "91", "+91 ", " 091 " and friends. The stored form always
    carries the leading plus so it can be concatenated with a national number
    to form a canonical phone key.
    """
    if raw is None:
        return ""
    digits = _NON_DIGITS.sub("", str(raw))
    if not digits:
        return ""
    return f"+{digits}"


def normalize_mobile(raw: str | int | None) -> str | None:
    """Return the number as the user gave it, digits only, or ``None``.

    ``None``/empty means "this player has no mobile number", which the app allows:
    a number may be optional and two players may legitimately share one.

    Leading zeros are *kept* here. They are part of the number the user typed and
    part of what a scorer may read back, and an integer column could not hold them
    at all - which is the whole reason this is a string. They are removed only for
    comparison, in :func:`national_digits`.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; never a phone number.
        return None
    digits = _NON_DIGITS.sub("", str(raw))
    return digits or None


def national_digits(country_code: str | int | None, mobile: str | int | None) -> str | None:
    """The subscriber's number on its own, ready to be compared.

    Strips the international prefix if the user pasted the number in E.164 form
    into a field that already has a country code, and strips the trunk zero that
    some countries dial. "+91 98123 45678" and "098123 45678" both come out as
    ``9812345678``, which is what makes the duplicate check fire on a number
    typed three different ways.
    """
    digits = normalize_mobile(mobile)
    if not digits:
        return None
    cc = normalize_country_code(country_code)
    if cc and len(digits) > len(cc) - 1 and digits.startswith(cc[1:]):
        digits = digits[len(cc) - 1 :]
    stripped = digits.lstrip("0")
    return stripped or None


def phone_e164(country_code: str | int | None, mobile: str | int | None) -> str | None:
    """Canonical comparison key for a phone number, or ``None`` if unusable.

    Both halves are normalised first, so the key does not depend on how the user
    typed the number. A number with no country code cannot be canonicalised and
    is deliberately left out of duplicate matching rather than guessed at.
    """
    cc = normalize_country_code(country_code)
    national = national_digits(cc, mobile)
    if not cc or not national:
        return None
    return f"{cc}{national}"


def generate_player_code() -> str:
    """Return a fresh random code, e.g. ``CKP-7F3KQ2ZM``.

    Random rather than derived from the name or the row id: a code must survive a
    rename, must not leak the name, and must not be guessable from one player's
    code to the next. Uniqueness is enforced by the unique index, and the caller
    retries on the (vanishingly rare) collision.
    """
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{CODE_PREFIX}-{body}"


def mask_mobile(country_code: str | None, mobile: str | None) -> str | None:
    """Return a phone number safe to show in a list, dropdown or search result.

    Keeps the country code and the last two digits, which is what a scorer needs
    to tell two same-named players apart, and hides the rest. A player with no
    number stays ``None`` rather than becoming a misleading "•••".
    """
    cc = normalize_country_code(country_code) if country_code else ""
    national = national_digits(cc, mobile)
    if national is None:
        return None
    if len(national) <= 2:
        return f"{cc}{'*' * len(national)}" if cc else "*" * len(national)
    hidden = len(national) - 2
    return f"{cc}{'*' * hidden}{national[-2:]}"


def full_name(first_name: str | None, last_name: str | None) -> str:
    """Display name, tolerant of missing parts. Never used for identity."""
    return " ".join(part for part in (first_name, last_name) if part).strip()


def alias_key(value: str) -> str:
    """Case- and space-insensitive form of a name, for comparing lookups only.

    Used to decide that "  rahul " and "Rahul" are the same *string*. Two rows
    having the same key is allowed and expected; it means "possible duplicate",
    never "the same player".
    """
    return " ".join(str(value or "").lower().split())


def name_key(first_name: str | None, last_name: str | None) -> str:
    """Normalised full name used to warn about possible duplicates."""
    return alias_key(full_name(first_name, last_name))


def is_valid_code(value: str) -> bool:
    """True when ``value`` looks like a player code this app would have issued."""
    value = str(value or "").strip().upper()
    if not value.startswith(f"{CODE_PREFIX}-"):
        return False
    body = value[len(CODE_PREFIX) + 1 :]
    return len(body) == CODE_LENGTH and all(c in _CODE_ALPHABET for c in body)
