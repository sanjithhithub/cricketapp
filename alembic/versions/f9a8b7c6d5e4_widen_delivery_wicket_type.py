"""widen deliveries.wicket_type for long dismissal names

Revision ID: f9a8b7c6d5e4
Revises: c3d4e5f6a7b8
Create Date: 2026-11-09 12:00:00.000000

The expanded dismissal model adds ``obstructing_the_field`` (21 characters),
``hit_the_ball_twice`` and ``retired_out``/``timed_out``. The column was sized
for the old nine-character names, so PostgreSQL would reject the longer values.
SQLite ignores the length, but the widen is needed everywhere else.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a8b7c6d5e4"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.alter_column(
            "wicket_type",
            existing_type=sa.String(20),
            type_=sa.String(40),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.alter_column(
            "wicket_type",
            existing_type=sa.String(40),
            type_=sa.String(20),
            existing_nullable=True,
        )
