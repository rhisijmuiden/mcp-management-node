"""m365: Microsoft 365 / Entra ID tenants per customer and the sign-ins that stood out (phase 19)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def _cust() -> sa.Column:
    return sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                     nullable=False, index=True)


def upgrade() -> None:
    op.create_table(
        'm365_tenants',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('tenant_id', sa.String(length=100), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('auth_method', sa.String(length=16), nullable=False, server_default='secret'),
        sa.Column('vault_item_id', sa.String(length=32), sa.ForeignKey('vault_items.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unknown'),
        sa.Column('last_sync_at', TS, nullable=True),
        sa.Column('last_ok_at', TS, nullable=True),
        sa.Column('last_error', sa.String(length=1000), nullable=True),
        sa.Column('signins_since', TS, nullable=True),
        sa.Column('vault_logged_at', TS, nullable=True),
        sa.Column('snapshot', J, nullable=False, server_default='{}'),
        sa.Column('settings', J, nullable=False, server_default='{}'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'm365_signins',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('tenant_id', sa.String(length=32), sa.ForeignKey('m365_tenants.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('signin_id', sa.String(length=64), nullable=False),
        sa.Column('at', TS, nullable=False, index=True),
        sa.Column('user_id', sa.String(length=64), nullable=True),
        sa.Column('upn', sa.String(length=255), nullable=True),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('app', sa.String(length=255), nullable=True),
        sa.Column('client_app', sa.String(length=100), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('country', sa.String(length=8), nullable=True),
        sa.Column('city', sa.String(length=120), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('error_code', sa.Integer(), nullable=True),
        sa.Column('failure_reason', sa.String(length=500), nullable=True),
        sa.Column('risk_level', sa.String(length=16), nullable=True),
        sa.Column('risk_state', sa.String(length=32), nullable=True),
        sa.Column('ca_status', sa.String(length=32), nullable=True),
        sa.Column('flags', J, nullable=False, server_default='[]'),
        sa.UniqueConstraint('tenant_id', 'signin_id', name='uq_m365_signin'),
    )


def downgrade() -> None:
    op.drop_table('m365_signins')
    op.drop_table('m365_tenants')
