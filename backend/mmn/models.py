from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mmn.db import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Role(str, enum.Enum):
    admin = "admin"
    viewer = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), default=Role.viewer)
    language: Mapped[str] = mapped_column(String(8), default="en")
    theme: Mapped[str] = mapped_column(String(8), default="system")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remember: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiToken(Base):
    """Personal API token, used for the MCP endpoint and scripts."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # "personal" (created in the UI) or "oauth" (issued to an AI client through the OAuth flow)
    kind: Mapped[str] = mapped_column(String(16), default="personal")
    oauth_client_id: Mapped[str | None] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=True, index=True)
    refresh_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the AI client said about itself in the MCP "initialize" call, and its User-Agent.
    last_client: Mapped[dict] = mapped_column(JsonType, default=dict)
    # Customers this token may see (environment ids). None = all customers.
    customer_ids: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True, default=None)

    user: Mapped[User] = relationship(lazy="joined")


class OAuthClient(Base):
    """An AI client registered through OAuth dynamic client registration (RFC 7591)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(200))
    redirect_uris: Mapped[list[str]] = mapped_column(JsonType, default=list)
    client_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.client_id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    scopes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    code_challenge: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)
    customer_ids: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True, default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="ui")  # ui, mcp, system, cli
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)


class BackupProtocol(str, enum.Enum):
    ftp = "ftp"
    ftps = "ftps"
    sftp = "sftp"
    scp = "scp"


class BackupTarget(Base):
    __tablename__ = "backup_targets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    protocol: Mapped[BackupProtocol] = mapped_column(Enum(BackupProtocol, name="backup_protocol"))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(128))
    # Encrypted secrets (Fernet), never returned by the API.
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # backup encryption
    remote_path: Mapped[str] = mapped_column(String(500), default=".")
    options: Mapped[dict] = mapped_column(JsonType, default=dict)  # ftps_implicit, verify_tls, host_key, passive
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    full_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    incremental_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    keep_full: Mapped[int] = mapped_column(Integer, default=4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    target_id: Mapped[str | None] = mapped_column(
        ForeignKey("backup_targets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # full, incremental, restore
    status: Mapped[str] = mapped_column(String(16), default="running")  # running, success, failed
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual, schedule, mcp
    backup_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # name on the remote
    parent_backup_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict] = mapped_column(JsonType, default=dict)  # full file state after this backup
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    files_changed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log: Mapped[list] = mapped_column(JsonType, default=list)


# ------------------------------------------------------------------ phase 2: environments and nodes
class Environment(Base):
    """A customer ("Home" is a customer too). Everything hangs under it: Proxmox VE nodes, servers,
    workstations and UniFi connections. Stored as "environments" for compatibility; the interface
    and the MCP tools call it a customer. Notification settings live here."""

    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="customer")  # home | customer
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str] = mapped_column(String(16), default="lucht")
    # Proxmox VE notification settings pushed to every node (password encrypted in "password_enc").
    notify: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Phase 7: customer details.
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    contacts: Mapped[list] = mapped_column(JsonType, default=list)  # [{name, role, email, phone}]
    # Shown to the AI (MCP resource and get_customer_overview): "never restart X during office hours".
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_window: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sla: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Phase 10: monthly report settings {"enabled", "day", "recipients", "lang", "include_ai"}.
    report: Mapped[dict] = mapped_column(JsonType, default=dict)
    # Phase 11: backup policy {"max_age_hours", "alert_failed", "alert_missing", "workstations",
    # "recipients", "exclude": [item keys]}.
    backup: Mapped[dict] = mapped_column(JsonType, default=dict)
    # Phase 12: nightly checks {"enabled": bool, "cert_endpoints": ["host:port", ...]}.
    nightly: Mapped[dict] = mapped_column(JsonType, default=dict)

    nodes: Mapped[list[Node]] = relationship(back_populates="environment", lazy="selectin")


class Node(Base):
    """A machine with the agent: a Proxmox VE node (later also stand-alone Linux/Windows servers)."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16), default="proxmox")  # proxmox | linux | windows
    role: Mapped[str] = mapped_column(String(16), default="server")  # server | workstation (linux/windows)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cluster_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os: Mapped[str | None] = mapped_column(String(32), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(16), nullable=True)
    install_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # deb | binary | windows
    capabilities: Mapped[list[str]] = mapped_column(JsonType, default=list)
    info: Mapped[dict] = mapped_column(JsonType, default=dict)
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict)
    updates: Mapped[dict] = mapped_column(JsonType, default=dict)
    governor: Mapped[dict] = mapped_column(JsonType, default=dict)  # current, available, driver
    governor_desired: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notify_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notify_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    cert_serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Proxmox VE: the node's own name, its guests (VMs/CTs) and what is in its template storages.
    pve_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    guests: Mapped[dict] = mapped_column(JsonType, default=dict)
    storage_scan: Mapped[dict] = mapped_column(JsonType, default=dict)
    # Monitoring: storages, physical disks with SMART health (collected every few minutes).
    health: Mapped[dict] = mapped_column(JsonType, default=dict)
    # Phase 10: hardware/software inventory and security status (workstations, collected through the agent).
    inventory: Mapped[dict] = mapped_column(JsonType, default=dict)
    inventory_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inventory_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    environment: Mapped[Environment] = relationship(back_populates="nodes", lazy="joined")


class PairingCode(Base):
    """One-time code that lets a new agent enroll into an environment."""

    __tablename__ = "pairing_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_enc: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Pair again as an existing node (e.g. after a reinstall) instead of creating a new one.
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)  # server | workstation
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    environment: Mapped[Environment] = relationship(lazy="joined")


class NodeJob(Base):
    """A longer task on a node (package upgrade, agent update), with its output."""

    __tablename__ = "node_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # updates.apply | agent.update
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | failed
    source: Mapped[str] = mapped_column(String(16), default="ui")
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    result: Mapped[dict] = mapped_column(JsonType, default=dict)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CtTemplate(Base):
    """An entry of the central container template library (official Proxmox VE or custom)."""

    __tablename__ = "ct_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))  # official | custom
    family: Mapped[str] = mapped_column(String(200), index=True)  # e.g. debian-13-standard
    filename: Mapped[str] = mapped_column(String(255))  # current file, e.g. debian-13-standard_13.1-2_amd64.tar.zst
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    previous_filenames: Mapped[list[str]] = mapped_column(JsonType, default=list)
    sync: Mapped[bool] = mapped_column(Boolean, default=True)
    storage: Mapped[str | None] = mapped_column(String(100), nullable=True)  # None = library default
    follow_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommunityScript(Base):
    """A script from community-scripts.org (ProxmoxVE repository), synced nightly."""

    __tablename__ = "community_scripts"

    slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), default="ct")  # ct | vm
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(255))  # ct/debian.sh
    blob_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JsonType, default=list)
    defaults: Mapped[dict] = mapped_column(JsonType, default=dict)  # cpu, ram, disk, os, version, unprivileged
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledTask(Base):
    """A recurring job: updates in containers, node updates, template sync or a custom command."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cron: Mapped[str] = mapped_column(String(100))
    targets: Mapped[dict] = mapped_column(JsonType, default=dict)
    options: Mapped[dict] = mapped_column(JsonType, default=dict)
    mail_to: Mapped[list[str]] = mapped_column(JsonType, default=list)
    mail_on: Mapped[str] = mapped_column(String(16), default="always")  # always | failure | never
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)


class TaskRun(Base):
    """One run of a scheduled task (or of the nightly template sync), with a result per target."""

    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_tasks.id", ondelete="SET NULL"),
                                                nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | partial | failed
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # schedule | manual | mcp
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    results: Mapped[list] = mapped_column(JsonType, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------ phase 4: monitoring, network, deploys
class MetricSample(Base):
    """One metrics sample per node and minute (kept 30 days) for the monitoring trends."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cpu: Mapped[float] = mapped_column(default=0.0)
    load1: Mapped[float | None] = mapped_column(nullable=True)
    mem_used: Mapped[int] = mapped_column(BigInteger, default=0)
    mem_total: Mapped[int] = mapped_column(BigInteger, default=0)
    swap_used: Mapped[int] = mapped_column(BigInteger, default=0)
    disk_used: Mapped[int] = mapped_column(BigInteger, default=0)
    disk_total: Mapped[int] = mapped_column(BigInteger, default=0)


class Alert(Base):
    """A monitoring alert: open while the condition lasts, mailed when it opens and resolves."""

    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # offline | cpu | memory | disk | storage | smart | deploy
    key: Mapped[str] = mapped_column(String(200), index=True)  # unique per open alert, e.g. "storage:<node>:local"
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # warning | critical
    message: Mapped[str] = mapped_column(String(500))
    value: Mapped[float | None] = mapped_column(nullable=True)
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SdnDesign(Base):
    """A draft of one SDN zone with its VNets and subnets, made in the SDN designer."""

    __tablename__ = "sdn_designs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)  # cluster anchor
    spec: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | applied | failed | rolled_back
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_backup_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ConfigBackup(Base):
    """Configuration taken from a node right before the app changed it (SDN or network), so it can
    be put back with one click."""

    __tablename__ = "config_backups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # sdn | network
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    design_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    files: Mapped[dict] = mapped_column(JsonType, default=dict)  # path -> content (None: did not exist)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Deployment(Base):
    """A GitHub repository deployed natively into an LXC container (production or test branch)."""

    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    repo: Mapped[str] = mapped_column(String(200))  # owner/name
    channel: Mapped[str] = mapped_column(String(8), default="prod")  # prod | test
    branch_prod: Mapped[str] = mapped_column(String(100), default="main")
    branch_test: Mapped[str] = mapped_column(String(100), default="test")
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    vmid: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(255))
    preset: Mapped[str] = mapped_column(String(32), default="script")
    setup_cmd: Mapped[str] = mapped_column(Text, default="")
    deploy_cmd: Mapped[str] = mapped_column(Text, default="")
    service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    env_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted KEY=value lines
    auto_deploy: Mapped[str] = mapped_column(String(16), default="off")  # off | webhook | poll
    webhook_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[bool] = mapped_column(Boolean, default=True)
    rollback: Mapped[bool] = mapped_column(Boolean, default=True)
    mail_to: Mapped[list[str]] = mapped_column(JsonType, default=list)
    last_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeploymentRun(Base):
    __tablename__ = "deployment_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | failed
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | webhook | poll | mcp | create
    branch: Mapped[str] = mapped_column(String(100))
    sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Transfer(Base):
    """A copy of files or folders from one server to another, relayed through the app."""

    __tablename__ = "transfers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    target_node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    source_name: Mapped[str] = mapped_column(String(100))
    target_name: Mapped[str] = mapped_column(String(100))
    source_paths: Mapped[list[str]] = mapped_column(JsonType, default=list)
    target_path: Mapped[str] = mapped_column(String(1000))
    overwrite: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | failed
    stage: Mapped[str] = mapped_column(String(16), default="send")  # send | receive | done
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="ui")
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    send_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    receive_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    wire_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------ phase 6: UniFi
class UnifiConnection(Base):
    """A UniFi API connection of an environment: a Network console (Integration API), the Site Manager
    cloud API (all consoles of an account) or an Access console (developer API)."""

    __tablename__ = "unifi_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # network | site_manager | access
    # How the app reaches the API: direct (from this server), agent (an agent in the customer's network makes the
    # HTTPS call) or cloud (Network API through the Site Manager connector of another connection).
    transport: Mapped[str] = mapped_column(String(16), default="direct")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    agent_node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    cloud_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("unifi_connections.id", ondelete="SET NULL"), nullable=True)
    console_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # Site Manager host id
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet, never returned by the API
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    tls_mode: Mapped[str] = mapped_column(String(16), default="verify")  # verify | pin | insecure
    tls_fingerprint: Mapped[str | None] = mapped_column(String(100), nullable=True)  # SHA-256 hex
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown | ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    info: Mapped[dict] = mapped_column(JsonType, default=dict)  # application version, sites
    snapshot: Mapped[dict] = mapped_column(JsonType, default=dict)  # last poll: devices, clients, WiFi per site
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    environment: Mapped[Environment] = relationship(lazy="joined")


class UnifiEvent(Base):
    """Something that changed between two polls: a device went offline, came back, a WAN changed, new firmware."""

    __tablename__ = "unifi_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("unifi_connections.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    site_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | warning | critical
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)


class AiPolicy(Base):
    """Guardrails for AI (MCP) tool calls. One row for the defaults (scope "global"), and optional rows
    per customer and per token. Each risk level and each tool maps to allow, approve or block;
    "inherit" (or a missing key) falls through to the layer above."""

    __tablename__ = "ai_policies"
    __table_args__ = (UniqueConstraint("scope", "scope_id", name="uq_ai_policies_scope"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(16))  # global | customer | token
    scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # {"read": "allow", "operate": "allow", "change": "allow", "destructive": "approve"}
    risks: Mapped[dict] = mapped_column(JsonType, default=dict)
    # {"run_command": "approve", "unlock_door": "block"}
    tools: Mapped[dict] = mapped_column(JsonType, default=dict)
    # What happens to change and destructive calls outside the customer's maintenance window:
    # inherit | ignore | approve | block
    outside_window: Mapped[str] = mapped_column(String(16), default="inherit")
    # Pause all AI actions for this customer or token (the global pause lives in the settings).
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AiApproval(Base):
    """An AI tool call that waits for a person: created when a policy says "approve", executed by the
    app (with the token's own rights) once an admin approves it."""

    __tablename__ = "ai_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # pending | approved | running | executed | failed | rejected | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    tool: Mapped[str] = mapped_column(String(100))
    risk: Mapped[str] = mapped_column(String(16))
    args_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # the full arguments, encrypted
    args_view: Mapped[dict] = mapped_column(JsonType, default=dict)  # without secrets, for the interface
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_ids: Mapped[list] = mapped_column(JsonType, default=list)
    customer_names: Mapped[list] = mapped_column(JsonType, default=list)
    reasons: Mapped[list] = mapped_column(JsonType, default=list)  # why it needs approval
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # the AI's own `reason` argument
    token_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    token_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client: Mapped[dict] = mapped_column(JsonType, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | list | None] = mapped_column(JsonType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Approved calls of the nightly checks that wait for the customer's maintenance window.
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class McpUpstream(Base):
    """Another MCP server registered in the MCP hub (Keystone, GitHub, Home Assistant...). Its tools are
    offered through Stackpilot's own MCP endpoint as ``<namespace>__<tool>``, with the same tokens, scopes,
    guardrails, approvals and audit log as the built-in tools."""

    __tablename__ = "mcp_upstreams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The customer this server belongs to; None = shared (only tokens for all customers see it).
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"),
                                                       nullable=True, index=True)
    transport: Mapped[str] = mapped_column(String(16), default="http")  # http | sse | stdio
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # http through an agent in the customer's network (no open ports)
    agent_node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    tls_mode: Mapped[str] = mapped_column(String(16), default="verify")  # verify | pin | insecure
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)  # stdio
    args: Mapped[list] = mapped_column(JsonType, default=list)
    auth_kind: Mapped[str] = mapped_column(String(16), default="none")  # none | bearer | headers | oauth
    # Encrypted JSON: {"token": ..., "headers": {...}, "env": {...}, "oauth": {...}}
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Not secret: preset, oauth client id/scopes, names of the secret headers and env vars, timeouts.
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # unknown | online | error | auth_required | disabled
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_info: Mapped[dict] = mapped_column(JsonType, default=dict)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the server offers now: [{name, exposed, title, description, input_schema, annotations, hash}]
    tools: Mapped[list] = mapped_column(JsonType, default=list)
    # Per tool: {"approved_hash": ..., "enabled": true, "risk": "change"|None}
    tool_settings: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CustomerReport(Base):
    """A monthly (or on-demand) report for one customer: availability, alerts, updates, backups,
    security, AI actions and recommendations, kept as data plus the rendered HTML and PDF."""

    __tablename__ = "customer_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lang: Mapped[str] = mapped_column(String(5), default="en")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # schedule | manual | mcp
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)
    html: Mapped[str] = mapped_column(Text, default="")
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    sent_to: Mapped[list] = mapped_column(JsonType, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ------------------------------------------------------------------ phase 11: backup management
class BackupCoverage(Base):
    """The latest backup coverage of one customer: every Proxmox VE guest, server and workstation
    with its last backup, age, size, failures and status, plus the backup jobs, storages and Proxmox
    Backup Servers. Collected by the scheduler; alerts, the report and the scan read it."""

    __tablename__ = "backup_coverage"

    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), primary_key=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NightlyRun(Base):
    """One run of the nightly checks (all customers, or the ones in customer_ids). ``summary`` holds
    per customer the counts, what is new and what was resolved since the run before."""

    __tablename__ = "nightly_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | failed
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule | manual | mcp
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_ids: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    summary: Mapped[dict] = mapped_column(JsonType, default=dict)
    summary_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NightlyFinding(Base):
    """A finding of a nightly run, with the proposal (a tool call) that fixes it and the approval
    request that waits for one click."""

    __tablename__ = "nightly_findings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("nightly_runs.id", ondelete="CASCADE"), index=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)  # stable over runs: code + target
    category: Mapped[str] = mapped_column(String(16))  # updates | cves | disks | certificates | backups | other
    severity: Mapped[str] = mapped_column(String(8))
    area: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    finding: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    next_tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    proposal: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class Runbook(Base):
    """A fixed procedure ("patch a server", "new employee", "new project via GitHub") that Stackpilot
    runs step by step with its own tools. Global (customer_id None) or for one customer.

    ``steps``: [{"id", "title", "kind": "tool"|"manual", "tool", "args", "approval": "policy"|"always",
    "on_failure": "stop"|"continue", "instructions", "assignee": "human"|"ai"}]; arguments may hold
    placeholders ({{params.x}}, {{customer.name}}, {{steps.<id>.result.<path>}}).
    ``params``: [{"name", "label", "required", "default", "description"}].
    ``category``: maintenance | onboarding | project | remediation | other. Runbooks with category
    "remediation" are meant as repair actions; ``triggers`` (reserved for automatic recovery,
    phase 14) lists the alerts or findings they fix."""

    __tablename__ = "runbooks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), nullable=True,
                                                    index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[list] = mapped_column(JsonType, default=list)
    steps: Mapped[list] = mapped_column(JsonType, default=list)
    use_window: Mapped[bool] = mapped_column(Boolean, default=True)
    triggers: Mapped[list] = mapped_column(JsonType, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RunbookRun(Base):
    """One run of a runbook. ``definition`` is a copy of the runbook (steps and params) when it
    started, so editing the runbook does not change a running procedure. ``state`` holds per step
    its status, approval request, result and error."""

    __tablename__ = "runbook_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    runbook_id: Mapped[str | None] = mapped_column(ForeignKey("runbooks.id", ondelete="SET NULL"), nullable=True,
                                                   index=True)
    runbook_name: Mapped[str] = mapped_column(String(120))
    runbook_version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JsonType, default=dict)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id", ondelete="SET NULL"), nullable=True,
                                                    index=True)
    customer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    # running | waiting_approval | waiting_input | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[list] = mapped_column(JsonType, default=list)
    trigger: Mapped[str] = mapped_column(String(16), default="ui")  # ui | mcp | remediation | schedule
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JsonType, default=dict)  # e.g. the alert a repair run answers
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    token_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
