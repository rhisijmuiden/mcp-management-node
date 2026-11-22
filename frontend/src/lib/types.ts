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
  customer_ids?: string[] | null;
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
  customer_ids?: string[] | null;
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
  contact_name?: string | null;
  contact_email?: string | null;
  contact_phone?: string | null;
  contacts?: CustomerContact[];
  notes?: string | null;
  maintenance_window?: string | null;
  sla?: string | null;
  counts?: { proxmox: number; servers: number; workstations: number };
}

/* ------------------------------------------------------------------ phase 7: customers */
export type Customer = Environment;

export interface CustomerContact {
  name: string;
  role?: string | null;
  email?: string | null;
  phone?: string | null;
}

export interface CustomerSummary extends Environment {
  machines: number;
  online: number;
  alerts: number;
  status: "ok" | "attention" | "empty";
  unifi: { connections: number; errors: number; devices: number; firmware: number };
}

export interface Machine {
  id: string;
  name: string;
  type: "proxmox" | "server" | "workstation";
  kind: string;
  role: "server" | "workstation";
  hostname: string | null;
  cluster: string | null;
  online: boolean;
  os: string | null;
  cpu: number | null;
  mem_pct: number | null;
  updates: number;
  reboot_required: boolean;
  attention: string[];
  agent_version: string | null;
  agent_update_available: boolean;
  last_seen_at: string | null;
  guests: { vms_total?: number; cts_total?: number; vms_running?: number; cts_running?: number } | null;
}

export interface TypeTotals { total: number; online: number; updates: number; attention: number; with_updates: number; linux: number; windows: number; guests?: number }

export interface CustomerAiAccess { id: string; name: string; kind: string; scopes: string[]; all_customers: boolean; customers: number; last_used_at: string | null; oauth_client_id: string | null }

export interface CustomerOverview {
  customer: Customer;
  machines: Machine[];
  unifi: { id: string; name: string; kind: string; status: string; enabled: boolean; last_error: string | null; last_ok_at: string | null; sites: number; totals: Record<string, number> }[];
  alerts: { id: number; node: string | null; severity: string; message: string; started_at: string }[];
  totals: { proxmox: TypeTotals; servers: TypeTotals; workstations: TypeTotals; network: { connections: number; errors: number; sites: number; devices: number; offline: number; firmware: number }; alerts: number };
  ai_access: CustomerAiAccess[];
}

export interface ScanFinding { severity: "high" | "medium" | "low" | "info"; area: string; target: string | null; finding: string; suggestion: string; next_tool?: string }

export interface ScanResult {
  customer: string;
  checked: { proxmox_nodes: number; servers: number; workstations: number; unifi_connections: number; live_network_checks: boolean };
  summary: Record<string, number>;
  findings: ScanFinding[];
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
  disks?: { path: string; fs?: string; total: number; used: number }[];
  failed_services?: number;
  processes?: number;
}

export interface NodeInfo {
  id: string;
  name: string;
  kind: string;
  role?: "server" | "workstation";
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
  endpoint_source?: "address" | "lan_ip" | "base_url" | "request";
  proxy_mode?: boolean;
  app_url: string;
  fingerprint: string;
  fingerprint_pretty: string;
  agent_version: string | null;
  packages_available: boolean;
  port: number;
  address: string;
  auto_update: boolean;
  updates_check_hours: number;
  transfer_max_gb?: number;
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
  windows_command?: string;
  msiexec?: string;
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

/* ------------------------------------------------------------------ phase 4: network, SDN, cluster, monitoring, deploys */
export interface NetIface {
  iface: string;
  type: string;
  active: boolean;
  autostart: boolean;
  method: string | null;
  cidr: string | null;
  address: string | null;
  gateway: string | null;
  cidr6: string | null;
  ports: string[];
  vlan_aware: boolean;
  vids: string | null;
  bond_mode: string | null;
  vlan_id: number | null;
  vlan_raw_device: string | null;
  mtu: number | null;
  comments: string;
  pending: boolean;
}

export interface MapGuest {
  vmid: number;
  type: "qemu" | "lxc";
  name: string;
  node_id: string;
  node: string;
  status: string;
  nic: string;
  mac: string | null;
  firewall: boolean;
  trunks: string[];
  ip: string | null;
  link_down: boolean;
  model: string | null;
}

export interface MapBridge {
  name: string;
  kind: "bridge" | "vnet" | "missing";
  vlan_aware: boolean;
  comments: string;
  interfaces: number;
  vnet_tag?: number | null;
  nodes: { node_id: string; node: string; ports: string[]; cidr: string | null; gateway: string | null; vlan_aware: boolean; active: boolean; vids: string | null; pending: boolean }[];
  lanes: { tag: number; guests: MapGuest[] }[];
}

export interface BridgeMap {
  bridges: MapBridge[];
  tags: { tag: number; count: number }[];
  nodes: { node_id: string; node: string; online: boolean }[];
  errors: { node_id: string; node: string; error: string }[];
}

export interface NetPending {
  node_id: string;
  has_changes: boolean;
  diff: string;
  warnings: string[];
  dry_run?: boolean;
  applied?: boolean;
  job?: NodeJob;
  backup_id?: string;
}

export interface ConfigBackup {
  id: string;
  kind: "network" | "sdn";
  node_id: string;
  design_id: string | null;
  reason: string;
  created_by: string | null;
  created_at: string;
  restored_at: string | null;
  files: string[];
  content?: Record<string, string | null>;
}

export interface FwRule {
  pos: number;
  type: string;
  action: string;
  enable?: number;
  proto?: string;
  dport?: string;
  sport?: string;
  source?: string;
  dest?: string;
  macro?: string;
  iface?: string;
  comment?: string;
}

export interface FirewallOverview {
  cluster_options: Record<string, unknown> & { enable?: number; policy_in?: string; policy_out?: string; error?: string };
  cluster_rules: FwRule[] | { error: string };
  groups: { group: string; comment?: string }[] | { error: string };
  aliases: { name: string; cidr: string; comment?: string }[] | { error: string };
  ipsets: { name: string; comment?: string }[] | { error: string };
  node_options: Record<string, unknown> & { enable?: number; error?: string };
  node_rules: FwRule[] | { error: string };
  guests: { vmid: number; type: "qemu" | "lxc"; name: string; nics: { nic: string; bridge: string | null; firewall: boolean }[] }[];
}

export type ZoneType = "vlan" | "simple" | "vxlan" | "evpn" | "qinq";
export interface SdnZoneSpec { id: string; type: ZoneType; bridge?: string | null; tag?: number | null; peers: string[]; controller?: string | null; vrf_vxlan?: number | null; mtu?: number | null; nodes: string[]; ipam: boolean; dhcp: boolean }
export interface SdnVnetSpec { id: string; alias: string; tag?: number | null; vlanaware: boolean; color?: string | null }
export interface SdnSubnetSpec { vnet: string; cidr: string; gateway?: string | null; snat: boolean; dhcp_start?: string | null; dhcp_end?: string | null }
export interface SdnSpec { zone: SdnZoneSpec; vnets: SdnVnetSpec[]; subnets: SdnSubnetSpec[] }

export interface SdnDesign {
  id: string;
  name: string;
  node_id: string;
  spec: SdnSpec;
  status: "draft" | "applied" | "failed" | "rolled_back";
  created_by: string | null;
  created_at: string;
  updated_at: string;
  applied_at: string | null;
  last_job_id: string | null;
  last_backup_id: string | null;
}

export interface SdnState {
  zones: Record<string, unknown>[];
  vnets: (Record<string, unknown> & { vnet: string; zone: string; tag?: number; alias?: string })[];
  subnets: (Record<string, unknown> & { vnet: string; cidr?: string; subnet?: string; gateway?: string })[];
  controllers: Record<string, unknown>[];
  status: Record<string, unknown>[];
}

export interface SdnOp { op: "create" | "update" | "delete" | "reload" | "none"; kind: string; id: string; summary: string; before?: Record<string, string> | null; after?: Record<string, string> | null; used_by?: string[] }

export interface SdnPlan {
  node_id: string;
  cluster: string | null;
  cluster_nodes: string[];
  errors: string[];
  warnings: string[];
  ops: SdnOp[];
  current: SdnState;
  can_apply: boolean;
  dry_run?: boolean;
  applied?: boolean;
  job?: NodeJob;
}

export interface HaOverview {
  mode: "rules" | "groups";
  status: (Record<string, unknown> & { id: string; type: string; status?: string; node?: string; sid?: string; state?: string })[];
  resources: (Record<string, unknown> & { sid: string; state?: string; group?: string; max_restart?: number; max_relocate?: number; comment?: string })[];
  groups: (Record<string, unknown> & { group: string; nodes?: string; restricted?: number })[];
  rules: (Record<string, unknown> & { rule: string; type: string; resources?: string; nodes?: string; affinity?: string })[];
}

export interface ReplicationJob {
  id: string;
  guest: number;
  source?: string;
  target: string;
  schedule?: string;
  rate?: number;
  comment?: string;
  disable?: number;
  last_sync?: number;
  next_sync?: number;
  duration?: number;
  fail_count?: number;
  error?: string;
  state?: string;
}

export interface BackupJobPve {
  id: string;
  schedule?: string;
  storage?: string;
  all?: number;
  vmid?: string;
  node?: string;
  mode?: string;
  compress?: string;
  enabled?: number;
  mailto?: string;
  "prune-backups"?: string | Record<string, string>;
  comment?: string;
  "next-run"?: number;
}

export interface StorageRow {
  storage: string;
  type: string;
  content: string;
  shared: boolean;
  disabled: boolean;
  nodes: string;
  path?: string | null;
  server?: string | null;
  export?: string | null;
  share?: string | null;
  datastore?: string | null;
  pool?: string | null;
  active: boolean;
  total: number;
  used: number;
  avail: number;
  used_pct: number | null;
}

export interface MetricPoint { at: string; cpu: number; cpu_max: number; mem_pct: number | null; mem_used: number; mem_total: number; disk_pct: number | null; load1: number | null }
export interface HealthStorage { storage: string; type: string; content: string; active: boolean; enabled: boolean; shared: boolean; total: number; used: number; avail: number; used_pct: number | null }
export interface HealthDisk { devpath: string; model: string; serial?: string; size: number; type: string; health: string; wearout: number | null; used: string }

export interface MonitoringNode {
  node_id: string;
  name: string;
  kind: string;
  environment_id: string;
  environment: string | null;
  cluster: string | null;
  online: boolean;
  last_seen_at: string | null;
  cpu: number | null;
  cpus?: number;
  load: number[];
  mem_used?: number;
  mem_total?: number;
  disk_used?: number;
  disk_total?: number;
  uptime?: number;
  trend: MetricPoint[];
  storages: HealthStorage[];
  disks: HealthDisk[];
  disk_health: "ok" | "failed" | "unknown";
  health_at: string | null;
  health_error: string | null;
  alerts: number;
}

export interface MonitoringOverview {
  range: string;
  totals: { nodes: number; online: number; cpu_avg: number | null; mem_used: number; mem_total: number; storage_used: number; storage_total: number; alerts_open: number };
  nodes: MonitoringNode[];
}

export interface MonAlert {
  id: string;
  node_id: string | null;
  node: string | null;
  kind: string;
  key: string;
  severity: "warning" | "critical";
  message: string;
  value: number | null;
  started_at: string;
  resolved_at: string | null;
  notified_at: string | null;
}

export interface MonitoringSettings { enabled: boolean; mail_to: string[]; cpu_pct: number; cpu_minutes: number; mem_pct: number; disk_pct: number; storage_pct: number; offline_minutes: number; smart: boolean; wearout_pct: number; unifi?: boolean }

export interface GithubStatus { configured: boolean; login: string | null; checked_at: string | null }
export interface GithubRepo { full_name: string; private: boolean; default_branch: string; description: string; pushed_at: string | null; html_url: string; archived: boolean }
export type DeployPreset = "script" | "python" | "node" | "static" | "custom";

export interface Deployment {
  id: string;
  name: string;
  repo: string;
  channel: "prod" | "test";
  branch_prod: string;
  branch_test: string;
  branch: string;
  node_id: string | null;
  vmid: number;
  path: string;
  preset: DeployPreset;
  setup_cmd: string;
  deploy_cmd: string;
  service: string | null;
  has_env: boolean;
  auto_deploy: "off" | "webhook" | "poll";
  snapshot: boolean;
  rollback: boolean;
  mail_to: string[];
  last_sha: string | null;
  last_status: string | null;
  last_run_at: string | null;
  webhook_url: string;
  created_by: string | null;
  created_at: string;
  runs?: DeploymentRun[];
}

export interface DeploymentRun {
  id: string;
  deployment_id: string;
  status: "running" | "success" | "failed";
  trigger: string;
  branch: string;
  sha: string | null;
  previous_sha: string | null;
  requested_by: string | null;
  job_id: string | null;
  snapshot: string | null;
  rolled_back: boolean;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ServerInfo extends NodeInfo {
  failed_services: number | null;
  disks: { path: string; fs?: string; total: number; used: number }[];
  attention: string[];
}

export interface ServersOverview {
  servers: ServerInfo[];
  totals: { servers: number; online: number; linux: number; windows: number; attention: number; workstations?: number; updates: number; servers_with_updates: number; failed_services: number };
}

export interface FanOutRow<T> {
  node_id: string;
  server: string;
  kind: string;
  environment: string | null;
  online: boolean;
  result?: T;
  error?: string;
}

export interface WinEvent { time: string; log: string; level: string; provider: string; id: number; message: string }

export interface LogSearchResult {
  source: "journal" | "eventlog" | "files";
  lines?: string[];
  events?: WinEvent[];
  matches?: { file: string; line: number; text: string }[];
  count?: number;
  files_scanned?: number;
  truncated?: boolean;
  stopped?: string;
  errors?: string[];
  error?: string;
}

export interface ErrorsResult {
  failed_services?: string[];
  error_count?: number;
  recent_errors?: string[];
  findings: string[];
  disks: { path: string; used_pct: number }[];
  services_error?: string;
  logs_error?: string;
}

export interface CveResult {
  installed: { name: string; version: string; source?: string; update_available?: string; status?: "vulnerable" | "fixed"; fixed_by_pending_update?: boolean }[];
  vulnerable: number;
}

export interface Diagnosis {
  at: string;
  hostname: string;
  metrics: NodeMetrics;
  findings: string[];
  failed_services: { name: string; description: string; active: string; sub: string }[] | null;
  recent_errors: string;
  top_cpu: { pid: number; name: string; user?: string; cpu: number; mem_bytes: number }[] | null;
  top_memory: { pid: number; name: string; user?: string; cpu: number; mem_bytes: number }[] | null;
  listening: { proto: string; address: string; process?: string }[] | null;
  time_sync?: string;
  errors?: string[];
}

export interface Transfer {
  id: string;
  source_node_id: string | null;
  target_node_id: string | null;
  source_server: string;
  target_server: string;
  source_paths: string[];
  target_path: string;
  overwrite: boolean;
  status: "running" | "success" | "failed";
  stage: "send" | "receive" | "done";
  reason: string | null;
  source: string;
  requested_by: string | null;
  send_job_id: string | null;
  receive_job_id: string | null;
  bytes: number | null;
  wire_bytes: number | null;
  files: number | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface InstalledPackage { name: string; version: string; arch?: string; status?: string; source?: string; vendor?: string }

/* ------------------------------------------------------------------ UniFi (phase 6) */
export type UnifiKind = "network" | "site_manager" | "access";
export interface UnifiConnection {
  id: string;
  name: string;
  kind: UnifiKind;
  transport: "direct" | "agent" | "cloud";
  base_url: string | null;
  environment_id: string;
  environment_name: string | null;
  agent_node_id: string | null;
  cloud_connection_id: string | null;
  console_id: string | null;
  has_key: boolean;
  read_only: boolean;
  tls_mode: "verify" | "pin" | "insecure";
  tls_fingerprint: string | null;
  enabled: boolean;
  status: "unknown" | "ok" | "error";
  last_error: string | null;
  last_ok_at: string | null;
  last_poll_at: string | null;
  version: string | null;
  sites: { id: string; name: string }[];
  totals: Record<string, number>;
}
export interface UnifiPort { idx: number; state: string; connector?: string; speed_mbps?: number | null; max_speed_mbps?: number | null; poe?: boolean; poe_state?: string | null }
export interface UnifiDevice {
  id: string;
  name: string;
  model: string | null;
  mac: string | null;
  ip: string | null;
  state: string;
  online: boolean;
  type: "gateway" | "switch" | "ap" | "other";
  firmware: string | null;
  firmware_updatable: boolean;
  uplink_device_id: string | null;
  uplink_name?: string | null;
  uplink_port?: number | null;
  clients: number;
  ports_up?: number;
  ports_total?: number;
  connection_id: string;
  connection: string;
  environment: string | null;
  site_id?: string;
  site?: string | null;
  source: "network" | "cloud";
}
export interface UnifiClient { id: string; name: string | null; mac: string | null; ip: string | null; type: string; connected_at: string | null; uplink_device_id: string | null; uplink_name: string | null; access: string; authorized: boolean | null }
export interface UnifiDeviceDetail extends Omit<UnifiDevice, "connection_id" | "connection" | "environment" | "source"> {
  ports: UnifiPort[];
  radios: { standard: string | null; frequency_ghz: number | null; channel: number | null; channel_width_mhz: number | null }[];
  stats: { uptime_sec: number | null; cpu_pct: number | null; mem_pct: number | null; uplink_tx_bps: number | null; uplink_rx_bps: number | null; radios: { frequency_ghz: number; tx_retries_pct: number | null }[] };
  client_list: UnifiClient[];
}
export interface UnifiWifi {
  id: string; name: string; enabled: boolean; security: string | null; vlan: number | null; network_name: string | null; hidden: boolean;
  client_isolation: boolean; mac_filter: { action: string; macs: string[] } | null; blackout_days: number; guest: boolean;
  connection_id: string; connection: string; site_id: string; site: string;
}
export interface UnifiEvent { id: number; at: string; connection_id: string; connection: string | null; site: string | null; kind: string; severity: "info" | "warning" | "critical"; subject: string | null; message: string }
export interface UnifiSite {
  connection_id: string; connection: string; environment: string | null; site_id: string; site: string; host?: string | null;
  clients: { total: number; wired?: number | null; wireless?: number | null; guest?: number | null };
  devices: number | null; offline: number | null; wan_uptime?: number | null; isp?: string | null; cloud?: boolean;
  wans?: { id: string; name: string; status: Record<string, unknown> }[];
}
export interface UnifiOverview {
  connections: UnifiConnection[];
  sites: UnifiSite[];
  devices: UnifiDevice[];
  wifi: UnifiWifi[];
  events: UnifiEvent[];
  totals: { devices: number; offline: number; firmware: number; clients: number; connections: number; errors: number };
}
export interface UnifiFinding { severity: "info" | "warning" | "critical"; text: string }
export interface UnifiDiagnosis { query: string; matches: UnifiClient[]; findings: UnifiFinding[]; ssids: { name: string; enabled: boolean; security: string | null; vlan: number | null; notes: string[] }[] }
export interface UnifiIsolation {
  verdict: "isolated" | "partial" | "open" | "unknown" | "no_guest_network";
  guest_networks: { network: string; vlan: number | null; zone: string | null; ssids: { name: string; client_isolation: boolean }[]; to: { network: string; vlan: number | null; zone: string | null; verdict: string; why: string }[] }[];
  note: string;
}
export interface UnifiDoor { id: string; name: string; full_name: string | null; lock: string | null; position: string | null; has_hub: boolean }
export interface UnifiAccessConn extends UnifiConnection { doors: UnifiDoor[] }
export interface UnifiLogRow { at: string; actor: string | null; actor_type: string | null; event: string | null; result: string | null; message: string | null; method: string | null; targets: { type: string; id: string; name: string }[] }
