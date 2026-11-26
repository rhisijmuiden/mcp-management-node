"""UniFi Fabric: Site Manager keys made inside a Fabric, consoles linked automatically (v0.29.0)

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('unifi_connections', sa.Column('fabric', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('unifi_connections', sa.Column('auto_link', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('unifi_connections', 'auto_link')
    op.drop_column('unifi_connections', 'fabric')
