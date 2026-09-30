"""add captain and vice captain to player team assignments

Revision ID: c4d5e6f7a8b9
Revises: b7d3e1a9c204
Create Date: 2026-09-29 10:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b7d3e1a9c204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two booleans rather than new `role` values: `role` decides which bucket a
    # player lands in (playing XI / substitute / bench) and how many may be in the
    # XI, so overloading it with a captaincy would make "captain" and "plays in
    # the XI" fight over the same column. One row can be captain, vice captain,
    # or neither, and the two are independent of the role bucket.
    with op.batch_alter_table('player_team_assignments', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_captain', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column('is_vice_captain', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('player_team_assignments', schema=None) as batch_op:
        batch_op.drop_column('is_vice_captain')
        batch_op.drop_column('is_captain')
