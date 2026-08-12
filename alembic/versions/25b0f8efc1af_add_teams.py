"""add teams

Revision ID: 25b0f8efc1af
Revises: e60aa5f598a4
Create Date: 2026-06-27 16:28:17.412630

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '25b0f8efc1af'
down_revision: Union[str, Sequence[str], None] = 'e60aa5f598a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'teams',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), unique=True, nullable=False),
        sa.Column('short_name', sa.String(10), nullable=False),
        sa.Column('logo', sa.String(500), nullable=True),
        sa.Column('country_id', sa.Integer(), sa.ForeignKey('countries.id'), nullable=False),
        sa.Column('state_id', sa.Integer(), sa.ForeignKey('states.id'), nullable=False),
        sa.Column('city_id', sa.Integer(), sa.ForeignKey('cities.id'), nullable=False),
        sa.Column('homeground', sa.String(200), nullable=False),
        sa.Column('founded_year', sa.Integer(), nullable=False),
        sa.Column('owners', sa.String(500), nullable=False),
    )
    op.create_table(
        'team_players',
        sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), primary_key=True),
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table('team_players')
    op.drop_table('teams')
