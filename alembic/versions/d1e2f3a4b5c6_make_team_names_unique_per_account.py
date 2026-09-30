"""make team names unique per account

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-09-23 09:30:00.000000

Team names were globally unique; two accounts could not both create a team
with the same name. Replace the global unique on teams.name with a composite
unique (user_id, name) so each account can reuse names like "CSK".
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _swap_table(bind, new_sql: str) -> None:
    """Rebuild the `teams` table from `new_sql`, preserving all rows.

    SQLite cannot drop a constraint-backed index in place, so the table is
    swapped: rename -> create -> copy -> drop.

    `PRAGMA legacy_alter_table=ON` is essential here. With the modern default
    (OFF), `ALTER TABLE ... RENAME TO` also rewrites the REFERENCES clauses of
    every other table that points at the renamed table. Renaming `teams` to
    `teams_old_uq` would therefore repoint matches.team_a_id, innings.*,
    player_team_assignments.team_id and the teams table's own self-references
    at `teams_old_uq`; dropping that table at the end of the swap would then
    leave every one of those foreign keys dangling at a table that no longer
    exists. Legacy rename mode leaves the stored REFERENCES text untouched.

    Both pragmas are connection state, so their incoming values are restored
    rather than assumed: switching foreign_keys back on unconditionally would
    break any later migration that rebuilds a referenced table in batch mode.
    """
    fk_was = bind.exec_driver_sql("PRAGMA foreign_keys").fetchone()[0]
    legacy_was = bind.exec_driver_sql("PRAGMA legacy_alter_table").fetchone()[0]

    bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        bind.exec_driver_sql("BEGIN")
        bind.exec_driver_sql("ALTER TABLE teams RENAME TO teams_old_uq")
        bind.exec_driver_sql(new_sql)
        bind.exec_driver_sql("INSERT INTO teams SELECT * FROM teams_old_uq")
        bind.exec_driver_sql("DROP TABLE teams_old_uq")
        bind.exec_driver_sql("COMMIT")
    except Exception:
        bind.exec_driver_sql("ROLLBACK")
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={int(bool(fk_was))}")
        bind.exec_driver_sql(f"PRAGMA legacy_alter_table={int(bool(legacy_was))}")


def _drop_global_unique(bind) -> None:
    if bind.dialect.name == "postgresql":
        # Postgres stores the inline UNIQUE (name) as a real named constraint,
        # so it can be dropped in place - no table rebuild required.
        bind.exec_driver_sql("ALTER TABLE teams DROP CONSTRAINT IF EXISTS teams_name_key")
        return

    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='teams'"
    ).fetchone()
    old_sql: str = row[0]
    new_sql = re.sub(r",\s*UNIQUE\s*\(\s*name\s*\)", "", old_sql, flags=re.IGNORECASE)
    if new_sql == old_sql:
        return

    _swap_table(bind, new_sql)


def upgrade() -> None:
    bind = op.get_bind()

    _drop_global_unique(bind)

    existing = {i["name"] for i in sa.inspect(bind).get_indexes("teams")}
    if "ix_teams_user_id" not in existing:
        op.create_index("ix_teams_user_id", "teams", ["user_id"])
    if "uq_teams_user_name" not in existing:
        op.execute("CREATE UNIQUE INDEX uq_teams_user_name ON teams (user_id, name)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_teams_user_name")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE teams ADD CONSTRAINT teams_name_key UNIQUE (name)"
        )
        op.execute("CREATE INDEX IF NOT EXISTS ix_teams_user_id ON teams (user_id)")
        return

    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='teams'"
    ).fetchone()
    ddl: str = row[0]
    if "UNIQUE(name)" not in ddl.replace(" ", ""):
        _swap_table(bind, ddl.replace("PRIMARY KEY (id),", "PRIMARY KEY (id),\n\tUNIQUE (name),"))
    op.execute("CREATE INDEX IF NOT EXISTS ix_teams_user_id ON teams (user_id)")
