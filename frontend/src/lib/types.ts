export type Role = "admin" | "viewer";

export interface User {
  id: string;
  username: string;
  email: string | null;
  full_name: string | null;
  role: Role;
  language: "en" | "nl";
  theme: "system" | "light" | "dark";
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface AuthStatus {
  setup_required: boolean;
  authenticated: boolean;
  app_name: string;
  default_language: "en" | "nl";
  mail_enabled: boolean;
}

export interface Release {
  tag: string;
  version: string;
  channel: "production" | "test";
  name: string;
  notes: string;
  published_at: string;
  url: string;
}

export interface SystemInfo {
  name: string;
  version: string;
  tag: string;
  build_channel: string;
  commit: string;
  built_at: string | null;
  dev: boolean;
  channel: "production" | "test";
  python: string;
  postgres: string;
  db_revision: string | null;
  os: string;
  uptime_seconds: number;
  updater_available: boolean;
  repo: string;
  last_check: string | null;
  latest: Release | null;
  update_available: boolean;
}

export interface UpdateCheck {
  checked_at: string;
  channel: string;
  current: string;
  latest: Release | null;
  update_available: boolean;
  is_downgrade: boolean;
}

export interface UpdateStatus {
  state: "idle" | "queued" | "running" | "success" | "failed" | "unknown";
  tag?: string;
  channel?: string;
  error?: string | null;
  steps?: { at: string; msg: string }[];
  started_at?: string;
  finished_at?: string;
}

export interface MailSettings {
  enabled: boolean;
  host: string;
  port: number;
  security: "none" | "starttls" | "tls";
  username: string;
  password_set: boolean;
  from_name: string;
  from_address: string;
  reply_to: string;
  verify_tls: boolean;
  min_tls: "1.2" | "1.3";
  timeout: number;
  dkim_enabled: boolean;
  dkim_domain: string;
  dkim_selector: string;
  dkim_key_set: boolean;
}

export type Protocol = "ftp" | "ftps" | "sftp" | "scp";

export interface BackupTarget {
  id: string;
  name: string;
  protocol: Protocol;
  host: string;
  port: number;
  username: string;
  remote_path: string;
  enabled: boolean;
  full_cron: string | null;
  incremental_cron: string | null;
  keep_full: number;
  ftps_implicit: boolean;
  verify_tls: boolean;
  passive: boolean;
  host_key_fingerprint: string | null;
  password_set: boolean;
  private_key_set: boolean;
  encrypted: boolean;
  next_runs?: { full: string | null; incremental: string | null };
}

export interface BackupRun {
  id: string;
  target_id: string | null;
  target_name: string | null;
  kind: "full" | "incremental" | "restore";
  status: "running" | "success" | "failed";
  trigger: string;
  backup_id: string | null;
  parent_backup_id: string | null;
  size_bytes: number | null;
  files_changed: number | null;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  log: { at: string; msg: string }[];
}

export interface RemoteBackup {
  id: string;
  kind: "full" | "incremental";
  chain_id: string;
  parent: string | null;
  created_at: string;
  app_version: string;
  files_changed: number;
  size: number;
  encrypted: boolean;
  restorable: boolean;
}

export interface ApiToken {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  kind: string;
  last_client: ClientInfo;
}

export interface ClientInfo {
  name?: string;
  version?: string;
  user_agent?: string;
  seen_at?: string;
}

export interface Connection {
  client_id: string;
  client_name: string;
  scopes: string[];
  connected_at: string;
  last_used_at: string | null;
  last_client: ClientInfo;
}

export interface AuditEntry {
  id: number;
  at: string;
  username: string | null;
  source: string;
  action: string;
  target: string | null;
  ip: string | null;
  success: boolean;
  detail: Record<string, unknown> & {
    tool?: string;
    args?: Record<string, unknown>;
    result?: Record<string, unknown>;
    error?: string;
    duration_ms?: number;
    token?: string;
    token_kind?: string;
    client?: ClientInfo;
  };
}

export interface ProxySettings {
  mode: "direct" | "proxy";
  trusted_proxies: string[];
  restrict_to_proxy: boolean;
  server_name: string;
  http_port: number;
  https_port: number;
  base_path: string;
  ssl_certificate: string;
  ssl_certificate_key: string;
  apply_available: boolean;
  apply_status: { state: string; error?: string };
  seen: { client_ip: string; scheme: string; host: string; forwarded_for: string | null; forwarded_proto: string | null };
}

/* ------------------------------------------------------------------ phase 2: Proxmox */
export interface Environment {
  id: string;
  name: string;
  kind: "home" | "customer";
  description: string | null;
  color: string;
  created_at: string;
  node_count: number;
  nodes_online: number;
  updates: number;
  notify_enabled: boolean;
  clusters: string[];
}

export interface PackageUpdate {
  name: string;
  current: string;
  new: string;
  arch?: string;
  suite?: string;
}

export interface NodeMetrics {
  at?: string;
  cpu_percent?: number;
  cpus?: number;
  load?: number[];
  mem_total?: number;
  mem_used?: number;
  swap_total?: number;
  swap_used?: number;
  disk_total?: number;
  disk_used?: number;
  uptime_seconds?: number;
  governor?: string;
  cpu_mhz?: number;
  guests?: { vms_running: number; vms_total: number; cts_running: number; cts_total: number };
}

export interface NodeInfo {
  id: string;
  name: string;
  kind: string;
  hostname: string | null;
  environment_id: string;
  environment_name: string | null;
  cluster_name: string | null;
  online: boolean;
  agent_version: string | null;
  agent_update_available: boolean;
  os: string | null;
  arch: string | null;
  install_kind: string | null;
  capabilities: string[];
  info: Record<string, unknown> & { pve_version?: string; kernel?: string; os_pretty_name?: string; cpu_model?: string; cluster?: { name?: string; quorate?: boolean; nodes?: { name: string; online: boolean; local: boolean; ip?: string }[] } };
  metrics: NodeMetrics;
  updates: { count: number; checked_at: string | null; reboot_required: boolean; error?: string | null; packages?: PackageUpdate[] };
  governor: { current?: string; available?: string[]; driver?: string; desired?: string | null };
  notify: { applied_at: string | null; error: string | null; hash: string | null };
  remote_ip: string | null;
  enrolled_at: string;
  last_seen_at: string | null;
  connected_at: string | null;
  cert_expires_at: string | null;
}

export interface Overview {
  environments: Environment[];
  nodes: NodeInfo[];
  totals: {
    nodes: number;
    online: number;
    updates: number;
    nodes_with_updates: number;
    reboot_required: number;
    agents_outdated: number;
    vms_running: number;
    vms_total: number;
    cts_running: number;
    cts_total: number;
  };
  agent_version: string | null;
}

export interface NodeJob {
  id: string;
  node_id: string;
  kind: string;
  status: "running" | "success" | "failed";
  source: string;
  requested_by: string | null;
  result: Record<string, unknown>;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  output?: string;
}

export interface ConnectInfo {
  endpoint: string;
  app_url: string;
  fingerprint: string;
  fingerprint_pretty: string;
  agent_version: string | null;
  packages_available: boolean;
  port: number;
  address: string;
  auto_update: boolean;
  updates_check_hours: number;
}

export interface Pairing {
  id: string;
  environment_id: string;
  environment_name: string;
  label: string | null;
  created_at: string;
  expires_at: string;
  code: string;
  command?: string;
  manual?: string;
}

export interface NotifyConfig {
  enabled: boolean;
  use_app_smtp: boolean;
  server: string;
  port: number;
  mode: "starttls" | "tls" | "insecure";
  username: string;
  password_set: boolean;
  from_address: string;
  author: string;
  mailto: string[];
  types: string[];
  severities: string[];
  types_available: string[];
  severities_available: string[];
}

export interface ExecResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  duration_ms: number;
  timed_out: boolean;
}
