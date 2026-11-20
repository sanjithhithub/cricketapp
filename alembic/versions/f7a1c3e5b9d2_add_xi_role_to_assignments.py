"""add xi_role to player team assignments

Revision ID: f7a1c3e5b9d2
Revises: a9b0c1d2e3f4
Create Date: 2026-10-30 10:00:00.000000

What a picked player does inside the playing XI: "wicketkeeper", "bowler"
or "fielder" (1 + 1 + 9 = 11). Kept off the ``role`` column for the same
reason captaincy was: ``role`` decides which bucket a player sits in and
how many may be in the XI, so a third meaning would fight it for the same
column.

Nullable with no server default, so it is purely additive: every existing
row reads ``NULL`` ("no field job recorded"), which is honest for squads
that were selected before this column existed, and the XI picker fills it
in from then on. No existing value is rewritten and no other table is
touched, so matches, scores and results are unaffected.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f7a1c3e5b9d2"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("player_team_assignments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("xi_role", sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("player_team_assignments", schema=None) as batch_op:
        batch_op.drop_column("xi_role")
