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
    op.execute("INSERT INTO team_levels (name) VALUES ('National'), ('IPL'), ('Domestic'), ('County'), ('U-19'), ('Women'), ('BBL')")

    # SQLite cannot add a column carrying a REFERENCES clause via plain
    # ALTER TABLE ADD COLUMN, and the column is NOT NULL so it cannot be added
    # straight to a table that already has rows. Add it nullable, point existing
    # rows at the first level, then tighten it to NOT NULL.
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('level_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_teams_level_id_team_levels', 'team_levels', ['level_id'], ['id']
        )

    op.execute(
        'UPDATE teams SET level_id = (SELECT MIN(id) FROM team_levels) WHERE level_id IS NULL'
    )

    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.alter_column('level_id', existing_type=sa.Integer(), nullable=False)

    # Drop the old single-team pointer before player_team_assignments exists, so
    # players is not rebuilt while a new child table already references it.
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.drop_column('team_id')

    op.create_table(
        'player_team_assignments',
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), primary_key=True),
        sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), primary_key=True),
        sa.Column('level_id', sa.Integer(), sa.ForeignKey('team_levels.id'), primary_key=True),
        sa.Column('role', sa.String(20), nullable=False, server_default='playing_11'),
    )

    # player_team_assignments supersedes the old two-column join table, which is
    # dropped here rather than left behind as dead schema on fresh databases.
    if 'team_players' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('team_players')


def downgrade() -> None:
    op.create_table(
        'team_players',
        sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), primary_key=True),
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), primary_key=True),
    )
    op.drop_table('player_team_assignments')
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_players_team_id_teams', 'teams', ['team_id'], ['id'])
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.drop_constraint('fk_teams_level_id_team_levels', type_='foreignkey')
        batch_op.drop_column('level_id')
    op.drop_table('team_levels')
