"""add matches.total_overs for a customisable over count

Revision ID: e1f2a3b4c5d6
Revises: d9e8f7a6b5c4
Create Date: 2026-10-15 10:00:00.000000

The over limit used to come only from the format (T20 20, ODI 50, Test
unlimited), with no way to run an 8-over junior game or a rain-shortened 15-over
one. matches.total_overs holds that override.

It is nullable on purpose: NULL keeps the format default, so existing matches and
Test cricket are untouched, and the column is purely additive. The scoring engine
reads it through one helper, so the scorecard, the per-bowler quota and the
innings-end rule all follow the value together.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d9e8f7a6b5c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.add_column(sa.Column("total_overs", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_column("total_overs")
