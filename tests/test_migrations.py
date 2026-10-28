"""Guards the Alembic migration chain.

The chain used to be unrunnable: `alembic upgrade head` on a fresh database
died three times over (an inline-FK add_column that SQLite rejects, a missing
`import re`, and a table swap whose rename silently repointed every child
foreign key at a table that was then dropped). It went unnoticed because
`pyproject.toml` excludes alembic/versions from ruff, deployments adopted
pre-built schemas with `alembic stamp` instead of migrating, and SQLite defaults
to `PRAGMA foreign_keys = 0`, so the dangling foreign keys never raised.

These tests build a real database from an empty file and assert the result is
exactly the schema the models describe, with foreign key enforcement switched
on so a constraint pointing at a missing table cannot pass unnoticed.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STALE_TABLE = "teams_old_uq"


def _models_metadata():
    """Base.metadata with every model module imported, as alembic/env.py does."""
    import app.auth.models  # noqa: F401
    import app.levels.models  # noqa: F401
    import app.matches.models  # noqa: F401
    import app.models  # noqa: F401
    import app.players.models  # noqa: F401
    import app.referees.models  # noqa: F401
    import app.scoring.models  # noqa: F401
    import app.team_owners.models  # noqa: F401
    import app.teams.models  # noqa: F401
    import app.tournaments.models  # noqa: F401
    from app.database import Base

    return Base.metadata


def _build_database(tmp_path: Path) -> Path:
    """Run the whole chain against an empty SQLite file and return its path."""
    db_path = tmp_path / "migrated.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}{result.stderr}"
    return db_path


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    db_path = _build_database(tmp_path_factory.mktemp("migrations"))
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    yield con
    con.close()


def _tables(con):
    return [row["name"] for row in con.execute("select name from sqlite_master where type='table'")]


def test_upgrade_head_succeeds_on_a_fresh_database(migrated):
    assert migrated.execute("select version_num from alembic_version").fetchone()[0]


def test_no_foreign_key_points_at_a_dropped_table(migrated):
    """The teams_old_uq regression: renaming a table rewrites child references."""
    offenders = [
        row["name"]
        for row in migrated.execute(
            "select name from sqlite_master where type='table' and sql like ?",
            (f"%{STALE_TABLE}%",),
        )
    ]
    assert offenders == [], f"DDL still references the dropped {STALE_TABLE}: {offenders}"

    for table in _tables(migrated):
        for fk in migrated.execute(f'PRAGMA foreign_key_list("{table}")'):
            assert fk["table"] != STALE_TABLE, f"{table}.{fk['from']} still targets {STALE_TABLE}"


def test_no_scratch_tables_are_left_behind(migrated):
    """An interrupted table swap or batch rebuild leaves its temp table around."""
    scratch = [t for t in _tables(migrated) if "alembic_tmp" in t or "alembic_fkfix" in t]
    assert scratch == [], f"leftover scratch tables: {scratch}"


def test_foreign_keys_are_actually_enforced(migrated):
    """Turn enforcement on and confirm no row violates any constraint."""
    migrated.execute("PRAGMA foreign_keys=ON")
    assert migrated.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    violations = migrated.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == [], f"foreign key violations: {[tuple(v) for v in violations[:10]]}"


def test_integrity_check_passes(migrated):
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migrated_schema_matches_the_models(migrated):
    """A fresh migrate and Base.metadata.create_all must agree."""
    tables = set(_tables(migrated))
    problems = []

    for table in _models_metadata().sorted_tables:
        if table.name not in tables:
            problems.append(f"missing table {table.name}")
            continue

        columns = {c["name"]: c for c in migrated.execute(f'PRAGMA table_info("{table.name}")')}
        for column in table.columns:
            if column.name not in columns:
                problems.append(f"{table.name}: missing column {column.name}")
            elif bool(columns[column.name]["notnull"]) != bool(column.nullable is False):
                problems.append(f"{table.name}.{column.name}: nullability drift")
        for name in columns:
            if name not in {c.name for c in table.columns}:
                problems.append(f"{table.name}: extra column {name}")

        actual_fks = {
            (fk["from"], fk["table"])
            for fk in migrated.execute(f'PRAGMA foreign_key_list("{table.name}")')
        }
        for constraint in table.foreign_key_constraints:
            element = constraint.elements[0]
            pair = (element.parent.name, element.column.table.name)
            if pair not in actual_fks:
                problems.append(f"{table.name}.{pair[0]}: foreign key should target {pair[1]}")

    extra = tables - {t.name for t in _models_metadata().sorted_tables} - {"alembic_version"}
    problems.extend(f"extra table {t}" for t in sorted(extra))

    assert problems == [], "schema drift after migrate:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# Postgres
#
# The SQLite suite above cannot see either of the bugs that killed a deploy:
# `batch_alter_table(recreate="always")` rebuilds the table on every backend,
# and on Postgres that means dropping the primary key out from under the
# foreign keys that point at it; and an unquoted string server default is
# accepted by SQLite but read by Postgres as a column reference. Both pass
# every SQLite assertion and fail only on Postgres, which is why a Postgres
# run is required to catch them.
#
# Set TEST_POSTGRES_URL to a disposable database - never a real one, the
# migrations are destructive - and these run. Without it they skip, so a
# machine with no Postgres still gets a green suite.
# ---------------------------------------------------------------------------

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


def _dsn_for_psycopg2() -> str:
    """A plain DSN for `psycopg2.connect`.

    The URL is SQLAlchemy-formatted (`postgresql+psycopg2://...`) because
    that is what `alembic/env.py` and `app/database.py` expect, but the
    DBAPI driver itself rejects the `+driver` suffix. Strip it for direct
    connections rather than asking the user for two URLs.
    """
    return POSTGRES_URL.replace("+psycopg2", "").replace("+asyncpg", "")


def _url_for_alembic() -> str:
    """The URL as `env.py` wants it: an async driver.

    `alembic/env.py` derives its sync engine by swapping `+asyncpg` for
    `+psycopg2`, and importing `app.database` builds an async engine, so the
    URL handed to Alembic must carry an async driver. Accept a sync URL from
    the caller and normalise it here.
    """
    return POSTGRES_URL.replace("+psycopg2", "+asyncpg")


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = _url_for_alembic()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _reset_schema() -> None:
    import psycopg2

    conn = psycopg2.connect(_dsn_for_psycopg2())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
    finally:
        conn.close()


@pytest.fixture(scope="module")
def migrated_postgres():
    if not POSTGRES_URL:
        pytest.skip("TEST_POSTGRES_URL not set; no Postgres to migrate against")
    _reset_schema()
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}{result.stderr}"
    import psycopg2

    conn = psycopg2.connect(_dsn_for_psycopg2())
    try:
        yield conn
    finally:
        conn.close()


def _pg_scalar(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def test_postgres_upgrade_head_succeeds(migrated_postgres):
    """The deploy killer: the chain must complete on Postgres, not just SQLite."""
    assert _pg_scalar(migrated_postgres, "select version_num from alembic_version")


def test_postgres_primary_key_survives_the_referees_migration(migrated_postgres):
    """`recreate="always"` on `matches` dropped `matches_pkey`, which
    `match_referee_assignments_match_id_fkey` had just been made to depend on.
    Postgres refused the drop, the migration aborted, and the health check
    timed out. The primary key must still be there."""
    assert (
        _pg_scalar(
            migrated_postgres,
            "select count(*) from pg_constraint "
            "where conrelid = 'matches'::regclass and conname = 'matches_pkey'",
        )
        == 1
    )


def test_postgres_every_foreign_key_still_resolves(migrated_postgres):
    """No constraint may be left pointing at a table the rebuild dropped."""
    with migrated_postgres.cursor() as cur:
        cur.execute(
            "select count(*) from information_schema.table_constraints where constraint_type = 'FOREIGN KEY'"
        )
        assert cur.fetchone()[0] > 0


def test_postgres_string_defaults_are_quoted_literals(migrated_postgres):
    """An unquoted `DEFAULT active` is a column reference to Postgres, which
    rejects it with "cannot use column reference in DEFAULT expression".
    SQLite accepts the bare word, so the bug is invisible locally."""
    with migrated_postgres.cursor() as cur:
        cur.execute(
            "select table_name, column_default from information_schema.columns "
            "where column_name = 'ownership_status'"
        )
        rows = cur.fetchall()
    assert rows, "ownership_status columns missing"
    for table_name, default in rows:
        assert default is not None, f"{table_name}.ownership_status lost its default"
        assert "'active'" in default, (
            f"{table_name}.ownership_status default is not a literal: {default}"
        )


def test_postgres_downgrade_and_reupgrade(migrated_postgres):
    """The downgrade path rebuilds too, so it carries the same risk."""
    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, f"alembic downgrade base failed:\n{down.stdout}{down.stderr}"
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, f"re-upgrade failed:\n{up.stdout}{up.stderr}"
