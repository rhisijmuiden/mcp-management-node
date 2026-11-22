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
