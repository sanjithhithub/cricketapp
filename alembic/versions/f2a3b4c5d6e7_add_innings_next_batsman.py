"""add innings.next_batsman_id for manual next-batsman selection

Revision ID: f2a3b4c5d6e7
Revises: b3c4d5e6f7a8
Create Date: 2026-09-26 14:05:00.000000

Manual next-batsman selection:
The striker is never stored directly - ScoreEngine._replay() rebuilds the
crease from the batting-order positions plus the delivery log. That made a
hand-picked batsman impossible to record: inserting a row still left the replay
walking the order positionally.

innings.next_batsman_id holds a one-shot override ("this player comes in for
the next vacancy"). _replay() consumes it instead of taking the next
positional batter, advances its order pointer past the chosen player, and the
value is cleared once a delivery is bowled with them at the crease.

The batch rebuild below reflects `innings`, so it fails on any database whose
foreign keys still name a dropped table. b3c4d5e6f7a8 repairs those first and
is this migration's parent for exactly that reason.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("innings") as batch_op:
        batch_op.add_column(sa.Column("next_batsman_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_innings_next_batsman_id_players",
            "players",
            ["next_batsman_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("innings") as batch_op:
        batch_op.drop_constraint("fk_innings_next_batsman_id_players", type_="foreignkey")
        batch_op.drop_column("next_batsman_id")
