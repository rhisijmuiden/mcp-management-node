"""software library: several GitHub tokens, repositories with rights per customer, deployments per customer and
onto servers (v0.27.0)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'github_accounts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('token_enc', sa.Text(), nullable=False),
        sa.Column('login', sa.String(length=100), nullable=True),
        sa.Column('token_kind', sa.String(length=16), nullable=False, server_default='fine_grained'),
        sa.Column('scopes', J, nullable=False, server_default='[]'),
        sa.Column('expires_at', TS, nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='ok'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('checked_at', TS, nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'repos',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('full_name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('account_id', sa.String(length=32), sa.ForeignKey('github_accounts.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='product'),
        sa.Column('owner_customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('private', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('default_branch', sa.String(length=100), nullable=False, server_default='main'),
        sa.Column('html_url', sa.String(length=300), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_repos_account_id', 'repos', ['account_id'])
    op.create_index('ix_repos_owner_customer_id', 'repos', ['owner_customer_id'])
    op.create_table(
        'repo_access',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('repo_id', sa.String(length=32), sa.ForeignKey('repos.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('can_deploy', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('ai_read', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint('repo_id', 'customer_id', name='uq_repo_access'),
    )
    op.create_index('ix_repo_access_repo_id', 'repo_access', ['repo_id'])
    op.create_index('ix_repo_access_customer_id', 'repo_access', ['customer_id'])

    op.add_column('deployments', sa.Column('target', sa.String(length=16), nullable=False, server_default='container'))
    op.add_column('deployments', sa.Column('customer_id', sa.String(length=32),
                                           sa.ForeignKey('environments.id', ondelete='SET NULL'), nullable=True))
    op.create_index('ix_deployments_customer_id', 'deployments', ['customer_id'])
    op.execute("UPDATE deployments d SET customer_id = n.environment_id FROM nodes n WHERE n.id = d.node_id")
    # Every repository that is deployed already goes into the library, linked to the customers it runs at.
    op.execute("""
        INSERT INTO repos (id, full_name, kind, description, private, default_branch, notes, created_at)
        SELECT md5(random()::text || clock_timestamp()::text || x.repo), x.repo, 'product', '', true, 'main', '', now()
        FROM (SELECT DISTINCT ON (lower(repo)) repo FROM deployments ORDER BY lower(repo), created_at) x
    """)
    op.execute("""
        INSERT INTO repo_access (id, repo_id, customer_id, can_deploy, ai_read)
        SELECT md5(random()::text || clock_timestamp()::text || r.id || y.customer_id), r.id, y.customer_id, true, false
        FROM (SELECT DISTINCT lower(repo) AS repo, customer_id FROM deployments WHERE customer_id IS NOT NULL) y
        JOIN repos r ON lower(r.full_name) = y.repo
    """)


def downgrade() -> None:
    op.drop_index('ix_deployments_customer_id', table_name='deployments')
    op.drop_column('deployments', 'customer_id')
    op.drop_column('deployments', 'target')
    op.drop_table('repo_access')
    op.drop_table('repos')
    op.drop_table('github_accounts')
