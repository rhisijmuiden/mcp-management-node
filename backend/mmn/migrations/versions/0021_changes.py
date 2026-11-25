"""changes: the change calendar per customer with notices to the customer (phase 22)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column('environments', sa.Column('changes', J, nullable=False, server_default='{}'))
    op.execute("CREATE SEQUENCE change_number_seq START WITH 1001")
    op.create_table(
        'changes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('number', sa.Integer(), nullable=False, unique=True,
                  server_default=sa.text("nextval('change_number_seq')")),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('internal_notes', sa.Text(), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='maintenance'),
        sa.Column('impact', sa.String(length=16), nullable=False, server_default='minor'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='planned', index=True),
        sa.Column('starts_at', TS, nullable=False, index=True),
        sa.Column('ends_at', TS, nullable=False, index=True),
        sa.Column('node_ids', J, nullable=False, server_default='[]'),
        sa.Column('task_id', sa.String(length=32), sa.ForeignKey('scheduled_tasks.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('runbook_id', sa.String(length=32), sa.ForeignKey('runbooks.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('run_runbook', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('runbook_run_id', sa.String(length=32), nullable=True),
        sa.Column('notify', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('notify_before', J, nullable=True),
        sa.Column('notices', J, nullable=False, server_default='[]'),
        sa.Column('outcome_note', sa.Text(), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
        sa.Column('started_at', TS, nullable=True),
        sa.Column('finished_at', TS, nullable=True),
    )
    op.execute("ALTER SEQUENCE change_number_seq OWNED BY changes.number")


def downgrade() -> None:
    op.drop_table('changes')
    op.execute("DROP SEQUENCE IF EXISTS change_number_seq")
    op.drop_column('environments', 'changes')
