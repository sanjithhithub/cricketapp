"""add MCC run components and idempotency key to deliveries

Revision ID: a7e1f4c2b9d6
Revises: f9a8b7c6d5e4
Create Date: 2026-11-17 09:00:00.000000

Extras are now stored the way the Laws name them - wides, no balls, byes, leg
byes and penalty runs each with their own figure - because one ``extra_type``
plus one ``runs_extras`` number cannot say "a no ball that also went for two
byes". The old pair is kept exactly as it was: ``runs_extras`` remains the
single number every existing query reads, and the invariant

    runs_extras == runs_wides + runs_no_balls + runs_byes + runs_leg_byes
                   + runs_penalty

is what makes the split a pure refinement rather than a rewrite.

Existing rows are backfilled from ``extra_type`` - which is precisely how the
old storage defined the split - so no data is reset and every past innings
replays to the same totals and the same ends. ``completed_runs`` and
``boundary_runs`` are deliberately left NULL on old rows: NULL means "derive
it", and the derivation (total minus the penalty, minus a 4/6 allowance) has
the same parity as the old completed-runs figure, so the ends do not move.

``batters_crossed`` stays NULL everywhere it was not answered: on a legacy row
the crossing is simply not recorded, and the parity of the completed runs is
all that can be known.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7e1f4c2b9d6"
down_revision: Union[str, Sequence[str], None] = "f9a8b7c6d5e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The five components, with the extra_type that used to define each one. A
# dead ball and a plain delivery have no extras at all, so they backfill to 0.
_COMPONENT_FOR_EXTRA = {
    "wide": "runs_wides",
    "no_ball": "runs_no_balls",
    "bye": "runs_byes",
    "leg_bye": "runs_leg_byes",
}


def upgrade() -> None:
    # Added one at a time rather than through a batch rebuild: ADD COLUMN with
    # a constant default is legal everywhere, and not rebuilding means no
    # chance of dropping a primary key or an index out from under the foreign
    # keys that point at this table (the referee migration's deploy killer).
    for name in (
        "runs_wides",
        "runs_no_balls",
        "runs_byes",
        "runs_leg_byes",
        "runs_penalty",
    ):
        op.add_column(
            "deliveries",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    for name in ("completed_runs", "boundary_runs", "total_team_runs"):
        op.add_column(
            "deliveries",
            sa.Column(name, sa.Integer(), nullable=True),
        )

    op.add_column(
        "deliveries",
        sa.Column("batters_crossed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "deliveries",
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
    )

    # Backfill the split from extra_type. This is not a guess: it is the rule
    # the old code itself used to turn (extra_type, runs_extras) into a
    # component, so a migrated row reads back exactly what it always meant.
    for extra_type, column in _COMPONENT_FOR_EXTRA.items():
        op.execute(
            sa.text(
                f"UPDATE deliveries SET {column} = runs_extras "
                "WHERE extra_type = :extra AND runs_extras > 0"
            ).bindparams(extra=extra_type)
        )

    # The total is a plain sum, so it may be recorded now; the completed /
    # boundary split is left NULL on purpose (see the module docstring).
    op.execute(
        sa.text(
            "UPDATE deliveries SET total_team_runs = runs_batsman + runs_extras"
        )
    )

    # Idempotency: a retry carrying the same key must not score the ball
    # twice. SQLite and PostgreSQL both treat NULLs as distinct, so every
    # legacy row (and every client that does not send a key) stays legal.
    # A partial index would also work, but the plain unique constraint is
    # what the model declares, and the schema-drift test compares columns -
    # the constraint itself is what stops the duplicate insert.
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.create_unique_constraint(
            "uq_deliveries_innings_client_request",
            ["innings_id", "client_request_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.drop_constraint(
            "uq_deliveries_innings_client_request",
            type_="unique",
        )

    for name in ("client_request_id", "batters_crossed"):
        op.drop_column("deliveries", name)
    for name in ("total_team_runs", "boundary_runs", "completed_runs"):
        op.drop_column("deliveries", name)
    for name in ("runs_penalty", "runs_leg_byes", "runs_byes", "runs_no_balls", "runs_wides"):
        op.drop_column("deliveries", name)
