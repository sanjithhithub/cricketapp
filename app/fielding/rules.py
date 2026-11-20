"""The substitution rules, and where a competition can change them.

Cricket's Laws (24.1) allow a substitute fielder to take the field for an
injured, ill or otherwise approved player, but an *ordinary* substitute then
may not bowl, keep wicket or act as captain; only a competition-approved
special replacement (a concussion replacement under the concussion protocols,
for example) inherits the full privileges of the player replaced.

Which reasons exist, which of them count as "special", whether ordinary
substitutes may bowl/keep/captain at all, and how many substitutions a team
may make are competition rules, not code constants - so every one of them is
read from the environment with a sensible default. Nothing in the application
hard-codes a value that is not defined here.

Environment variables (all optional):

``SUBSTITUTION_ENABLED``
    ``true``/``false``. When false the substitution endpoint refuses every
    request with a clear message, for competitions that do not allow it.
``SUBSTITUTION_REASONS``
    Comma-separated reasons the scorer may pick. Default:
    ``injury,illness,concussion,tactical,other``.
``SUBSTITUTION_SPECIAL_REASONS``
    The subset of those that count as an approved *special* replacement and
    therefore keep the full privileges of an XI player. Default:
    ``concussion``. Empty means no competition in this deployment approves
    special replacements, so every substitution is an ordinary one.
``SUBSTITUTION_MAX_PER_MATCH``
    Maximum substitutions a single team may make in a match. ``0`` (the
    default) means unlimited.
``SUBSTITUTE_CAN_BOWL`` / ``SUBSTITUTE_CAN_KEEP`` / ``SUBSTITUTE_CAN_CAPTAIN``
    Whether an *ordinary* substitute may bowl, keep wicket or captain when
    the competition permits it. Default ``false`` for all three, which is the
    standard Law.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class SubstitutionRules:
    """The rules in force for this deployment."""

    enabled: bool = True
    reasons: tuple[str, ...] = ("injury", "illness", "concussion", "tactical", "other")
    special_reasons: tuple[str, ...] = ("concussion",)
    max_per_match: int = 0
    ordinary_can_bowl: bool = False
    ordinary_can_keep: bool = False
    ordinary_can_captain: bool = False

    @property
    def reason_options(self) -> tuple[dict[str, str], ...]:
        """The reasons offered to the scorer, each tagged ordinary/special.

        The tag is what lets the UI say "this is an approved special
        replacement" rather than silently treating every reserve as one.
        """
        return tuple(
            {"value": reason, "type": "special" if reason in self.special_reasons else "normal"}
            for reason in self.reasons
        )


def load_rules() -> SubstitutionRules:
    """Read the rules from the environment.

    Called per request rather than cached at import so a deployment can change
    a rule (and restart, or just edit the environment) without a code change,
    and so tests can monkeypatch the environment.
    """
    enabled = _bool_env("SUBSTITUTION_ENABLED", True)
    reasons = _list_env("SUBSTITUTION_REASONS", "injury,illness,concussion,tactical,other")
    special = tuple(
        r for r in _list_env("SUBSTITUTION_SPECIAL_REASONS", "concussion") if r in reasons
    )
    max_per_match = int(os.getenv("SUBSTITUTION_MAX_PER_MATCH", "0") or 0)

    return SubstitutionRules(
        enabled=enabled,
        reasons=reasons,
        special_reasons=special,
        max_per_match=max_per_match,
        ordinary_can_bowl=_bool_env("SUBSTITUTE_CAN_BOWL", False),
        ordinary_can_keep=_bool_env("SUBSTITUTE_CAN_KEEP", False),
        ordinary_can_captain=_bool_env("SUBSTITUTE_CAN_CAPTAIN", False),
    )


def replacement_type_for(reason: str, rules: SubstitutionRules | None = None) -> str:
    """``"special"`` when the reason is an approved special replacement."""
    rules = rules or load_rules()
    return "special" if reason in rules.special_reasons else "normal"


def validate_reason(reason: str, rules: SubstitutionRules | None = None) -> str | None:
    """The error for a reason this competition does not allow, or ``None``."""
    rules = rules or load_rules()
    if not rules.enabled:
        return "Substitutions are not permitted by this competition's rules"
    if reason not in rules.reasons:
        return f"Substitution reason '{reason}' is not permitted; allowed reasons are " + ", ".join(
            rules.reasons
        )
    return None
