"""runbooks: fixed procedures run step by step with approvals (phase 13)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'runbooks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=32), nullable=False, server_default='other'),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('params', J, nullable=False, server_default='[]'),
        sa.Column('steps', J, nullable=False, server_default='[]'),
        sa.Column('use_window', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('triggers', J, nullable=False, server_default='[]'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('template', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'runbook_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('runbook_id', sa.String(length=32), sa.ForeignKey('runbooks.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('runbook_name', sa.String(length=120), nullable=False),
        sa.Column('runbook_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('definition', J, nullable=False, server_default='{}'),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('customer_name', sa.String(length=120), nullable=True),
        sa.Column('params', J, nullable=False, server_default='{}'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='running', index=True),
        sa.Column('current_step', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('state', J, nullable=False, server_default='[]'),
        sa.Column('trigger', sa.String(length=16), nullable=False, server_default='ui'),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('context', J, nullable=False, server_default='{}'),
        sa.Column('user_id', sa.String(length=32), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('token_id', sa.String(length=32), nullable=True, index=True),
        sa.Column('token_name', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('runbook_runs')
    op.drop_table('runbooks')
