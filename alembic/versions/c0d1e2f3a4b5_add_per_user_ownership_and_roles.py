"""add per-user ownership and roles

Revision ID: c0d1e2f3a4b5
Revises: a0b1c2d3e4f5
Create Date: 2026-09-23 07:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _existing_indexes(insp: sa.Inspector, table: str) -> set[str]:
    return {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "role" not in _existing_columns(insp, "users"):
        op.add_column(
            "users",
            sa.Column("role", sa.String(20), server_default="admin", nullable=False),
        )

    tables_with_user_id = (("teams", "ix_teams_user_id"), ("players", "ix_players_user_id"), ("matches", "ix_matches_user_id"))
    for table, index_name in tables_with_user_id:
        if "user_id" not in _existing_columns(insp, table):
            op.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if index_name not in _existing_indexes(insp, table):
            op.create_index(index_name, table, ["user_id"])

    first_user = "(SELECT id FROM users ORDER BY id ASC LIMIT 1)"
    op.execute(f"UPDATE teams SET user_id = {first_user} WHERE user_id IS NULL")
    op.execute(f"UPDATE players SET user_id = {first_user} WHERE user_id IS NULL")
    op.execute(f"UPDATE matches SET user_id = {first_user} WHERE user_id IS NULL")


def downgrade() -> None:
    op.drop_index("ix_matches_user_id", table_name="matches")
    op.drop_index("ix_players_user_id", table_name="players")
    op.drop_index("ix_teams_user_id", table_name="teams")
    op.drop_column("matches", "user_id")
    op.drop_column("players", "user_id")
    op.drop_column("teams", "user_id")
    op.drop_column("users", "role")
