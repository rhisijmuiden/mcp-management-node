"""nightly AI checks and the morning summary (phase 12)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Nightly checks per customer: on/off and extra TLS endpoints (host:port) whose certificate is checked.
    op.add_column('environments', sa.Column('nightly', J, nullable=False, server_default='{}'))
    # An approved call that waits for the customer's maintenance window before it runs.
    op.add_column('ai_approvals', sa.Column('run_after', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_ai_approvals_run_after', 'ai_approvals', ['run_after'])
    op.create_table(
        'nightly_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='running'),
        sa.Column('trigger', sa.String(length=16), nullable=False, server_default='schedule'),
        sa.Column('requested_by', sa.String(length=64), nullable=True),
        sa.Column('customer_ids', J, nullable=True),
        sa.Column('summary', J, nullable=False, server_default='{}'),
        sa.Column('summary_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_table(
        'nightly_findings',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('run_id', sa.String(length=32), sa.ForeignKey('nightly_runs.id', ondelete='CASCADE'), index=True,
                  nullable=False),
        sa.Column('environment_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  index=True, nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False, index=True),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('area', sa.String(length=32), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('code', sa.String(length=64), nullable=True),
        sa.Column('params', J, nullable=False, server_default='{}'),
        sa.Column('finding', sa.Text(), nullable=False),
        sa.Column('suggestion', sa.Text(), nullable=False),
        sa.Column('next_tool', sa.String(length=100), nullable=True),
        sa.Column('is_new', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('proposal', J, nullable=True),
        sa.Column('approval_id', sa.String(length=32), nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_table('nightly_findings')
    op.drop_table('nightly_runs')
    op.drop_index('ix_ai_approvals_run_after', table_name='ai_approvals')
    op.drop_column('ai_approvals', 'run_after')
    op.drop_column('environments', 'nightly')
