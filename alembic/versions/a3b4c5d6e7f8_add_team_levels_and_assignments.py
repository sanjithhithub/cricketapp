"""add team levels and player team assignments

Revision ID: a3b4c5d6e7f8
Revises: f1e2d3c4b5a6
Create Date: 2026-08-17 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f1e2d3c4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'team_levels',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(50), unique=True, nullable=False),
    )

    op.add_column('teams', sa.Column('level_id', sa.Integer(), sa.ForeignKey('team_levels.id'), nullable=False))

    op.create_table(
        'player_team_assignments',
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), primary_key=True),
        sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), primary_key=True),
        sa.Column('level_id', sa.Integer(), sa.ForeignKey('team_levels.id'), primary_key=True),
        sa.Column('role', sa.String(20), nullable=False, server_default='playing_11'),
    )

    op.drop_column('players', 'team_id')

    op.execute("INSERT INTO team_levels (name) VALUES ('National'), ('IPL'), ('Domestic'), ('County'), ('U-19'), ('Women'), ('BBL')")


def downgrade() -> None:
    op.add_column('players', sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=True))
    op.drop_table('player_team_assignments')
    op.drop_column('teams', 'level_id')
    op.drop_table('team_levels')
