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
    # SQLite cannot add a column carrying a REFERENCES clause through plain
    # ALTER TABLE ADD COLUMN (alembic emits it as a separate constraint and the
    # dialect raises NotImplementedError), so the table is rebuilt in batch mode.
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'team_id',
                sa.Integer(),
                sa.ForeignKey('teams.id', name='fk_players_team_id_teams'),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.drop_column('team_id')
