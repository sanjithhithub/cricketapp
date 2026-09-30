"""add player codes, string phone numbers and player aliases

Identity used to be "whatever the name and phone happened to be". This gives
every player a permanent `player_code` that survives renames, turns
`players.mobile_number` from a bigint into text so leading zeros and country
codes are no longer mangled, and stores a canonical `phone_e164` to compare
against instead of the display-form number.

The unique constraint on (country_code, mobile_number) is dropped rather than
recreated: a phone number is optional in this app and may legitimately be shared
(club landline, one handset for two siblings), so a database constraint would
reject registrations that are legitimate. `ix_players_phone_e164` is a plain
index that makes the "is this number already registered?" check fast; the check
itself returns candidates for a human to confirm and never merges or blocks on
its own.

Existing rows are preserved. The backfill derives `phone_e164` from the stored
country code and number and gives every existing player a freshly generated
code, so no player is left without one and no id changes.

Revision ID: b7d3e1a9c204
Revises: f2a3b4c5d6e7
Create Date: 2026-09-28 11:20:00.000000

"""

import re
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d3e1a9c204"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PREFIX = "CKP"
CODE_LENGTH = 8

# Same canonical form as app.players.identity. Duplicated deliberately: a
# migration has to keep producing the same value after the module it mirrors is
# edited, or re-running it on another database would rewrite the column
# differently. `re` is imported for the same reason - a missing import here only
# shows up when the migration runs against a database that needs the backfill.
_NON_DIGITS = re.compile(r"\D")


def _phone_e164(country_code, mobile) -> str | None:
    cc_digits = _NON_DIGITS.sub("", str(country_code or ""))
    national = _NON_DIGITS.sub("", str(mobile or "")).lstrip("0")
    if not cc_digits or not national:
        return None
    return f"+{cc_digits}{national}"


def _generate_player_code(taken: set) -> str:
    while True:
        body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
        code = f"{CODE_PREFIX}-{body}"
        if code not in taken:
            return code


class _PragmaGuard:
    """Keep SQLite's connection-level rename pragma as it was found.

    batch_alter_table rebuilds `players` by renaming a scratch table into place.
    With `legacy_alter_table` off, SQLite rewrites the REFERENCES clauses of every
    child table on rename. The pragma is connection state, not something a
    migration should leave changed, so it is captured and put back exactly.
    """

    def __init__(self, bind) -> None:
        self.bind = bind
        self._was = None

    def __enter__(self):
        if self.bind.dialect.name != "sqlite":
            return self
        try:
            self._was = self.bind.exec_driver_sql("PRAGMA legacy_alter_table").fetchone()[0]
            self.bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        except Exception:
            self._was = None
        return self

    def __exit__(self, *exc):
        if self._was is not None:
            self.bind.exec_driver_sql(f"PRAGMA legacy_alter_table={int(bool(self._was))}")
        return False


def upgrade() -> None:
    bind = op.get_bind()

    with _PragmaGuard(bind):
        # 1. One rebuild: add the new columns, relax mobile_number to text and
        #    optional, and drop the uniqueness the app no longer wants. Adding
        #    the columns nullable keeps the existing rows valid throughout.
        with op.batch_alter_table("players", schema=None) as batch_op:
            batch_op.add_column(sa.Column("player_code", sa.String(16), nullable=True))
            batch_op.add_column(sa.Column("phone_e164", sa.String(20), nullable=True))
            batch_op.drop_constraint("uq_player_phone", type_="unique")
            batch_op.alter_column(
                "mobile_number",
                existing_type=sa.BigInteger(),
                type_=sa.String(20),
                existing_nullable=False,
                nullable=True,
                postgresql_using="mobile_number::text",
            )

        # 2. Backfill. Reads every row, so it also covers databases that already
        #    contain players; a row that somehow already has a code keeps it.
        taken = {
            row[0] for row in bind.execute(sa.text("SELECT player_code FROM players")).all() if row[0]
        }
        for player_id, country_code, mobile_number, existing_code in bind.execute(
            sa.text("SELECT id, country_code, mobile_number, player_code FROM players")
        ).all():
            code = existing_code or _generate_player_code(taken)
            taken.add(code)
            bind.execute(
                sa.text("UPDATE players SET player_code = :code, phone_e164 = :e164 WHERE id = :id"),
                {"code": code, "e164": _phone_e164(country_code, mobile_number), "id": player_id},
            )

        # 3. Every row now has a code, so it can be NOT NULL, and the indexes the
        #    duplicate check and the name lookup rely on can be created.
        with op.batch_alter_table("players", schema=None) as batch_op:
            batch_op.alter_column("player_code", existing_type=sa.String(16), nullable=False)
            batch_op.create_unique_constraint("uq_players_player_code", ["player_code"])
            batch_op.create_index("ix_players_phone_e164", ["phone_e164"], unique=False)
            batch_op.create_index("ix_players_name_lookup", ["user_id", "last_name", "first_name"])

    # 4. OTP lookups are keyed on the same numbers, so they get the same text
    #    treatment. Left as a bigint they would compare 98123 to "98123" and
    #    never match, silently breaking phone verification.
    with op.batch_alter_table("otps", schema=None) as batch_op:
        batch_op.alter_column(
            "mobile_number",
            existing_type=sa.BigInteger(),
            type_=sa.String(20),
            existing_nullable=False,
            nullable=False,
            postgresql_using="mobile_number::text",
        )

    # 5. Nicknames resolve to the same player id instead of spawning a new row.
    op.create_table(
        "player_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("alias_key", sa.String(100), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("player_id", "alias_key", name="uq_player_alias_key"),
    )
    op.create_index("ix_player_aliases_alias_key", "player_aliases", ["alias_key"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_player_aliases_alias_key", table_name="player_aliases")
    op.drop_table("player_aliases")

    with _PragmaGuard(bind):
        with op.batch_alter_table("players", schema=None) as batch_op:
            batch_op.drop_index("ix_players_name_lookup")
            batch_op.drop_index("ix_players_phone_e164")
            batch_op.drop_constraint("uq_players_player_code", type_="unique")
            batch_op.drop_column("phone_e164")
            batch_op.drop_column("player_code")

        # Back to a required integer number. Players registered without one cannot
        # be represented, so they get 0 rather than being deleted: losing a player
        # would orphan their matches, wickets and statistics. Also drops the
        # leading zero an int column cannot hold, which is the loss that made
        # this column a string in the first place.
        for player_id, number in bind.execute(
            sa.text("SELECT id, mobile_number FROM players")
        ).all():
            digits = _NON_DIGITS.sub("", str(number or "")).lstrip("0")
            bind.execute(
                sa.text("UPDATE players SET mobile_number = :n WHERE id = :id"),
                {"n": int(digits or 0), "id": player_id},
            )

        with op.batch_alter_table("players", schema=None) as batch_op:
            batch_op.alter_column(
                "mobile_number",
                existing_type=sa.String(20),
                type_=sa.BigInteger(),
                existing_nullable=True,
                nullable=False,
                postgresql_using="mobile_number::bigint",
            )
            batch_op.create_unique_constraint("uq_player_phone", ["country_code", "mobile_number"])
