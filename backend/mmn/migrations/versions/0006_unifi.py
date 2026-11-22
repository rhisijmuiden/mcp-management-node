"""UniFi connections and events (phase 6)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table('unifi_connections',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('environment_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('transport', sa.String(length=16), nullable=False, server_default='direct'),
    sa.Column('base_url', sa.String(length=500), nullable=True),
    sa.Column('agent_node_id', sa.String(length=32), nullable=True),
    sa.Column('cloud_connection_id', sa.String(length=32), nullable=True),
    sa.Column('console_id', sa.String(length=200), nullable=True),
    sa.Column('api_key_enc', sa.Text(), nullable=True),
    sa.Column('read_only', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('tls_mode', sa.String(length=16), nullable=False, server_default='verify'),
    sa.Column('tls_fingerprint', sa.String(length=100), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('status', sa.String(length=16), nullable=False, server_default='unknown'),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_ok_at', TS, nullable=True),
    sa.Column('last_poll_at', TS, nullable=True),
    sa.Column('info', J, nullable=False, server_default='{}'),
    sa.Column('snapshot', J, nullable=False, server_default='{}'),
    sa.Column('created_at', TS, nullable=False),
    sa.ForeignKeyConstraint(['environment_id'], ['environments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['agent_node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['cloud_connection_id'], ['unifi_connections.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'))
    op.create_index('ix_unifi_connections_environment_id', 'unifi_connections', ['environment_id'])
    op.create_table('unifi_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('connection_id', sa.String(length=32), nullable=False),
    sa.Column('at', TS, nullable=False),
    sa.Column('site_id', sa.String(length=100), nullable=True),
    sa.Column('site_name', sa.String(length=200), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False, server_default='info'),
    sa.Column('subject', sa.String(length=255), nullable=True),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('detail', J, nullable=False, server_default='{}'),
    sa.ForeignKeyConstraint(['connection_id'], ['unifi_connections.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_unifi_events_connection_id', 'unifi_events', ['connection_id'])
    op.create_index('ix_unifi_events_at', 'unifi_events', ['at'])


def downgrade() -> None:
    op.drop_index('ix_unifi_events_at', table_name='unifi_events')
    op.drop_index('ix_unifi_events_connection_id', table_name='unifi_events')
    op.drop_table('unifi_events')
    op.drop_index('ix_unifi_connections_environment_id', table_name='unifi_connections')
    op.drop_table('unifi_connections')
