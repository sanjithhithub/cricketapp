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
    import app.scoring.models  # noqa: F401
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
