"""costs: price list, cost items (licences, contracts, hardware), daily usage and final statements (phase 21)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def _cust(nullable: bool = False) -> sa.Column:
    return sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                     nullable=nullable, index=True)


def upgrade() -> None:
    op.add_column('environments', sa.Column('costs', J, nullable=False, server_default='{}'))
    op.create_table(
        'cost_rates',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(nullable=True),
        sa.Column('key', sa.String(length=120), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('unit', sa.String(length=32), nullable=True),
        sa.Column('price', sa.Numeric(12, 4), nullable=False),
        sa.Column('cost', sa.Numeric(12, 4), nullable=True),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    # One default price and one price per customer for every key (NULLs are distinct in a unique constraint).
    op.create_index('uq_cost_rate_default', 'cost_rates', ['key'], unique=True,
                    postgresql_where=sa.text('customer_id IS NULL'))
    op.create_index('uq_cost_rate_customer', 'cost_rates', ['customer_id', 'key'], unique=True,
                    postgresql_where=sa.text('customer_id IS NOT NULL'))
    op.create_table(
        'cost_items',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='licence'),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('vendor', sa.String(length=200), nullable=True),
        sa.Column('reference', sa.String(length=200), nullable=True),
        sa.Column('serial', sa.String(length=120), nullable=True),
        sa.Column('node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True,
                  index=True),
        sa.Column('quantity', sa.Numeric(12, 2), nullable=False, server_default='1'),
        sa.Column('unit_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('cost_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('billing', sa.String(length=12), nullable=False, server_default='monthly'),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True, index=True),
        sa.Column('auto_renew', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('renew_months', sa.Integer(), nullable=True),
        sa.Column('notice_days', sa.Integer(), nullable=True),
        sa.Column('warn_days', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('history', J, nullable=False, server_default='[]'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'cost_usage',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('day', sa.Date(), nullable=False, index=True),
        sa.Column('cpu', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ram_gb', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('storage_gb', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('guests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('devices', J, nullable=False, server_default='{}'),
        sa.Column('m365_users', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('m365', J, nullable=False, server_default='{}'),
        sa.Column('detail', J, nullable=False, server_default='{}'),
        sa.Column('taken_at', TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('customer_id', 'day', name='uq_cost_usage_day'),
    )
    op.create_table(
        'cost_statements',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('period', sa.String(length=7), nullable=False),
        sa.Column('data', J, nullable=False, server_default='{}'),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.UniqueConstraint('customer_id', 'period', name='uq_cost_statement'),
    )


def downgrade() -> None:
    op.drop_table('cost_statements')
    op.drop_table('cost_usage')
    op.drop_table('cost_items')
    op.drop_index('uq_cost_rate_customer', table_name='cost_rates')
    op.drop_index('uq_cost_rate_default', table_name='cost_rates')
    op.drop_table('cost_rates')
    op.drop_column('environments', 'costs')
