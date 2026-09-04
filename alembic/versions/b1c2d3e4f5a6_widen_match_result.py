"""widen match result column

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-08-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('matches') as batch_op:
        batch_op.alter_column('result', existing_type=sa.String(20), type_=sa.String(500), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('matches') as batch_op:
        batch_op.alter_column('result', existing_type=sa.String(500), type_=sa.String(20), nullable=True)
