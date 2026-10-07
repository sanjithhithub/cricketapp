"""add matches.player_of_match_id for the completed-match award

Revision ID: a2b3c4d5e6f7
Revises: ef1a2b3c4d5e
Create Date: 2026-10-07 09:00:00.000000

The completed-match summary shows the player of the match. It is a
human-judged award, so it is stored on the match rather than derived from the
scorecards, and it is nullable because a match can finish without one. The
player may belong to the losing side, so no check ties it to the winner.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "ef1a2b3c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.add_column(sa.Column("player_of_match_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_matches_player_of_match_id_players",
            "players",
            ["player_of_match_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_constraint("fk_matches_player_of_match_id_players", type_="foreignkey")
        batch_op.drop_column("player_of_match_id")
