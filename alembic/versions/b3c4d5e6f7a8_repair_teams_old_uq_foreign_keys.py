"""repair foreign keys left pointing at the dropped teams_old_uq table

Revision ID: b3c4d5e6f7a8
Revises: e5f6a7b8c9d0
Create Date: 2026-09-26 16:30:00.000000

Migration d1e2f3a4b5c6 replaced the global UNIQUE (name) on `teams` by swapping
the table: rename teams -> teams_old_uq, create a new teams, copy the rows,
drop teams_old_uq.

SQLite's `ALTER TABLE ... RENAME TO` also rewrites the REFERENCES clauses of
every other table pointing at the renamed table. So the rename silently
repointed matches.team_a_id / team_b_id / toss_winner_id, innings.batting_team_id
/ bowling_team_id and player_team_assignments.team_id at `teams_old_uq`, and the
final DROP left all of them dangling at a table that no longer exists.

The breakage was invisible because SQLite defaults to PRAGMA foreign_keys = 0
and nothing ever enabled it, so `PRAGMA foreign_key_check` was the only thing
reporting it. Enabling FK enforcement, or dropping a referenced parent row,
would fail from that point on.

d1e2f3a4b5c6 now sets PRAGMA legacy_alter_table = ON so fresh databases are
built correctly. This migration repairs the databases that were already built
by the broken version, by rewriting the stored REFERENCES text back to `teams`.
It is a no-op on databases that are already correct (including Postgres, where
the table swap never happened).

It has to run *before* f2a3b4c5d6e7, which is why that migration's down_revision
points here: f2a3b4c5d6e7 rebuilds `innings` in batch mode, and reflecting a
table whose foreign keys name a non-existent table raises NoSuchTableError. The
repair therefore has to land first.
"""

import re
from typing import Sequence, Union

from alembic import op


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STALE_TABLE = "teams_old_uq"


def _affected_tables(bind) -> list[str]:
    """Tables whose stored DDL still references the dropped teams_old_uq."""
    tables = []
    for (name, sql) in bind.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ):
        if STALE_TABLE in sql:
            tables.append(name)
    return tables


def _rebuild(bind, table: str, new_sql: str) -> None:
    # The rebuild renames and drops a table that other tables reference, which
    # SQLite only allows with FK enforcement off. The pragma is connection state,
    # not something a migration should leave changed: batch_alter_table in later
    # migrations rebuilds referenced tables too, and would fail against a
    # connection this migration silently switched to foreign_keys=ON. So the
    # incoming value is captured and put back exactly as it was found.
    fk_was = bind.exec_driver_sql("PRAGMA foreign_keys").fetchone()[0]
    legacy_was = bind.exec_driver_sql("PRAGMA legacy_alter_table").fetchone()[0]

    bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        bind.exec_driver_sql("BEGIN")
        bind.exec_driver_sql(f'ALTER TABLE "{table}" RENAME TO "{table}_alembic_fkfix"')
        bind.exec_driver_sql(new_sql)
        bind.exec_driver_sql(
            f'INSERT INTO "{table}" SELECT * FROM "{table}_alembic_fkfix"'
        )
        bind.exec_driver_sql(f'DROP TABLE "{table}_alembic_fkfix"')
        bind.exec_driver_sql("COMMIT")
    except Exception:
        bind.exec_driver_sql("ROLLBACK")
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={int(bool(fk_was))}")
        bind.exec_driver_sql(f"PRAGMA legacy_alter_table={int(bool(legacy_was))}")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    for table in _affected_tables(bind):
        row = bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        new_sql = re.sub(
            rf'REFERENCES\s+"{STALE_TABLE}"', 'REFERENCES "teams"', row[0]
        )
        if new_sql == row[0]:
            continue
        _rebuild(bind, table, new_sql)

    # An interrupted table swap leaves its scratch table behind: either this
    # migration's own, or the _alembic_tmp_* that batch_alter_table uses. The
    # real table survived the rollback, so the scratch copies are pure garbage.
    for pattern in ("%alembic_fkfix", "_alembic_tmp_%"):
        for (t,) in bind.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
            (pattern,),
        ):
            bind.exec_driver_sql(f'DROP TABLE "{t}"')


def downgrade() -> None:
    # The repaired state is the correct one; there is nothing to undo.
    pass
