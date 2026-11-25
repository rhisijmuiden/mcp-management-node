"""netsec: certificates, firewall versions and VPN tunnels per customer (phase 20)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-25
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
TS = sa.DateTime(timezone=True)


def _cust() -> sa.Column:
    return sa.Column('customer_id', sa.String(length=32), sa.ForeignKey('environments.id', ondelete='CASCADE'),
                     nullable=False, index=True)


def _node(nullable: bool = True, ondelete: str = 'CASCADE', index: bool = False) -> sa.Column:
    return sa.Column('node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete=ondelete), nullable=nullable,
                     index=index)


def _conn() -> sa.Column:
    return sa.Column('connection_id', sa.String(length=32), sa.ForeignKey('unifi_connections.id', ondelete='CASCADE'),
                     nullable=True)


def upgrade() -> None:
    op.create_table(
        'cert_endpoints',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('name', sa.String(length=120), nullable=True),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False, server_default='443'),
        sa.Column('sni', sa.String(length=255), nullable=True),
        sa.Column('starttls', sa.String(length=8), nullable=False, server_default='none'),
        sa.Column('probe_node_id', sa.String(length=32), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='manual'),
        sa.Column('uptime_check_id', sa.String(length=32), sa.ForeignKey('uptime_checks.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('warn_days', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_check_at', TS, nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=100), nullable=True),
    )
    op.create_table(
        'certificates',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('key', sa.String(length=400), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('endpoint_id', sa.String(length=32), sa.ForeignKey('cert_endpoints.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        _node(index=True),
        sa.Column('location', sa.String(length=500), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=True),
        sa.Column('common_name', sa.String(length=255), nullable=True),
        sa.Column('sans', J, nullable=False, server_default='[]'),
        sa.Column('issuer', sa.String(length=500), nullable=True),
        sa.Column('serial', sa.String(length=100), nullable=True),
        sa.Column('fingerprint', sa.String(length=100), nullable=True),
        sa.Column('not_before', TS, nullable=True),
        sa.Column('not_after', TS, nullable=True, index=True),
        sa.Column('key_info', sa.String(length=50), nullable=True),
        sa.Column('self_signed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('trusted', sa.Boolean(), nullable=True),
        sa.Column('trust_error', sa.String(length=300), nullable=True),
        sa.Column('hostname_ok', sa.Boolean(), nullable=True),
        sa.Column('in_use', sa.Boolean(), nullable=True),
        sa.Column('renew', sa.String(length=16), nullable=True),
        sa.Column('ignored', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ignored_note', sa.String(length=300), nullable=True),
        sa.Column('previous_not_after', TS, nullable=True),
        sa.Column('renewed_at', TS, nullable=True),
        sa.Column('first_seen', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen', TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('customer_id', 'key', name='uq_certificate_key'),
    )
    op.create_table(
        'firewall_sources',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('key', sa.String(length=300), nullable=False, unique=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=False),
        _node(),
        _conn(),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_hash', sa.String(length=64), nullable=True),
        sa.Column('summary', J, nullable=False, server_default='{}'),
        sa.Column('last_check_at', TS, nullable=True),
        sa.Column('last_ok_at', TS, nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('created_at', TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'firewall_versions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('source_id', sa.String(length=32), sa.ForeignKey('firewall_sources.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        _cust(),
        sa.Column('taken_at', TS, nullable=False, server_default=sa.func.now(), index=True),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('baseline', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('diff', sa.Text(), nullable=True),
        sa.Column('added', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('removed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('made_by', sa.String(length=300), nullable=True),
        sa.Column('acknowledged_at', TS, nullable=True),
        sa.Column('acknowledged_by', sa.String(length=100), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
    )
    op.create_table(
        'vpn_tunnels',
        sa.Column('id', sa.String(length=32), primary_key=True),
        _cust(),
        sa.Column('key', sa.String(length=400), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        _node(index=True),
        _conn(),
        sa.Column('remote', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unknown'),
        sa.Column('status_since', TS, nullable=True),
        sa.Column('detail', J, nullable=False, server_default='{}'),
        sa.Column('changes', J, nullable=False, server_default='[]'),
        sa.Column('watched', sa.Boolean(), nullable=True),
        sa.Column('last_up_at', TS, nullable=True),
        sa.Column('last_check_at', TS, nullable=True),
        sa.Column('first_seen', TS, nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen', TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('customer_id', 'key', name='uq_vpn_tunnel_key'),
    )
    # The addresses listed per customer for the nightly certificate check (phase 12) become endpoints.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, nightly FROM environments")).fetchall()
    import re
    import uuid

    for env_id, nightly in rows:
        cfg = nightly if isinstance(nightly, dict) else json.loads(nightly or "{}")
        seen = set()
        for ep in (cfg or {}).get("cert_endpoints") or []:
            text = re.sub(r"^[a-z]+://", "", str(ep).strip(), flags=re.I).split("/")[0].strip()
            m = re.fullmatch(r"\[([^\]]+)\](?::(\d{1,5}))?", text) or re.fullmatch(r"([^:\s]+)(?::(\d{1,5}))?", text)
            if not m or (m.group(1).lower(), int(m.group(2) or 443)) in seen:
                continue
            seen.add((m.group(1).lower(), int(m.group(2) or 443)))
            conn.execute(sa.text(
                "INSERT INTO cert_endpoints (id, customer_id, host, port, starttls, source, enabled, created_at, created_by) "
                "VALUES (:id, :c, :h, :p, 'none', 'nightly', true, now(), 'migration')"),
                {"id": uuid.uuid4().hex, "c": env_id, "h": m.group(1), "p": int(m.group(2) or 443)})


def downgrade() -> None:
    op.drop_table('vpn_tunnels')
    op.drop_table('firewall_versions')
    op.drop_table('firewall_sources')
    op.drop_table('certificates')
    op.drop_table('cert_endpoints')
