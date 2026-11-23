"""AI gateway (phase 8): policies and approvals for MCP tool calls

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'ai_policies',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('scope_id', sa.String(length=64), nullable=True),
        sa.Column('risks', J, nullable=False, server_default='{}'),
        sa.Column('tools', J, nullable=False, server_default='{}'),
        sa.Column('outside_window', sa.String(length=16), nullable=False, server_default='inherit'),
        sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=64), nullable=True),
        sa.UniqueConstraint('scope', 'scope_id', name='uq_ai_policies_scope'),
    )
    op.create_table(
        'ai_approvals',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('tool', sa.String(length=100), nullable=False),
        sa.Column('risk', sa.String(length=16), nullable=False),
        sa.Column('args_enc', sa.Text(), nullable=True),
        sa.Column('args_view', J, nullable=False, server_default='{}'),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('customer_ids', J, nullable=False, server_default='[]'),
        sa.Column('customer_names', J, nullable=False, server_default='[]'),
        sa.Column('reasons', J, nullable=False, server_default='[]'),
        sa.Column('ai_reason', sa.Text(), nullable=True),
        sa.Column('token_id', sa.String(length=32), nullable=True),
        sa.Column('token_name', sa.String(length=100), nullable=True),
        sa.Column('user_id', sa.String(length=32), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('client', J, nullable=False, server_default='{}'),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.String(length=64), nullable=True),
        sa.Column('decision_note', sa.Text(), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', J, nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_ai_approvals_created_at', 'ai_approvals', ['created_at'])
    op.create_index('ix_ai_approvals_status', 'ai_approvals', ['status'])
    op.create_index('ix_ai_approvals_fingerprint', 'ai_approvals', ['fingerprint'])
    op.create_index('ix_ai_approvals_token_id', 'ai_approvals', ['token_id'])


def downgrade() -> None:
    op.drop_table('ai_approvals')
    op.drop_table('ai_policies')
