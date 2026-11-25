"""tickets: customer portal users and sessions, tickets and their messages (phase 17)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'portal_users',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('email', sa.String(length=255), nullable=False, unique=True, index=True),
        sa.Column('full_name', sa.String(length=128), nullable=True),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=16), nullable=False, server_default='member'),
        sa.Column('language', sa.String(length=8), nullable=False, server_default='en'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('notify_all', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('invited_at', TS, nullable=True),
        sa.Column('last_login_at', TS, nullable=True),
    )
    op.create_table(
        'portal_sessions',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('portal_user_id', sa.String(length=32), sa.ForeignKey('portal_users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', TS, nullable=False),
        sa.Column('remember', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
    )
    op.create_table(
        'portal_tokens',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('portal_user_id', sa.String(length=32), sa.ForeignKey('portal_users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('expires_at', TS, nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("CREATE SEQUENCE ticket_number_seq START WITH 1001")
    op.create_table(
        'tickets',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('number', sa.Integer(), nullable=False, unique=True,
                  server_default=sa.text("nextval('ticket_number_seq')")),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False, server_default='problem'),
        sa.Column('priority', sa.String(length=16), nullable=False, server_default='normal'),
        sa.Column('status', sa.String(length=24), nullable=False, server_default='new', index=True),
        sa.Column('node_ids', J, nullable=False, server_default='[]'),
        sa.Column('reporter_id', sa.String(length=32), sa.ForeignKey('portal_users.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('reporter_name', sa.String(length=128), nullable=True),
        sa.Column('reporter_email', sa.String(length=255), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('assignee_id', sa.String(length=32), sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('diagnosis', J, nullable=False, server_default='{}'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now(), index=True),
        sa.Column('updated_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('last_customer_at', TS, nullable=True),
        sa.Column('last_staff_at', TS, nullable=True),
        sa.Column('first_response_at', TS, nullable=True),
        sa.Column('resolved_at', TS, nullable=True),
        sa.Column('closed_at', TS, nullable=True),
    )
    op.execute("ALTER SEQUENCE ticket_number_seq OWNED BY tickets.number")
    op.create_table(
        'ticket_messages',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('ticket_id', sa.String(length=32), sa.ForeignKey('tickets.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('author_kind', sa.String(length=16), nullable=False),
        sa.Column('author_name', sa.String(length=128), nullable=True),
        sa.Column('portal_user_id', sa.String(length=32), sa.ForeignKey('portal_users.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('user_id', sa.String(length=32), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('internal', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('event', J, nullable=False, server_default='{}'),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table('ticket_messages')
    op.drop_table('tickets')
    op.execute("DROP SEQUENCE IF EXISTS ticket_number_seq")
    op.drop_table('portal_tokens')
    op.drop_table('portal_sessions')
    op.drop_table('portal_users')
