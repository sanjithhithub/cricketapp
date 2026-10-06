"""relax players.email uniqueness to a non-unique index

Revision ID: ef1a2b3c4d5e
Revises: d7e8f9a0b1c2
Create Date: 2026-10-06 10:00:00.000000

`players.email` carried a global UNIQUE constraint since the initial schema,
but the app treats a player's email as a contact detail, not an identity. Two
players genuinely share one address all the time - a parent's email for two
children, a club's shared inbox - exactly like the phone, which is deliberately
NOT unique. The constraint leaked as a 500: `create_player` pre-checks only the
phone and the name, so a duplicate email hit the unique index, and the IntegrityError
handler (written for player_code collisions) rolled back and retried the identical
insert, whose second IntegrityError escaped as a server error.

Replace the unique with a plain index so the duplicate check stays fast while a
second row with the same email stops being a database error.
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ef1a2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PLAYER_INDEXES = {
    "ix_players_user_id": ["user_id"],
    "ix_players_phone_e164": ["phone_e164"],
    "ix_players_name_lookup": ["user_id", "last_name", "first_name"],
    "ix_players_email": ["email"],
}


def _swap_table(bind, new_sql: str) -> None:
    """Rebuild the `players` table from `new_sql`, preserving all rows.

    SQLite cannot drop a constraint-backed index in place, so the table is
    swapped: rename -> create -> copy -> drop.

    `PRAGMA legacy_alter_table=ON` is essential here. With the modern default
    (OFF), `ALTER TABLE ... RENAME TO` also rewrites the REFERENCES clauses of
    every other table that points at the renamed table. Renaming `players` to
    `players_old_uq` would therefore repoint innings_batsmen.player_id,
    deliveries.*, innings.next_batsman_id, player_aliases.player_id and
    player_team_assignments.player_id at `players_old_uq`; dropping that table
    at the end of the swap would then leave every one of those foreign keys
    dangling at a table that no longer exists. Legacy rename mode leaves the
    stored REFERENCES text untouched.

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
        bind.exec_driver_sql("ALTER TABLE players RENAME TO players_old_uq")
        bind.exec_driver_sql(new_sql)
        bind.exec_driver_sql("INSERT INTO players SELECT * FROM players_old_uq")
        bind.exec_driver_sql("DROP TABLE players_old_uq")
        bind.exec_driver_sql("COMMIT")
    except Exception:
        bind.exec_driver_sql("ROLLBACK")
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={int(bool(fk_was))}")
        bind.exec_driver_sql(f"PRAGMA legacy_alter_table={int(bool(legacy_was))}")


def _ensure_indexes(bind) -> None:
    existing = {i["name"] for i in sa.inspect(bind).get_indexes("players")}
    for name, columns in _PLAYER_INDEXES.items():
        if name not in existing:
            op.create_index(name, "players", columns)


def _drop_email_unique(bind) -> None:
    if bind.dialect.name == "postgresql":
        # Postgres stores the inline UNIQUE (email) as a real named constraint,
        # so it can be dropped in place - no table rebuild required.
        bind.exec_driver_sql("ALTER TABLE players DROP CONSTRAINT IF EXISTS players_email_key")
        return

    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='players'"
    ).fetchone()
    old_sql: str = row[0]
    new_sql = re.sub(r",\s*UNIQUE\s*\(\s*email\s*\)", "", old_sql, flags=re.IGNORECASE)
    if new_sql == old_sql:
        return

    _swap_table(bind, new_sql)


def upgrade() -> None:
    bind = op.get_bind()

    _drop_email_unique(bind)
    _ensure_indexes(bind)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS ix_players_email")

    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE players ADD CONSTRAINT players_email_key UNIQUE (email)"
        )
        _ensure_indexes(bind)
        return

    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='players'"
    ).fetchone()
    ddl: str = row[0]
    if "UNIQUE(email)" not in ddl.replace(" ", ""):
        _swap_table(bind, ddl.replace("PRIMARY KEY (id),", "PRIMARY KEY (id),\n\tUNIQUE (email),"))
    _ensure_indexes(bind)
