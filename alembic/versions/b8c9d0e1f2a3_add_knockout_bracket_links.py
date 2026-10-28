"""link knockout rounds and allow undecided participants

Revision ID: b8c9d0e1f2a3
Revises: f3a4b5c6d7e8
Create Date: 2026-10-23 10:00:00.000000

A knockout draw is more than a list of pairings: a semi-final feeds the final.
The old table could not express that, so the generator created one round and
stopped, and the previous workaround - putting the same teams into the next
round by hand - could advance the wrong side or leave a team in two places.

Two changes make the bracket storable:

* ``team_a_id``/``team_b_id`` become nullable. Before the semi-finals are
  played the final has no participants, and ``NULL`` says "to be decided"
  honestly instead of inventing a placeholder team.
* ``next_fixture_id``/``next_slot`` record the edge from a fixture to the slot
  its winner fills. ``next_slot`` is "a" or "b"; the pair is meaningless
  outside a generated knockout draw.

``match_id`` also gains a unique constraint: one scored match can stand for at
most one fixture, so a result cannot be counted twice by linking it to two
different rows. Multiple NULLs remain allowed, which is what an unlinked
fixture is.

SQLite cannot alter a column's nullability in place, so the table is rebuilt
with Alembic batch mode. Its data is copied as-is; nothing is dropped.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tournament_fixtures", recreate="always") as batch_op:
        batch_op.alter_column("team_a_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("team_b_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("next_fixture_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("next_slot", sa.String(length=1), nullable=True))
        batch_op.create_foreign_key(
            "fk_tournament_fixtures_next_fixture",
            "tournament_fixtures",
            ["next_fixture_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint("uq_tournament_fixtures_match", ["match_id"])
        batch_op.create_index(
            "ix_tournament_fixtures_next_fixture_id", ["next_fixture_id"]
        )


def downgrade() -> None:
    """Undo the bracket columns.

    A bracket that still has an undecided slot cannot be represented by the old
    NOT NULL columns, so the alter fails rather than inventing a participant.
    Rows are otherwise preserved.
    """
    with op.batch_alter_table("tournament_fixtures", recreate="always") as batch_op:
        batch_op.drop_index("ix_tournament_fixtures_next_fixture_id")
        batch_op.drop_constraint("uq_tournament_fixtures_match", type_="unique")
        batch_op.drop_constraint(
            "fk_tournament_fixtures_next_fixture", type_="foreignkey"
        )
        batch_op.drop_column("next_slot")
        batch_op.drop_column("next_fixture_id")
        batch_op.alter_column("team_a_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("team_b_id", existing_type=sa.Integer(), nullable=False)
