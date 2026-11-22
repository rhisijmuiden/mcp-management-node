"""customers central (phase 7): customer details, workstations, tokens per customer

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Environments become customers: same table, so every node, server, UniFi connection and
    # agent keeps working. Only new, optional columns.
    op.add_column('environments', sa.Column('contact_name', sa.String(length=200), nullable=True))
    op.add_column('environments', sa.Column('contact_email', sa.String(length=255), nullable=True))
    op.add_column('environments', sa.Column('contact_phone', sa.String(length=50), nullable=True))
    op.add_column('environments', sa.Column('contacts', J, nullable=False, server_default='[]'))
    op.add_column('environments', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('environments', sa.Column('maintenance_window', sa.String(length=200), nullable=True))
    op.add_column('environments', sa.Column('sla', sa.String(length=200), nullable=True))
    # Linux/Windows machines are a server or a workstation.
    op.add_column('nodes', sa.Column('role', sa.String(length=16), nullable=False, server_default='server'))
    op.add_column('pairing_codes', sa.Column('role', sa.String(length=16), nullable=True))
    # MCP tokens (personal and OAuth) can be tied to customers; NULL = all customers (as before).
    op.add_column('api_tokens', sa.Column('customer_ids', J, nullable=True))
    op.add_column('oauth_codes', sa.Column('customer_ids', J, nullable=True))


def downgrade() -> None:
    op.drop_column('oauth_codes', 'customer_ids')
    op.drop_column('api_tokens', 'customer_ids')
    op.drop_column('pairing_codes', 'role')
    op.drop_column('nodes', 'role')
    for c in ('sla', 'maintenance_window', 'notes', 'contacts', 'contact_phone', 'contact_email', 'contact_name'):
        op.drop_column('environments', c)
