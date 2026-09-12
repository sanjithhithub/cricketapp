"""create scoring tables

Revision ID: a0b1c2d3e4f5
Revises: 035396880329
Create Date: 2026-09-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a0b1c2d3e4f5'
down_revision: Union[str, Sequence[str], None] = '035396880329'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('matches') as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(20), nullable=False, server_default='scheduled'))
        batch_op.add_column(sa.Column('current_innings_number', sa.Integer(), nullable=False, server_default='0'))

    op.create_table(
        'innings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('match_id', sa.Integer(), sa.ForeignKey('matches.id'), nullable=False),
        sa.Column('batting_team_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=False),
        sa.Column('bowling_team_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=False),
        sa.Column('innings_number', sa.Integer(), nullable=False),
        sa.Column('target', sa.Integer(), nullable=True),
    )
    op.create_index('ix_innings_match_id', 'innings', ['match_id'])

    op.create_table(
        'deliveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('innings_id', sa.Integer(), sa.ForeignKey('innings.id'), nullable=False),
        sa.Column('over_number', sa.Integer(), nullable=False),
        sa.Column('ball_number', sa.Integer(), nullable=False),
        sa.Column('striker_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=False),
        sa.Column('non_striker_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=False),
        sa.Column('bowler_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=False),
        sa.Column('runs_batsman', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('runs_extras', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('extra_type', sa.String(20), nullable=False, server_default='none'),
        sa.Column('wicket_type', sa.String(20), nullable=True),
        sa.Column('dismissed_player_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=True),
    )
    op.create_index('ix_deliveries_innings_id', 'deliveries', ['innings_id'])

    op.create_table(
        'innings_batsmen',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('innings_id', sa.Integer(), sa.ForeignKey('innings.id'), nullable=False),
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.UniqueConstraint('innings_id', 'position', name='uq_innings_batsman_position'),
    )


def downgrade() -> None:
    op.drop_table('innings_batsmen')
    op.drop_index('ix_deliveries_innings_id', table_name='deliveries')
    op.drop_table('deliveries')
    op.drop_index('ix_innings_match_id', table_name='innings')
    op.drop_table('innings')
    with op.batch_alter_table('matches') as batch_op:
        batch_op.drop_column('current_innings_number')
        batch_op.drop_column('status')
