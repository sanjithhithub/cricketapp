"""add substitute fielders and substitution history

Revision ID: c3d4e5f6a7b8
Revises: f7a1c3e5b9d2
Create Date: 2026-10-31 12:00:00.000000

Three changes, all inside the fielding tables:

1. ``fielding_assignments`` gains ``is_substitute`` (did this fielder come on
   through a substitution, or start in the XI?) and ``custom_label`` (the
   scorer's own name for a position outside every catalog anchor). Both are
   additive: ``is_substitute`` defaults to false, which is exactly what every
   existing row means, and ``custom_label`` is NULL for every existing row.

2. The unique constraint on ``(setup_id, position_key)`` is dropped. Two
   fielders may legitimately share a named area - a second and third slip
   both standing in the cordon, or a captain who wants two men at point - as
   long as their coordinates differ. Uniqueness by position *name* alone was
   the wrong rule; ``(setup_id, player_id)`` stays unique, so one player can
   still never stand in two places.

3. The new ``field_substitutions`` table records who left, who came on, for
   which side and innings, why, whether it was an ordinary or an approved
   special replacement, who authorised it and when. Nothing existing is
   deleted or rewritten: no match, player, team, score or result row is
   touched, and the existing fielding rows survive with their meaning intact.

Every step is guarded, following the pattern set by ``e2f3a4b5c6d7``: the
SQLite startup path runs ``Base.metadata.create_all()``, which builds the
*tables* this revision would create but never ALTERs an existing table to add
the *columns*. A database can therefore arrive here in three states - empty
(raise everything from scratch), built by ``create_all`` (table present,
columns absent), or already migrated (nothing to do) - and all three must
succeed. Un-guarded, the second state fails with "table already exists" and
leaves the fielding endpoints answering 500.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "f7a1c3e5b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# name, type, nullable, server_default - in the order they are added.
ASSIGNMENT_COLUMNS = (
    ("is_substitute", sa.Boolean(), False, sa.false()),
    ("custom_label", sa.String(length=60), True, None),
)

# The old rule that a position name could only be held once.
STALE_POSITION_UNIQUE = "uq_fielding_assignment_setup_position"

SUBSTITUTION_INDEXES = (
    ("ix_field_substitutions_match_id", ["match_id"]),
    ("ix_field_substitutions_setup_id", ["setup_id"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # --- 1 & 2. assignments: two new columns, one dropped constraint -------
    _add_assignment_columns_and_drop_unique(inspector)

    # --- 3. the substitution history --------------------------------------
    _create_substitutions_table(inspector)


def _add_assignment_columns_and_drop_unique(inspector) -> None:
    """Bring ``fielding_assignments`` to the shape the model describes.

    A database this revision already migrated has both columns and no stale
    constraint, so there is nothing to do. One built by ``create_all`` has the
    table but is missing the columns, and still carries the old
    unique-on-position constraint that ``create_all`` never removed.
    """
    columns = {column["name"] for column in inspector.get_columns("fielding_assignments")}
    missing = [spec for spec in ASSIGNMENT_COLUMNS if spec[0] not in columns]

    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("fielding_assignments")
    }
    has_stale_unique = STALE_POSITION_UNIQUE in unique_constraints

    if not missing and not has_stale_unique:
        return

    # batch_alter_table rebuilds the table on SQLite (which cannot ALTER a
    # constraint away) and issues plain ALTERs on Postgres, so this one block
    # is safe on both. `recreate="auto"` is deliberate - the same reasoning as
    # in e2f3a4b5c6d7: `"always"` would rebuild on Postgres too and drop the
    # primary key out from under any foreign key pointing at this table.
    with op.batch_alter_table("fielding_assignments") as batch_op:
        for name, column_type, nullable, server_default in missing:
            batch_op.add_column(
                sa.Column(name, column_type, nullable=nullable, server_default=server_default)
            )
        if has_stale_unique:
            batch_op.drop_constraint(STALE_POSITION_UNIQUE, type_="unique")


def _create_substitutions_table(inspector) -> None:
    """The append-only history of every substitution.

    Guarded on the table and on each index: ``create_all`` builds the table
    *and* its indexes from the model, so a database that took that path needs
    no work here at all, while a half-applied run may have the table without
    one of the indexes.
    """
    if "field_substitutions" not in set(inspector.get_table_names()):
        op.create_table(
            "field_substitutions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id"), nullable=False),
            sa.Column("setup_id", sa.Integer(), sa.ForeignKey("fielding_setups.id"), nullable=False),
            sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
            sa.Column("innings_number", sa.Integer(), nullable=False),
            sa.Column("outgoing_player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
            sa.Column("incoming_player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
            sa.Column("reason", sa.String(length=60), nullable=False),
            sa.Column(
                "replacement_type",
                sa.String(length=20),
                nullable=False,
                server_default="normal",
            ),
            sa.Column("authorized_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )

    # Re-inspect: the table may have just been created above, and the index
    # list must reflect that rather than the snapshot taken on entry.
    live = sa.inspect(op.get_bind())
    existing = {index["name"] for index in live.get_indexes("field_substitutions")}
    for name, index_columns in SUBSTITUTION_INDEXES:
        if name not in existing:
            op.create_index(name, "field_substitutions", index_columns, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "field_substitutions" in set(inspector.get_table_names()):
        live = sa.inspect(op.get_bind())
        existing = {index["name"] for index in live.get_indexes("field_substitutions")}
        for name, _ in SUBSTITUTION_INDEXES:
            if name in existing:
                op.drop_index(name, table_name="field_substitutions")
        op.drop_table("field_substitutions")

    columns = {column["name"] for column in inspector.get_columns("fielding_assignments")}
    droppable = [name for name, *_ in reversed(ASSIGNMENT_COLUMNS) if name in columns]

    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("fielding_assignments")
    }
    needs_position_unique = STALE_POSITION_UNIQUE not in unique_constraints

    if not droppable and not needs_position_unique:
        return

    with op.batch_alter_table("fielding_assignments") as batch_op:
        if needs_position_unique:
            batch_op.create_unique_constraint(
                STALE_POSITION_UNIQUE, ["setup_id", "position_key"]
            )
        for name in droppable:
            batch_op.drop_column(name)
