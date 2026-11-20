"""Compatibility entry point for ``uvicorn main:app``.

The application itself lives in ``app/main.py``; this file only re-exports it so
that the shorter ``uvicorn main:app --reload`` command works when launched from
the repository root. It contains no logic on purpose, so the two entry points
cannot drift apart.

Prefer ``uvicorn app.main:app --reload`` (or ``./run.ps1``): that is what the
Docker image runs, and it keeps the package layout obvious.
"""

from app.main import app

__all__ = ["app"]
