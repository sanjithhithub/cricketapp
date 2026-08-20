"""increase session_id length to 500

Revision ID: b7c8d9e0f1a2
Revises: a3b4c5d6e7f8
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('otps', schema=None) as batch_op:
        batch_op.alter_column(
            'session_id',
            existing_type=sa.String(length=100),
            type_=sa.String(length=500),
        )


def downgrade() -> None:
    with op.batch_alter_table('otps', schema=None) as batch_op:
        batch_op.alter_column(
            'session_id',
            existing_type=sa.String(length=500),
            type_=sa.String(length=100),
        )
