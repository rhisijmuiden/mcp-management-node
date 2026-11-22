"""data copies between servers (phase 5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table('transfers',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('source_node_id', sa.String(length=32), nullable=True),
    sa.Column('target_node_id', sa.String(length=32), nullable=True),
    sa.Column('source_name', sa.String(length=100), nullable=False),
    sa.Column('target_name', sa.String(length=100), nullable=False),
    sa.Column('source_paths', J, nullable=False, server_default='[]'),
    sa.Column('target_path', sa.String(length=1000), nullable=False),
    sa.Column('overwrite', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('status', sa.String(length=16), nullable=False, server_default='running'),
    sa.Column('stage', sa.String(length=16), nullable=False, server_default='send'),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False, server_default='ui'),
    sa.Column('requested_by', sa.String(length=64), nullable=True),
    sa.Column('send_job_id', sa.String(length=32), nullable=True),
    sa.Column('receive_job_id', sa.String(length=32), nullable=True),
    sa.Column('bytes', sa.BigInteger(), nullable=True),
    sa.Column('wire_bytes', sa.BigInteger(), nullable=True),
    sa.Column('files', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', TS, nullable=False),
    sa.Column('finished_at', TS, nullable=True),
    sa.ForeignKeyConstraint(['source_node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['target_node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_transfers_started_at', 'transfers', ['started_at'])


def downgrade() -> None:
    op.drop_index('ix_transfers_started_at', table_name='transfers')
    op.drop_table('transfers')
