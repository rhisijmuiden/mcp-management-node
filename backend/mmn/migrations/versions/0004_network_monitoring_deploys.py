"""monitoring samples and alerts, SDN designs, config backups, GitHub deployments

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column('nodes', sa.Column('health', J, nullable=False, server_default='{}'))
    op.create_table('metric_samples',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.String(length=32), nullable=False),
    sa.Column('at', TS, nullable=False),
    sa.Column('cpu', sa.Float(), nullable=False),
    sa.Column('load1', sa.Float(), nullable=True),
    sa.Column('mem_used', sa.BigInteger(), nullable=False),
    sa.Column('mem_total', sa.BigInteger(), nullable=False),
    sa.Column('swap_used', sa.BigInteger(), nullable=False),
    sa.Column('disk_used', sa.BigInteger(), nullable=False),
    sa.Column('disk_total', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_metric_samples_node_id', 'metric_samples', ['node_id'])
    op.create_index('ix_metric_samples_at', 'metric_samples', ['at'])
    op.create_index('ix_metric_samples_node_at', 'metric_samples', ['node_id', 'at'])

    op.create_table('alerts',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('node_id', sa.String(length=32), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('detail', J, nullable=False),
    sa.Column('started_at', TS, nullable=False),
    sa.Column('resolved_at', TS, nullable=True),
    sa.Column('notified_at', TS, nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_alerts_node_id', 'alerts', ['node_id'])
    op.create_index('ix_alerts_key', 'alerts', ['key'])
    op.create_index('ix_alerts_started_at', 'alerts', ['started_at'])

    op.create_table('sdn_designs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('node_id', sa.String(length=32), nullable=False),
    sa.Column('spec', J, nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', TS, nullable=False),
    sa.Column('updated_at', TS, nullable=False),
    sa.Column('applied_at', TS, nullable=True),
    sa.Column('last_job_id', sa.String(length=32), nullable=True),
    sa.Column('last_backup_id', sa.String(length=32), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_sdn_designs_node_id', 'sdn_designs', ['node_id'])

    op.create_table('config_backups',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('node_id', sa.String(length=32), nullable=False),
    sa.Column('design_id', sa.String(length=32), nullable=True),
    sa.Column('reason', sa.String(length=300), nullable=False),
    sa.Column('files', J, nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', TS, nullable=False),
    sa.Column('restored_at', TS, nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_config_backups_node_id', 'config_backups', ['node_id'])
    op.create_index('ix_config_backups_design_id', 'config_backups', ['design_id'])
    op.create_index('ix_config_backups_created_at', 'config_backups', ['created_at'])

    op.create_table('deployments',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('repo', sa.String(length=200), nullable=False),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('branch_prod', sa.String(length=100), nullable=False),
    sa.Column('branch_test', sa.String(length=100), nullable=False),
    sa.Column('node_id', sa.String(length=32), nullable=True),
    sa.Column('vmid', sa.Integer(), nullable=False),
    sa.Column('path', sa.String(length=255), nullable=False),
    sa.Column('preset', sa.String(length=32), nullable=False),
    sa.Column('setup_cmd', sa.Text(), nullable=False),
    sa.Column('deploy_cmd', sa.Text(), nullable=False),
    sa.Column('service', sa.String(length=100), nullable=True),
    sa.Column('env_enc', sa.Text(), nullable=True),
    sa.Column('auto_deploy', sa.String(length=16), nullable=False),
    sa.Column('webhook_secret_enc', sa.Text(), nullable=True),
    sa.Column('snapshot', sa.Boolean(), nullable=False),
    sa.Column('rollback', sa.Boolean(), nullable=False),
    sa.Column('mail_to', J, nullable=False),
    sa.Column('last_sha', sa.String(length=64), nullable=True),
    sa.Column('last_status', sa.String(length=16), nullable=True),
    sa.Column('last_run_at', TS, nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', TS, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'))
    op.create_index('ix_deployments_node_id', 'deployments', ['node_id'])

    op.create_table('deployment_runs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('deployment_id', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('branch', sa.String(length=100), nullable=False),
    sa.Column('sha', sa.String(length=64), nullable=True),
    sa.Column('previous_sha', sa.String(length=64), nullable=True),
    sa.Column('requested_by', sa.String(length=64), nullable=True),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.Column('snapshot', sa.String(length=64), nullable=True),
    sa.Column('rolled_back', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', TS, nullable=False),
    sa.Column('finished_at', TS, nullable=True),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_deployment_runs_deployment_id', 'deployment_runs', ['deployment_id'])
    op.create_index('ix_deployment_runs_started_at', 'deployment_runs', ['started_at'])


def downgrade() -> None:
    op.drop_table('deployment_runs')
    op.drop_table('deployments')
    op.drop_table('config_backups')
    op.drop_table('sdn_designs')
    op.drop_table('alerts')
    op.drop_table('metric_samples')
    op.drop_column('nodes', 'health')
