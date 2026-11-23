"""backup management per customer (phase 11)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Backup policy per customer: SLA (max age), alerts, extra recipients, excluded items.
    op.add_column('environments', sa.Column('backup', J, nullable=False, server_default='{}'))
    # The latest backup coverage of a customer, collected by the scheduler (or on demand).
    op.create_table(
        'backup_coverage',
        sa.Column('environment_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data', J, nullable=False, server_default='{}'),
        sa.Column('error', sa.Text(), nullable=True),
    )
    # New scope "restore_guests" (restore VMs, containers and files from Proxmox backups): give it to
    # tokens that may already create and delete guests. The AI gateway still asks approval for restores.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""UPDATE api_tokens SET scopes = scopes::jsonb || '["restore_guests"]'::jsonb
                      WHERE scopes::jsonb @> '["guests_admin"]'::jsonb
                        AND NOT scopes::jsonb @> '["restore_guests"]'::jsonb""")


def downgrade() -> None:
    op.drop_table('backup_coverage')
    op.drop_column('environments', 'backup')
