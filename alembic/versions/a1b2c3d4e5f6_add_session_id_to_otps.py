"""add session_id to otps

Revision ID: a1b2c3d4e5f6
Revises: eaf3f565a4e6
Create Date: 2026-07-04 16:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'eaf3f565a4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('otps', sa.Column('session_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('otps', 'session_id')
