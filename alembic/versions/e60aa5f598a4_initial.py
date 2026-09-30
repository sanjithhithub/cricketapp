"""initial

Revision ID: e60aa5f598a4
Revises:
Create Date: 2026-06-26 18:30:43.092415

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e60aa5f598a4'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'countries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), unique=True, nullable=False),
    )
    op.create_table(
        'states',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('country_id', sa.Integer(), sa.ForeignKey('countries.id'), nullable=False),
    )
    op.create_table(
        'cities',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('state_id', sa.Integer(), sa.ForeignKey('states.id'), nullable=False),
    )
    op.create_table(
        'players',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('first_name', sa.String(50), nullable=False),
        sa.Column('last_name', sa.String(50), nullable=False),
        sa.Column('date_of_birth', sa.Date(), nullable=False),
        sa.Column('gender', sa.String(10), nullable=False),
        sa.Column('profile_image', sa.String(500), nullable=True),
        sa.Column('batting_hand', sa.String(1), nullable=False),
        sa.Column('batting_position', sa.String(5), nullable=False),
        sa.Column('bowling_hand', sa.String(1), nullable=False),
        sa.Column('bowling_type', sa.String(10), nullable=False),
        sa.Column('country_id', sa.Integer(), sa.ForeignKey('countries.id'), nullable=False),
        sa.Column('state_id', sa.Integer(), sa.ForeignKey('states.id'), nullable=False),
        sa.Column('city_id', sa.Integer(), sa.ForeignKey('cities.id'), nullable=False),
        sa.Column('height', sa.Float(), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('country_code', sa.String(5), nullable=False),
        sa.Column('mobile_number', sa.BigInteger(), nullable=False),
        sa.Column('email', sa.String(100), unique=True, nullable=False),
    )


def downgrade() -> None:
    op.drop_table('players')
    op.drop_table('cities')
    op.drop_table('states')
    op.drop_table('countries')
