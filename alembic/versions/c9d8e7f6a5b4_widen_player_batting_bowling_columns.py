"""widen player batting/bowling columns

The validators store full words ("Right", "Middle Order", "Medium Fast"),
but the columns were sized for single characters, causing DataError (500)
on PostgreSQL.

Revision ID: c9d8e7f6a5b4
Revises: b7c8d9e0f1a2
Create Date: 2026-08-24 10:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d8e7f6a5b4'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('players') as batch_op:
        batch_op.alter_column('batting_hand', existing_type=sa.String(1), type_=sa.String(10))
        batch_op.alter_column('batting_position', existing_type=sa.String(5), type_=sa.String(20))
        batch_op.alter_column('bowling_hand', existing_type=sa.String(1), type_=sa.String(10))
        batch_op.alter_column('bowling_type', existing_type=sa.String(10), type_=sa.String(20))


def downgrade() -> None:
    with op.batch_alter_table('players') as batch_op:
        batch_op.alter_column('bowling_type', existing_type=sa.String(20), type_=sa.String(10))
        batch_op.alter_column('bowling_hand', existing_type=sa.String(10), type_=sa.String(1))
        batch_op.alter_column('batting_position', existing_type=sa.String(20), type_=sa.String(5))
        batch_op.alter_column('batting_hand', existing_type=sa.String(10), type_=sa.String(1))
