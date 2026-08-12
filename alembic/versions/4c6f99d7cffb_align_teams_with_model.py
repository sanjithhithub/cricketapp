"""align teams with model

Revision ID: 4c6f99d7cffb
Revises: 7b6a94a2c2af
Create Date: 2026-08-12 11:33:40.978448

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4c6f99d7cffb'
down_revision: Union[str, Sequence[str], None] = '7b6a94a2c2af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("teams", "owners")
    op.add_column("teams", sa.Column("founder", sa.String(100), nullable=False))
    op.add_column("teams", sa.Column("owner", sa.String(200), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("teams", "owner")
    op.drop_column("teams", "founder")
    op.add_column("teams", sa.Column("owners", sa.String(500), nullable=False))
