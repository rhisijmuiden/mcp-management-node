"""guests, template library, community scripts, scheduled tasks

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column('nodes', sa.Column('pve_name', sa.String(length=100), nullable=True))
    op.add_column('nodes', sa.Column('guests', J, nullable=False, server_default='{}'))
    op.add_column('nodes', sa.Column('storage_scan', J, nullable=False, server_default='{}'))
    op.create_table('ct_templates',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('family', sa.String(length=200), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=True),
    sa.Column('os', sa.String(length=32), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('size', sa.BigInteger(), nullable=True),
    sa.Column('source_url', sa.String(length=1000), nullable=True),
    sa.Column('previous_filenames', J, nullable=False),
    sa.Column('sync', sa.Boolean(), nullable=False),
    sa.Column('storage', sa.String(length=100), nullable=True),
    sa.Column('follow_latest', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ct_templates_family'), 'ct_templates', ['family'], unique=False)
    op.create_table('community_scripts',
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('path', sa.String(length=255), nullable=False),
    sa.Column('blob_sha', sa.String(length=64), nullable=True),
    sa.Column('source_url', sa.String(length=1000), nullable=True),
    sa.Column('tags', J, nullable=False),
    sa.Column('defaults', J, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('slug')
    )
    op.create_table('scheduled_tasks',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('cron', sa.String(length=100), nullable=False),
    sa.Column('targets', J, nullable=False),
    sa.Column('options', J, nullable=False),
    sa.Column('mail_to', J, nullable=False),
    sa.Column('mail_on', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=16), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_runs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=True),
    sa.Column('results', J, nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['scheduled_tasks.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_runs_task_id'), 'task_runs', ['task_id'], unique=False)
    op.create_index(op.f('ix_task_runs_started_at'), 'task_runs', ['started_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_task_runs_started_at'), table_name='task_runs')
    op.drop_index(op.f('ix_task_runs_task_id'), table_name='task_runs')
    op.drop_table('task_runs')
    op.drop_table('scheduled_tasks')
    op.drop_table('community_scripts')
    op.drop_index(op.f('ix_ct_templates_family'), table_name='ct_templates')
    op.drop_table('ct_templates')
    op.drop_column('nodes', 'storage_scan')
    op.drop_column('nodes', 'guests')
    op.drop_column('nodes', 'pve_name')
