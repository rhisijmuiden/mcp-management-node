"""uptime: external checks, results, incidents and the customer's uptime SLA (phase 16)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column('environments', sa.Column('uptime', J, nullable=False, server_default='{}'))
    op.create_table(
        'uptime_checks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('target', sa.String(length=500), nullable=False),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('config', J, nullable=False, server_default='{}'),
        sa.Column('probe_node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True),
        sa.Column('node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('interval_seconds', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('fail_after', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('slow_ms', sa.Integer(), nullable=True),
        sa.Column('cert_warn_days', sa.Integer(), nullable=False, server_default='14'),
        sa.Column('severity', sa.String(length=16), nullable=False, server_default='critical'),
        sa.Column('in_sla', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unknown'),
        sa.Column('status_since', TS, nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failing_since', TS, nullable=True),
        sa.Column('last_check_at', TS, nullable=True),
        sa.Column('last_ms', sa.Float(), nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('last_code', sa.Integer(), nullable=True),
        sa.Column('cert_expires_at', TS, nullable=True),
        sa.Column('created_at', TS, nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', TS, nullable=False),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'uptime_results',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('check_id', sa.String(length=32), sa.ForeignKey('uptime_checks.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('at', TS, nullable=False, index=True),
        sa.Column('ok', sa.Boolean(), nullable=True),
        sa.Column('ms', sa.Float(), nullable=True),
        sa.Column('code', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
    )
    op.create_index('ix_uptime_results_check_at', 'uptime_results', ['check_id', 'at'])
    op.create_table(
        'uptime_incidents',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('check_id', sa.String(length=32), sa.ForeignKey('uptime_checks.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('check_name', sa.String(length=120), nullable=False),
        sa.Column('started_at', TS, nullable=False, index=True),
        sa.Column('ended_at', TS, nullable=True),
        sa.Column('error', sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('uptime_incidents')
    op.drop_index('ix_uptime_results_check_at', table_name='uptime_results')
    op.drop_table('uptime_results')
    op.drop_table('uptime_checks')
    op.drop_column('environments', 'uptime')
