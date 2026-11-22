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

/* ------------------------------------------------------------------ phase 3: guests, templates, tasks */
export interface Guest {
  vmid: number;
  type: "qemu" | "lxc";
  name: string;
  status: string;
  node_id: string;
  node: string;
  pve_node: string | null;
  environment_id: string;
  environment: string | null;
  cluster: string | null;
  online: boolean;
  cpu: number;
  maxcpu: number;
  mem: number;
  maxmem: number;
  disk: number;
  maxdisk: number;
  uptime: number;
  netin: number;
  netout: number;
  tags: string[];
  template: boolean;
  lock: string | null;
  hastate: string | null;
  community_script: boolean;
}

export interface Snapshot {
  name: string;
  description: string;
  snaptime: number | null;
  parent: string | null;
  vmstate: boolean;
}

export interface GuestDetail extends Guest {
  config: Record<string, string | number> | null;
  snapshots: Snapshot[];
  interfaces: { name?: string; inet?: string; inet6?: string; hwaddr?: string; "hardware-address"?: string; "ip-addresses"?: { "ip-address": string; prefix: number }[] }[] | null;
  error?: string;
}

export interface NodeResources {
  node_id: string;
  node: string;
  nextid: number | null;
  storages: { storage: string; type: string; content: string[]; avail: number | null; total: number | null; shared: boolean; active: boolean }[];
  bridges: string[];
  isos: { volid: string; size: number; storage: string }[];
  templates: { volid: string; filename: string; size: number }[];
}

export interface SuggestNode {
  node_id?: string;
  node?: string;
  score?: number;
  ranking?: { node_id: string; node: string; score: number }[];
}

export interface TemplateLocation {
  node_id: string;
  node: string;
  environment: string | null;
  online: boolean;
  storage: string;
  status: "current" | "outdated" | "missing" | "unknown";
  volid?: string;
  present?: string[];
}

export interface CtTemplate {
  id: string;
  name: string;
  kind: "official" | "custom";
  family: string;
  filename: string;
  version: string | null;
  os: string | null;
  description: string | null;
  sha256: string | null;
  size: number | null;
  source_url: string | null;
  sync: boolean;
  storage: string | null;
  follow_latest: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  locations?: TemplateLocation[];
  summary?: Record<"current" | "outdated" | "missing" | "unknown", number>;
}

export interface TemplateLibrary {
  templates: CtTemplate[];
  settings: { storage: string };
  catalog_checked_at: string | null;
  nodes: { node_id: string; node: string; online: boolean; checked_at: string | null; storages: string[] }[];
}

export interface CatalogItem {
  filename: string;
  family: string;
  version: string | null;
  os: string | null;
  section: string | null;
  headline: string | null;
  description: string | null;
  infopage: string | null;
}

export interface CommunityScript {
  slug: string;
  kind: "ct" | "vm";
  name: string;
  path: string;
  source_url: string | null;
  tags: string[];
  defaults: { cpu?: number | null; ram?: number | null; disk?: number | null; os?: string | null; version?: string | null; unprivileged?: boolean };
  updated_at: string;
  page_url: string;
}

export interface CommunityScriptFull extends CommunityScript {
  script: string;
  sha256: string;
  script_url: string;
  install_script: string | null;
  install_url: string | null;
  command: string;
}

export interface CommunityState {
  synced_at?: string;
  count?: number;
  error?: string | null;
  repo: string;
  branch: string;
}

export type TaskKind = "ct_community_update" | "ct_apt_upgrade" | "node_updates" | "template_sync" | "custom_command";

export interface TaskTargets {
  scope: "all" | "environments" | "nodes" | "guests" | "tag";
  environment_ids: string[];
  node_ids: string[];
  guests: { node_id: string; vmid: number }[];
  tag: string | null;
  exclude_vmids: number[];
}

export interface TaskOptions {
  snapshot: boolean;
  rollback: boolean;
  keep_snapshots: number;
  command: string | null;
  run_on: "ct" | "node";
  timeout: number;
  parallel_nodes: number;
}

export interface ScheduledTask {
  id: string;
  name: string;
  kind: TaskKind;
  enabled: boolean;
  cron: string;
  targets: TaskTargets;
  options: TaskOptions;
  mail_to: string[];
  mail_on: "always" | "failure" | "never";
  created_by: string | null;
  created_at: string;
  last_run_at: string | null;
  last_status: string | null;
  next_run_at: string | null;
}

export interface TaskTarget {
  kind: "guest" | "node";
  node_id: string;
  node: string;
  vmid?: number;
  name?: string;
  status?: string;
  label: string;
}

export interface TaskRunResult {
  target: string;
  node_id?: string;
  vmid?: number;
  status: "success" | "failed" | "skipped";
  error?: string | null;
  snapshot?: string;
  rolled_back?: boolean;
  job_id?: string;
  output?: string;
  note?: string;
}

export interface TaskRun {
  id: string;
  task_id: string | null;
  name: string;
  kind: string;
  status: "running" | "success" | "partial" | "failed";
  trigger: string;
  requested_by: string | null;
  started_at: string;
  finished_at: string | null;
  results: TaskRunResult[];
  counts: { success: number; failed: number; skipped: number };
}

export interface ConsoleSession {
  session: string;
  kind: "vnc" | "pty";
  mode: string;
  password?: string | null;
  label: string;
  type?: "qemu" | "lxc";
  node?: string;
  vmid?: number;
  job?: NodeJob;
}
