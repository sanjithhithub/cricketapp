"""add refresh_tokens

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
Create Date: 2026-10-06 09:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One row per issued refresh token, rather than a single "last issued" column
    # on users. That is what makes rotation and per-device logout expressible:
    # rotating revokes one row and points it at its successor, and logging out of
    # one device revokes one row without touching the others.
    #
    # `token_hash` holds SHA-256 of the token, never the token: the raw value is
    # returned to the client once and is not recoverable from this table.
    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('replaced_by_id', sa.Integer(), nullable=True),
        # NOT NULL, matching the mapped column: a token row with no creation time
        # cannot be reasoned about when auditing, and the Python-side default is
        # applied by the ORM rather than by the database, so nothing writes a
        # missing value in practice.
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['replaced_by_id'],
            ['refresh_tokens.id'],
            name=op.f('fk_refresh_tokens_replaced_by_id_refresh_tokens'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # token_hash is unique (and indexed) so a lookup on refresh is a single-row
    # read; user_id is indexed because revocation-by-user queries the other
    # direction.
    op.create_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens', ['token_hash'], unique=True)
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_index(op.f('ix_refresh_tokens_token_hash'), table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
