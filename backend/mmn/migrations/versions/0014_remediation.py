"""remediation: automatic recovery rules and attempts (phase 14)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'remediation_rules',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('trigger', sa.String(length=16), nullable=False, server_default='alert'),
        sa.Column('match', J, nullable=False, server_default='{}'),
        sa.Column('runbook_id', sa.String(length=32), sa.ForeignKey('runbooks.id', ondelete='SET NULL'), nullable=True),
        sa.Column('params', J, nullable=False, server_default='{}'),
        sa.Column('mode', sa.String(length=16), nullable=False, server_default='auto'),
        sa.Column('delay_minutes', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('cooldown_minutes', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('preset', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'remediation_attempts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('rule_id', sa.String(length=32), sa.ForeignKey('remediation_rules.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('rule_name', sa.String(length=120), nullable=False),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('customer_name', sa.String(length=120), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('source_key', sa.String(length=200), nullable=False, index=True),
        sa.Column('source_id', sa.String(length=32), nullable=True, index=True),
        sa.Column('node_id', sa.String(length=32), nullable=True),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('problem', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='running', index=True),
        sa.Column('reason', J, nullable=False, server_default='{}'),
        sa.Column('run_id', sa.String(length=32), sa.ForeignKey('runbook_runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('runbook_name', sa.String(length=120), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('remediation_attempts')
    op.drop_table('remediation_rules')
