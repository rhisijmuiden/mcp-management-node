"""docs and vault: customer documentation with revisions and attachments, the password vault with its
history, AI grants and access log (phase 18)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def _cust(nullable: bool = False, ondelete: str = 'CASCADE') -> sa.Column:
    return sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete=ondelete),
                     nullable=nullable, index=True)


def upgrade() -> None:
    op.add_column('user_sessions', sa.Column('vault_unlocked_until', TS, nullable=True))
    op.create_table(
        'documents',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('category', sa.String(length=24), nullable=False, server_default='notes'),
        sa.Column('body', sa.Text(), nullable=False, server_default=''),
        sa.Column('tags', J, nullable=False, server_default='[]'),
        sa.Column('node_ids', J, nullable=False, server_default='[]'),
        sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ai_visible', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now(), index=True),
        sa.Column('created_by', sa.String(length=128), nullable=True),
        sa.Column('updated_by', sa.String(length=128), nullable=True),
    )
    op.create_table(
        'document_revisions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('document_id', sa.String(length=32), sa.ForeignKey('documents.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=128), nullable=True),
    )
    op.create_table(
        'document_attachments',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('document_id', sa.String(length=32), sa.ForeignKey('documents.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False, server_default='application/octet-stream'),
        sa.Column('size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=128), nullable=True),
    )
    op.create_table(
        'vault_items',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False, server_default='login'),
        sa.Column('url', sa.String(length=500), nullable=True),
        sa.Column('tags', J, nullable=False, server_default='[]'),
        sa.Column('node_ids', J, nullable=False, server_default='[]'),
        sa.Column('ai_access', sa.String(length=16), nullable=False, server_default='approval'),
        sa.Column('rotate_days', sa.Integer(), nullable=True),
        sa.Column('key_id', sa.String(length=16), nullable=False),
        sa.Column('wrapped_key', sa.LargeBinary(), nullable=False),
        sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('secret_changed_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('last_used_at', TS, nullable=True),
        sa.Column('created_by', sa.String(length=128), nullable=True),
        sa.Column('updated_by', sa.String(length=128), nullable=True),
    )
    op.create_table(
        'vault_item_history',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('item_id', sa.String(length=32), sa.ForeignKey('vault_items.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('key_id', sa.String(length=16), nullable=False),
        sa.Column('wrapped_key', sa.LargeBinary(), nullable=False),
        sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=128), nullable=True),
    )
    op.create_table(
        'vault_grants',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('item_id', sa.String(length=32), sa.ForeignKey('vault_items.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('token_id', sa.String(length=32), sa.ForeignKey('api_tokens.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('approval_id', sa.String(length=32), nullable=True),
        sa.Column('approved_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', TS, nullable=False),
        sa.Column('uses_left', sa.Integer(), nullable=False, server_default='1'),
    )
    op.create_table(
        'vault_access',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('at', TS, nullable=False, server_default=sa.func.now(), index=True),
        sa.Column('item_id', sa.String(length=32), sa.ForeignKey('vault_items.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('item_name', sa.String(length=200), nullable=True),
        _cust(nullable=True),
        sa.Column('action', sa.String(length=24), nullable=False),
        sa.Column('actor_kind', sa.String(length=8), nullable=False, server_default='user'),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('token_id', sa.String(length=32), nullable=True),
        sa.Column('token_name', sa.String(length=100), nullable=True),
        sa.Column('approval_id', sa.String(length=32), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('detail', J, nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_table('vault_access')
    op.drop_table('vault_grants')
    op.drop_table('vault_item_history')
    op.drop_table('vault_items')
    op.drop_table('document_attachments')
    op.drop_table('document_revisions')
    op.drop_table('documents')
    op.drop_column('user_sessions', 'vault_unlocked_until')
