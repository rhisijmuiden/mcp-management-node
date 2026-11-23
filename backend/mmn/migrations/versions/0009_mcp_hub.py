"""MCP hub (phase 9): other MCP servers whose tools Stackpilot passes through

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'mcp_upstreams',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('namespace', sa.String(length=32), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('environment_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=True),
        sa.Column('transport', sa.String(length=16), nullable=False, server_default='http'),
        sa.Column('url', sa.Text(), nullable=True),
        sa.Column('agent_node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('tls_mode', sa.String(length=16), nullable=False, server_default='verify'),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('command', sa.Text(), nullable=True),
        sa.Column('args', J, nullable=False, server_default='[]'),
        sa.Column('auth_kind', sa.String(length=16), nullable=False, server_default='none'),
        sa.Column('secret_enc', sa.Text(), nullable=True),
        sa.Column('config', J, nullable=False, server_default='{}'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unknown'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('server_info', J, nullable=False, server_default='{}'),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('tools', J, nullable=False, server_default='[]'),
        sa.Column('tool_settings', J, nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
    )
    op.create_index('ix_mcp_upstreams_environment_id', 'mcp_upstreams', ['environment_id'])


def downgrade() -> None:
    op.drop_table('mcp_upstreams')
