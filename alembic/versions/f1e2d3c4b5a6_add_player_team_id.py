"""add player team_id

Revision ID: f1e2d3c4b5a6
Revises: 4c6f99d7cffb
Create Date: 2026-08-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, Sequence[str], None] = '4c6f99d7cffb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('players', sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=True))


def downgrade() -> None:
    op.drop_column('players', 'team_id')
