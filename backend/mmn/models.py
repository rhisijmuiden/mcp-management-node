from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
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
    """A group of nodes: "Home", or one per customer. Notification settings live here."""

    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="customer")  # home | customer
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str] = mapped_column(String(16), default="lucht")
    # Proxmox VE notification settings pushed to every node (password encrypted in "password_enc").
    notify: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    nodes: Mapped[list[Node]] = relationship(back_populates="environment", lazy="selectin")


class Node(Base):
    """A machine with the agent: a Proxmox VE node (later also stand-alone Linux/Windows servers)."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16), default="proxmox")  # proxmox | linux | windows
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
