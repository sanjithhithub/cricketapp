"""merge heads

Revision ID: 7b6a94a2c2af
Revises: 2967c7871772, a1b2c3d4e5f6
Create Date: 2026-08-12 11:33:31.638933

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '7b6a94a2c2af'
down_revision: Union[str, Sequence[str], None] = ('2967c7871772', 'a1b2c3d4e5f6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
