"""create tournaments, tournament teams and fixtures

Revision ID: f4e3d2c1b0a9
Revises: a2b3c4d5e6f7
Create Date: 2026-10-09 10:00:00.000000

Three tables for the tournament/league feature. ``tournaments`` holds the
competition and its points rules, ``tournament_teams`` its entries (unique per
tournament), and ``tournament_fixtures`` the schedule - a fixture links to a
scored ``matches`` row through a nullable ``match_id``, so a plan exists before a
match does.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4e3d2c1b0a9"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tournaments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("level_id", sa.Integer(), nullable=True),
        sa.Column("format", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("points_win", sa.Integer(), nullable=False),
        sa.Column("points_tie", sa.Integer(), nullable=False),
        sa.Column("points_loss", sa.Integer(), nullable=False),
        sa.Column("points_no_result", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["level_id"], ["team_levels.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_tournaments_user_name"),
    )
    op.create_index("ix_tournaments_user_id", "tournaments", ["user_id"])

    op.create_table(
        "tournament_teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_id", "team_id", name="uq_tournament_team"),
    )
    op.create_index("ix_tournament_teams_tournament_id", "tournament_teams", ["tournament_id"])
    op.create_index("ix_tournament_teams_team_id", "tournament_teams", ["team_id"])

    op.create_table(
        "tournament_fixtures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("team_a_id", sa.Integer(), nullable=False),
        sa.Column("team_b_id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("scheduled_time", sa.String(length=10), nullable=True),
        sa.Column("venue", sa.String(length=200), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"]),
        sa.ForeignKeyConstraint(["team_a_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["team_b_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tournament_fixtures_tournament_id", "tournament_fixtures", ["tournament_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_tournament_fixtures_tournament_id", table_name="tournament_fixtures")
    op.drop_table("tournament_fixtures")
    op.drop_index("ix_tournament_teams_team_id", table_name="tournament_teams")
    op.drop_index("ix_tournament_teams_tournament_id", table_name="tournament_teams")
    op.drop_table("tournament_teams")
    op.drop_index("ix_tournaments_user_id", table_name="tournaments")
    op.drop_table("tournaments")
