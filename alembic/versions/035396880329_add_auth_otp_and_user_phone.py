"""add auth otp table and user phone

Revision ID: 035396880329
Revises: b1c2d3e4f5a6
Create Date: 2026-09-04 11:03:51.612839

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '035396880329'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # auth_otps table may have been created by app start (Base.metadata.create_all).
    # Ensure it exists with all required columns.
    if not inspector.has_table('auth_otps'):
        op.create_table(
            'auth_otps',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('purpose', sa.String(30), nullable=False),
            sa.Column('email', sa.String(255), nullable=True),
            sa.Column('country_code', sa.String(5), nullable=True),
            sa.Column('mobile_number', sa.BigInteger(), nullable=True),
            sa.Column('otp_hash', sa.String(255), nullable=False),
            sa.Column('session_id', sa.String(500), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('is_verified', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )

    auth_cols = [c['name'] for c in inspector.get_columns('auth_otps')]
    if 'session_id' not in auth_cols:
        with op.batch_alter_table('auth_otps', schema=None) as batch_op:
            batch_op.add_column(sa.Column('session_id', sa.String(500), nullable=True))

    user_cols = [c['name'] for c in inspector.get_columns('users')]
    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'country_code' not in user_cols:
            batch_op.add_column(sa.Column('country_code', sa.String(length=5), nullable=True))
        if 'mobile_number' not in user_cols:
            batch_op.add_column(sa.Column('mobile_number', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    user_cols = [c['name'] for c in inspector.get_columns('users')]
    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'mobile_number' in user_cols:
            batch_op.drop_column('mobile_number')
        if 'country_code' in user_cols:
            batch_op.drop_column('country_code')

    if inspector.has_table('auth_otps'):
        op.drop_table('auth_otps')
