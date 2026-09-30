"""create matches table

Revision ID: d5f6a7b8c9d1
Revises: c9d8e7f6a5b4
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f6a7b8c9d1'
down_revision: Union[str, Sequence[str], None] = 'c9d8e7f6a5b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'matches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('match_date', sa.Date(), nullable=False),
        sa.Column('match_time', sa.String(10), nullable=False),
        sa.Column('venue', sa.String(200), nullable=False),
        sa.Column('match_type', sa.String(10), nullable=False),
        sa.Column('result', sa.String(20), nullable=False),
        sa.Column('team_a_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=False),
        sa.Column('team_b_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=False),
        sa.Column('toss_winner_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=False),
        sa.Column('toss_decision', sa.String(10), nullable=False),
        sa.Column('referee_1_name', sa.String(100), nullable=True),
        sa.Column('referee_2_name', sa.String(100), nullable=True),
        sa.Column('match_referee_name', sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('matches')
