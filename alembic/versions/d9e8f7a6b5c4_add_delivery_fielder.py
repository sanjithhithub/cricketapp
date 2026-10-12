"""add deliveries.fielder_id for fielding attribution

Revision ID: d9e8f7a6b5c4
Revises: f4e3d2c1b0a9
Create Date: 2026-10-12 10:00:00.000000

A dismissal used to record only who was out, never who took the catch, ran the
batsman out or made the stumping. deliveries.fielder_id holds that attribution.

It is nullable and unused by the scoring engine on purpose: existing rows have
no fielder, the column is purely additive, and points/figures do not depend on
it. Fielding leaderboards count only rows where it is set.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9e8f7a6b5c4"
down_revision: Union[str, Sequence[str], None] = "f4e3d2c1b0a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.add_column(sa.Column("fielder_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_deliveries_fielder_id_players",
            "players",
            ["fielder_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.drop_constraint("fk_deliveries_fielder_id_players", type_="foreignkey")
        batch_op.drop_column("fielder_id")
