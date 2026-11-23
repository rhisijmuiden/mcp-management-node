"""workstations and monthly customer reports (phase 10)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column('nodes', sa.Column('inventory', J, nullable=False, server_default='{}'))
    op.add_column('nodes', sa.Column('inventory_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('nodes', sa.Column('inventory_error', sa.Text(), nullable=True))
    op.add_column('environments', sa.Column('report', J, nullable=False, server_default='{}'))
    op.create_table(
        'customer_reports',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('environment_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('period', sa.String(length=7), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lang', sa.String(length=5), nullable=False, server_default='en'),
        sa.Column('trigger', sa.String(length=16), nullable=False, server_default='manual'),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data', J, nullable=False, server_default='{}'),
        sa.Column('html', sa.Text(), nullable=False, server_default=''),
        sa.Column('pdf', sa.LargeBinary(), nullable=True),
        sa.Column('sent_to', J, nullable=False, server_default='[]'),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('send_error', sa.Text(), nullable=True),
    )
    op.create_index('ix_customer_reports_environment_id', 'customer_reports', ['environment_id'])
    op.create_index('ix_customer_reports_created_at', 'customer_reports', ['created_at'])
    # New scopes: give them to tokens that can already do more than that. "workstations" (Defender
    # scan, gpupdate, message, log off) is a subset of "exec"; "reports" (mail a report) sits next to
    # "settings" (send test mail). The AI gateway policies still apply to every call.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""UPDATE api_tokens SET scopes = scopes::jsonb || '["workstations"]'::jsonb
                      WHERE scopes::jsonb @> '["exec"]'::jsonb AND NOT scopes::jsonb @> '["workstations"]'::jsonb""")
        op.execute("""UPDATE api_tokens SET scopes = scopes::jsonb || '["reports"]'::jsonb
                      WHERE scopes::jsonb @> '["settings"]'::jsonb AND NOT scopes::jsonb @> '["reports"]'::jsonb""")


def downgrade() -> None:
    op.drop_table('customer_reports')
    op.drop_column('environments', 'report')
    op.drop_column('nodes', 'inventory_error')
    op.drop_column('nodes', 'inventory_at')
    op.drop_column('nodes', 'inventory')
