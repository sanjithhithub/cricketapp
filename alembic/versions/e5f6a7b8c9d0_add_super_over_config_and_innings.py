"""add super over config and super over innings flag

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6
Create Date: 2026-09-24 12:00:00.000000

Super Over support:
- matches.super_over_enabled  : play a one-over-per-side decider when the main
  match is level instead of declaring it tied.
- matches.super_over_repeat   : keep playing extra Super Overs (batting order
  swapped) when a Super Over is itself level.
- innings.is_super_over       : marks the Super Over innings so the engine caps
  them at 6 legal deliveries and keeps them separate from the normal innings.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.add_column(
            sa.Column("super_over_enabled", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("super_over_repeat", sa.Boolean(), nullable=False, server_default="1")
        )

    with op.batch_alter_table("innings") as batch_op:
        batch_op.add_column(
            sa.Column("is_super_over", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("innings") as batch_op:
        batch_op.drop_column("is_super_over")

    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_column("super_over_repeat")
        batch_op.drop_column("super_over_enabled")
