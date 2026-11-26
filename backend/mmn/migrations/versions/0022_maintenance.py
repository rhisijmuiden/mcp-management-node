"""maintenance: orchestrated Proxmox maintenance runs with a timeline and a customer report; where a change
came from (origin, requested at) in the change calendar (v0.24.0)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column('task_runs', sa.Column('timeline', J, nullable=False, server_default='[]'))
    op.add_column('task_runs', sa.Column('customer_ids', J, nullable=False, server_default='[]'))
    op.add_column('task_runs', sa.Column('reports', J, nullable=False, server_default='[]'))
    op.add_column('task_runs', sa.Column('approval_id', sa.String(length=32), nullable=True))

    op.add_column('changes', sa.Column('origin', sa.String(length=16), nullable=False, server_default='manual'))
    op.add_column('changes', sa.Column('origin_ref', sa.String(length=300), nullable=True))
    op.add_column('changes', sa.Column('ticket_id', sa.String(length=32),
                                       sa.ForeignKey('tickets.id', ondelete='SET NULL'), nullable=True))
    op.add_column('changes', sa.Column('requested_at', TS, nullable=True))
    op.add_column('changes', sa.Column('requested_by', sa.String(length=100), nullable=True))
    op.add_column('changes', sa.Column('task_run_id', sa.String(length=32), nullable=True))
    op.add_column('changes', sa.Column('automatic', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE changes SET requested_at = created_at, requested_by = created_by")
    op.create_index('ix_changes_task_run_id', 'changes', ['task_run_id'])


def downgrade() -> None:
    op.drop_index('ix_changes_task_run_id', table_name='changes')
    for c in ('automatic', 'task_run_id', 'requested_by', 'requested_at', 'ticket_id', 'origin_ref', 'origin'):
        op.drop_column('changes', c)
    for c in ('approval_id', 'reports', 'customer_ids', 'timeline'):
        op.drop_column('task_runs', c)
