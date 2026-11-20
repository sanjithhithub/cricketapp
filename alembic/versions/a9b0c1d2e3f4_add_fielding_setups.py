"""add fielding setups and assignments

Revision ID: a9b0c1d2e3f4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-29 12:00:00.000000

Match-level fielding: which side fields in an innings, who keeps wicket, and
where every fielder stands.

Two tables rather than columns on ``innings`` because the scoring engine must
never read any of this - a bug in the field panel cannot be allowed to touch a
ball, a run or a wicket, and nothing here references ``deliveries``.

``fielding_setups`` is unique on (match_id, innings_number), which is what
makes "change the keeper" an UPDATE of one row instead of a second claim on
the role. ``fielding_assignments`` carries the two rules the API enforces -
one position per fielder, one fielder per position - as database constraints,
so a race or a direct write cannot violate them either.

Both tables are new and purely additive; no existing table is altered, so
existing matches, players, scores and results are untouched. Both foreign keys
to ``players`` are nullable-free except the keeper column, which is nullable
because a setup may be saved before a keeper is named.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fielding_setups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("innings_number", sa.Integer(), nullable=False),
        sa.Column("fielding_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column(
            "wicketkeeper_player_id",
            sa.Integer(),
            sa.ForeignKey("players.id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "match_id", "innings_number", name="uq_fielding_setup_match_innings"
        ),
    )
    op.create_index(
        "ix_fielding_setups_match_id", "fielding_setups", ["match_id"], unique=False
    )

    op.create_table(
        "fielding_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "setup_id",
            sa.Integer(),
            sa.ForeignKey("fielding_setups.id"),
            nullable=False,
        ),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("position_key", sa.String(length=40), nullable=False),
        sa.Column("anchor_x", sa.Float(), nullable=False),
        sa.Column("anchor_y", sa.Float(), nullable=False),
        sa.UniqueConstraint("setup_id", "player_id", name="uq_fielding_assignment_setup_player"),
        sa.UniqueConstraint(
            "setup_id", "position_key", name="uq_fielding_assignment_setup_position"
        ),
    )
    op.create_index(
        "ix_fielding_assignments_setup_id", "fielding_assignments", ["setup_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_fielding_assignments_setup_id", table_name="fielding_assignments")
    op.drop_table("fielding_assignments")
    op.drop_index("ix_fielding_setups_match_id", table_name="fielding_setups")
    op.drop_table("fielding_setups")
