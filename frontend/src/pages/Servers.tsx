import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, AppWindow, ArrowRight, ArrowUpCircle, Bot, Copy, FolderSync, KeyRound, MessageSquare, Monitor, Play, Plus, ScrollText,
  Search, ShieldAlert, Terminal,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { CodeBlock, CopyButton } from "@/components/CopyButton";
import { useCustomer } from "@/components/customers";
import { JobBadge, JobView, Output } from "@/components/jobs";
import { AgentAddressNotice, StatusDot, UsageBar, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { errorMessage, get, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  ConnectInfo, CveResult, ErrorsResult, FanOutRow, LogSearchResult, NodeInfo, Pairing, ServerInfo, ServersOverview, Transfer,
} from "@/lib/types";
import { cn, formatBytes, timeAgo } from "@/lib/utils";

const tabCls = "rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer";

export function OsBadge({ kind, label }: { kind: string; label?: string | null }) {
  const win = kind === "windows";
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide",
        win ? "bg-info-soft text-ink" : kind === "proxmox" ? "bg-accent-soft text-accent-text" : "bg-sunken text-ink")}>
        {win ? <AppWindow className="size-3" /> : <Terminal className="size-3" />}{win ? "Windows" : kind === "proxmox" ? "Proxmox" : "Linux"}
      </span>
      {label && <span className="truncate text-xs text-muted">{label}</span>}
    </span>
  );
}

const pct = (u?: number, t?: number) => (t ? ((u ?? 0) / t) * 100 : 0);

function worstDisk(s: ServerInfo) {
  const disks = s.disks?.length ? s.disks : s.metrics.disk_total ? [{ path: "/", total: s.metrics.disk_total, used: s.metrics.disk_used ?? 0 }] : [];
  return disks.sort((a, b) => pct(b.used, b.total) - pct(a.used, a.total))[0];
}

function useServers(os: string, env: string, role?: string) {
  const qs = new URLSearchParams();
  if (os !== "all") qs.set("os", os);
  if (env) qs.set("environment_id", env);
  if (role) qs.set("role", role);
  return useQuery({ queryKey: ["servers-overview", os, env, role ?? ""], queryFn: () => get<ServersOverview>(`api/servers/overview?${qs}`), refetchInterval: 30_000 });
}

/* ------------------------------------------------------------------ overview */
export function ServersPage({ role = "server" }: { role?: "server" | "workstation" }) {
  const { t, i18n } = useTranslation();
  const ws = role === "workstation";
  const { customerId } = useCustomer();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const envs = useEnvironments();
  const [os, setOs] = useState("all");
  const [env, setEnv] = useState("");
  const [add, setAdd] = useState(false);
  const q = useServers(os, env, role);
  const tot = q.data?.totals;
  const rows = q.data?.servers ?? [];
  return (
    <>
      <PageHeader
        title={ws ? t("cust.ws_title") : t("srv.title")}
        description={ws ? t("cust.ws_subtitle") : t("srv.subtitle")}
        actions={<>
          <Segmented value={os} onChange={setOs} options={[{ value: "all", label: t("srv.all") }, { value: "linux", label: "Linux" }, { value: "windows", label: "Windows" }]} />
          {!customerId && (
            <Select value={env} onChange={(e) => setEnv(e.target.value)} className="h-9 w-auto py-0 text-[13px]" aria-label={t("px.environment")}>
              <option value="">{t("srv.all_envs")}</option>
              {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </Select>
          )}
          {admin && <Button variant="primary" onClick={() => setAdd(true)}><Plus /> {ws ? t("cust.ws_add") : t("srv.add")}</Button>}
        </>}
      />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={ws ? t("cust.workstations") : t("srv.k_servers")} value={tot?.servers ?? "…"} hint={tot ? t("srv.k_split", { linux: tot.linux, windows: tot.windows }) : undefined} icon={<Monitor />} tone="info" />
        <Stat label={t("srv.k_online")} value={tot ? `${tot.online}/${tot.servers}` : "…"} hint={t("srv.k_online_hint")} icon={<KeyRound />} tone={tot && tot.online < tot.servers ? "offline" : "online"} />
        <Stat label={t("srv.k_attention")} value={tot?.attention ?? "…"} hint={tot?.failed_services ? t("srv.k_failed", { count: tot.failed_services }) : t("srv.k_attention_hint")} icon={<AlertTriangle />} tone={tot?.attention ? "warning" : "online"} />
        <Stat label={t("srv.k_updates")} value={tot?.updates ?? "…"} hint={tot ? t("srv.k_updates_hint", { count: tot.servers_with_updates }) : undefined} icon={<ArrowUpCircle />} tone={tot?.updates ? "accent" : "online"} />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="min-w-0">
          <CardHeader icon={<Monitor />} title={ws ? t("cust.workstations") : t("srv.list")} description={ws ? t("cust.ws_list_desc") : t("srv.list_desc")} />
          {q.isLoading ? <CardBody><Spinner /></CardBody> : !rows.length ? (
            <EmptyState icon={<Monitor />} title={ws ? t("cust.ws_empty") : t("srv.empty")} action={admin && <Button variant="primary" onClick={() => setAdd(true)}><Plus /> {ws ? t("cust.ws_add") : t("srv.add")}</Button>}>{ws ? t("cust.ws_empty_text") : t("srv.empty_text")}</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <thead><tr><Th>{t("srv.col_server")}</Th><Th>{t("srv.col_os")}</Th><Th className="w-28">CPU</Th><Th className="w-28">RAM</Th><Th className="w-28">{t("srv.col_disk")}</Th><Th>{t("srv.col_services")}</Th><Th>{t("srv.col_updates")}</Th></tr></thead>
                <tbody>
                  {rows.map((s) => {
                    const d = worstDisk(s);
                    return (
                      <Tr key={s.id} className="cursor-pointer" onClick={() => navigate(`/servers/${s.id}`)}>
                        <Td>
                          <div className="flex items-center gap-2"><StatusDot online={s.online} /><Link to={`/servers/${s.id}`} className="font-bold hover:underline" onClick={(e) => e.stopPropagation()}>{s.name}</Link></div>
                          <div className="whitespace-nowrap text-xs text-muted">{s.environment_name}{!s.online && ` · ${t("px.offline")} ${timeAgo(s.last_seen_at, i18n.language)}`}</div>
                        </Td>
                        <Td>
                          <OsBadge kind={s.kind} />
                          <div className="mt-1 max-w-48 truncate text-xs text-muted" title={String(s.info?.os_pretty_name ?? "")}>{String(s.info?.os_pretty_name ?? "")}</div>
                          <div className="text-[11px] text-muted">{t("srv.col_agent")} <span className="mono">{s.agent_version ?? "–"}</span>{s.agent_update_available && <Badge tone="info" className="ml-1.5">{t("px.agent_outdated")}</Badge>}</div>
                        </Td>
                        <Td><UsageBar label="" percent={s.metrics.cpu_percent ?? 0} /></Td>
                        <Td><UsageBar label="" used={s.metrics.mem_used} total={s.metrics.mem_total} /></Td>
                        <Td>{d ? <UsageBar label={d.path} used={d.used} total={d.total} /> : "–"}</Td>
                        <Td>{s.failed_services == null ? <span className="text-muted">–</span> : s.failed_services > 0
                          ? <Badge tone="offline" dot className="whitespace-nowrap">{t("srv.failed_n", { count: s.failed_services })}</Badge>
                          : <Badge tone="online" dot className="whitespace-nowrap">{t("srv.all_running")}</Badge>}</Td>
                        <Td className="whitespace-nowrap">
                          {s.updates.count > 0 ? <Badge tone="accent">{s.updates.count}</Badge> : s.updates.checked_at ? <span className="text-xs text-muted">{t("srv.none")}</span> : "–"}
                          {s.updates.reboot_required && <div className="mt-1"><Badge tone="warning">{t("px.reboot_required")}</Badge></div>}
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            </div>
          )}
        </Card>
        <div className="space-y-6">
          <ClaudeCard />
          <Card>
            <CardHeader icon={<Search />} title={t("srv.tools_title")} />
            <CardBody className="space-y-2">
              {[
                { to: "/servers/logs", icon: <ScrollText className="size-4" />, label: t("nav.srv_logs") },
                { to: "/servers/software", icon: <ShieldAlert className="size-4" />, label: t("nav.srv_software") },
                { to: "/servers/copy", icon: <FolderSync className="size-4" />, label: t("nav.srv_copy") },
              ].map((l) => (
                <Link key={l.to} to={l.to} className="flex items-center gap-2 rounded-md px-3 py-2 text-sm font-semibold hover:bg-sunken">{l.icon}{l.label}<ArrowRight className="ml-auto size-4 text-muted" /></Link>
              ))}
            </CardBody>
          </Card>
        </div>
      </div>
      <AddServerDialog open={add} onOpenChange={setAdd} role={role} customerId={customerId || env || undefined} />
    </>
  );
}

export function ClaudeCard({ server }: { server?: NodeInfo }) {
  const { t } = useTranslation();
  const examples = server
    ? [t("srv.ask_s1", { name: server.name }), t(server.kind === "windows" ? "srv.ask_s2_win" : "srv.ask_s2", { name: server.name }), t("srv.ask_s3", { name: server.name })]
    : [t("srv.ask_1"), t("srv.ask_2"), t("srv.ask_3"), t("srv.ask_4")];
  return (
    <Card className="bg-nacht text-white dark:bg-raised">
      <CardBody className="space-y-3 py-5">
        <div className="flex items-center gap-2 text-lucht"><Bot className="size-5" /><span className="text-sm font-bold uppercase tracking-wider">{t("srv.claude_title")}</span></div>
        <div className="space-y-2">
          {examples.map((e) => (
            <div key={e} className="flex items-start gap-2 rounded-md bg-white/10 px-3 py-2 text-sm dark:bg-sunken">
              <MessageSquare className="mt-0.5 size-3.5 shrink-0 text-koraal" /><span className="flex-1 italic">“{e}”</span><CopyButton value={e} />
            </div>
          ))}
        </div>
        <p className="text-xs text-white/70 dark:text-muted">{t("srv.claude_note")}</p>
        <Link to="/help/capabilities" className="inline-flex items-center gap-1.5 text-sm font-semibold text-koraal hover:underline">{t("srv.claude_more")} <ArrowRight className="size-4" /></Link>
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ add a server */
export function AddServerDialog({ open, onOpenChange, role: initialRole = "server", customerId }: {
  open: boolean; onOpenChange: (v: boolean) => void; role?: "server" | "workstation"; customerId?: string;
}) {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const qc = useQueryClient();
  const [env, setEnv] = useState("");
  const [role, setRole] = useState<"server" | "workstation">(initialRole);
  const [label, setLabel] = useState("");
  const [os, setOs] = useState<"linux" | "windows">("linux");
  const [result, setResult] = useState<(Pairing & { connect: ConnectInfo }) | null>(null);
  useEffect(() => {
    if (open) {
      setResult(null);
      setLabel("");
      setRole(initialRole);
      setEnv((e) => customerId || e || envs.data?.[0]?.id || "");
    }
  }, [open, envs.data, customerId, initialRole]);
  const create = useMutation({
    mutationFn: () => post<Pairing & { connect: ConnectInfo }>("api/pairing", { environment_id: env, label: label || null, role }),
    onSuccess: (r) => { setResult(r); qc.invalidateQueries({ queryKey: ["pairing"] }); },
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide title={t("srv.add_title")} description={t("srv.add_desc")}
      footer={result ? <Button variant="primary" onClick={() => onOpenChange(false)}>{t("common.done")}</Button> : <>
        <Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={create.isPending} disabled={!env} onClick={() => create.mutate()}><KeyRound /> {t("px.pair_create")}</Button>
      </>}>
      <div className="space-y-5">
        <Segmented value={os} onChange={setOs} options={[{ value: "linux", label: <span className="flex items-center gap-1.5"><Terminal className="size-4" /> Linux</span> }, { value: "windows", label: <span className="flex items-center gap-1.5"><AppWindow className="size-4" /> Windows</span> }]} />
        {!result ? (
          <div className="space-y-4">
            {create.isError && <Alert tone="offline">{errorMessage(create.error, t)}</Alert>}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("px.environment")}>
                <Select value={env} onChange={(e) => setEnv(e.target.value)}>{envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}</Select>
              </Field>
              <Field label={t("px.pair_label")} hint={t("px.pair_label_hint")}><Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={os === "windows" ? "dc01" : "www1"} /></Field>
            </div>
            <Field label={t("cust.machine_role")} hint={t("cust.machine_role_hint")}>
              <Segmented value={role} onChange={(v) => setRole(v as "server" | "workstation")} options={[{ value: "server", label: t("cust.role_server") }, { value: "workstation", label: t("cust.role_workstation") }]} />
            </Field>
            <Alert tone="info">{t(os === "windows" ? "srv.add_how_win" : "srv.add_how_linux")}</Alert>
          </div>
        ) : (
          <div className="space-y-5">
            {!result.connect.packages_available && <Alert tone="warning">{t("px.no_packages")}</Alert>}
            <AgentAddressNotice info={result.connect} onNavigate={() => onOpenChange(false)} />
            <div className="flex flex-wrap items-center gap-3 rounded-md bg-sunken px-4 py-3">
              <div className="flex-1">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("px.pair_code")}</div>
                <div className="mono text-2xl font-bold tracking-[0.12em]">{result.code}</div>
              </div>
              <div className="text-right text-xs text-muted">{t("px.pair_valid")}<br />{new Date(result.expires_at).toLocaleString()}</div>
              <CopyButton value={result.code} />
            </div>
            {os === "linux" ? (
              <>
                <div>
                  <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><Terminal className="size-4" /> {t("srv.run_linux")}</div>
                  <CodeBlock code={result.command ?? ""} />
                  <p className="mt-2 text-xs text-muted">{t("srv.linux_note")}</p>
                </div>
                <details className="rounded-md border border-line p-4">
                  <summary className="cursor-pointer text-sm font-semibold">{t("px.pair_manual")}</summary>
                  <p className="my-3 text-sm text-muted">{t("px.pair_manual_text")}</p>
                  <CodeBlock code={result.manual ?? ""} />
                </details>
              </>
            ) : (
              <>
                <div>
                  <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><AppWindow className="size-4" /> {t("srv.run_win")}</div>
                  <CodeBlock code={result.windows_command ?? ""} />
                  <p className="mt-2 text-xs text-muted">{t("srv.win_note")}</p>
                </div>
                <details className="rounded-md border border-line p-4">
                  <summary className="cursor-pointer text-sm font-semibold">{t("srv.win_msiexec")}</summary>
                  <p className="my-3 text-sm text-muted">{t("srv.win_msiexec_text")}</p>
                  <CodeBlock code={result.msiexec ?? ""} />
                </details>
              </>
            )}
            <div className="grid gap-2 text-xs text-muted sm:grid-cols-2">
              <div><span className="font-semibold text-ink">{t("px.endpoint")}:</span> <span className="mono">{result.connect.endpoint}</span></div>
              <div className="break-all"><span className="font-semibold text-ink">{t("px.fingerprint")}:</span> <span className="mono">{result.connect.fingerprint.slice(0, 16)}…</span></div>
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ choosing machines */
interface Targets { mode: "all" | "env" | "pick"; environment: string; os: string; servers: string[] }

function useAllMachines() {
  return useQuery({ queryKey: ["nodes", "all"], queryFn: () => get<NodeInfo[]>("api/nodes"), refetchInterval: 60_000 });
}

function targetBody(tg: Targets) {
  return {
    servers: tg.mode === "pick" ? tg.servers : null,
    environment: tg.mode === "env" ? tg.environment || null : null,
    os: tg.mode !== "pick" && tg.os !== "all" ? tg.os : null,
  };
}

function TargetPicker({ value, onChange }: { value: Targets; onChange: (v: Targets) => void }) {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const machines = useAllMachines();
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented value={value.mode} onChange={(mode) => onChange({ ...value, mode })} options={[
          { value: "all", label: t("srv.t_all") }, { value: "env", label: t("srv.t_env") }, { value: "pick", label: t("srv.t_pick") }]} />
        {value.mode === "env" && (
          <Select value={value.environment} onChange={(e) => onChange({ ...value, environment: e.target.value })} className="h-9 w-auto py-0 text-[13px]">
            <option value="">–</option>
            {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
          </Select>
        )}
        {value.mode !== "pick" && (
          <Select value={value.os} onChange={(e) => onChange({ ...value, os: e.target.value })} className="h-9 w-auto py-0 text-[13px]">
            <option value="all">{t("srv.t_os_all")}</option>
            <option value="linux">Linux</option>
            <option value="windows">Windows</option>
            <option value="proxmox">Proxmox VE</option>
          </Select>
        )}
      </div>
      {value.mode === "pick" && (
        <div className="grid max-h-48 gap-2 overflow-y-auto rounded-md border border-line p-3 sm:grid-cols-2 lg:grid-cols-3">
          {machines.data?.map((n) => (
            <Checkbox key={n.id} checked={value.servers.includes(n.name)} disabled={!n.online}
              onCheckedChange={(v) => onChange({ ...value, servers: v ? [...value.servers, n.name] : value.servers.filter((s) => s !== n.name) })}
              label={<span className="flex items-center gap-2"><StatusDot online={n.online} />{n.name}</span>} description={`${n.environment_name ?? ""} · ${n.kind}`} />
          ))}
        </div>
      )}
    </div>
  );
}

function RowHead({ r, children }: { r: FanOutRow<unknown>; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
      <StatusDot online={r.online} />
      <Link to={r.kind === "proxmox" ? `/proxmox/nodes/${r.node_id}` : `/servers/${r.node_id}`} className="font-bold hover:underline">{r.server}</Link>
      <OsBadge kind={r.kind} />
      <span className="text-xs text-muted">{r.environment}</span>
      <span className="ml-auto flex items-center gap-2">{children}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ log search + errors */
export function LogSearchPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState("search");
  const [tg, setTg] = useState<Targets>({ mode: "all", environment: "", os: "all", servers: [] });
  const [f, setF] = useState({ pattern: "", since: "24 hours ago", source: "auto", priority: "", unit: "", paths: "" });
  const [since, setSince] = useState("1 hour ago");
  const search = useMutation({
    mutationFn: () => post<FanOutRow<LogSearchResult>[]>("api/servers/search-logs", {
      ...targetBody(tg), pattern: f.pattern, since: f.since, source: f.source, priority: f.priority || null, unit: f.unit || null,
      paths: f.source === "files" && f.paths.trim() ? f.paths.split(/[\n,]+/).map((p) => p.trim()).filter(Boolean) : null,
    }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const errors = useMutation({
    mutationFn: () => post<FanOutRow<ErrorsResult>[]>("api/servers/errors", { ...targetBody(tg), since }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <>
      <PageHeader title={t("srv.logs_title")} description={t("srv.logs_subtitle")} />
      <Card className="mb-6">
        <CardHeader icon={<Monitor />} title={t("srv.targets")} />
        <CardBody><TargetPicker value={tg} onChange={setTg} /></CardBody>
      </Card>
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="search" className={tabCls}>{t("srv.tab_search")}</Tabs.Trigger>
          <Tabs.Trigger value="errors" className={tabCls}>{t("srv.tab_errors")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="search" className="space-y-6">
          <Card>
            <CardBody>
              <form className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6" onSubmit={(e) => { e.preventDefault(); if (f.pattern.trim()) search.mutate(); }}>
                <Field label={t("srv.pattern")} className="xl:col-span-2"><Input value={f.pattern} onChange={(e) => setF({ ...f, pattern: e.target.value })} placeholder="error|timed out|reject" autoFocus /></Field>
                <Field label={t("px.since")}><Input value={f.since} onChange={(e) => setF({ ...f, since: e.target.value })} /></Field>
                <Field label={t("srv.source")}>
                  <Select value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>
                    {["auto", "journal", "eventlog", "files"].map((s) => <option key={s} value={s}>{t(`srv.src_${s}`)}</option>)}
                  </Select>
                </Field>
                <Field label={t("px.priority")}>
                  <Select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })} disabled={f.source === "files"}>
                    <option value="">{t("px.all")}</option>
                    {["crit", "err", "warning", "info"].map((p) => <option key={p} value={p}>{t(`srv.level_${p}`)}</option>)}
                  </Select>
                </Field>
                <div className="flex items-end"><Button type="submit" variant="dark" className="w-full" loading={search.isPending} disabled={!f.pattern.trim()}><Search /> {t("srv.search")}</Button></div>
                {f.source === "files"
                  ? <Field label={t("srv.paths")} hint={t("srv.paths_hint")} className="sm:col-span-2 xl:col-span-6"><Input className="mono" value={f.paths} onChange={(e) => setF({ ...f, paths: e.target.value })} placeholder="/var/log/nginx/*.log, /var/log/mail.log" /></Field>
                  : <Field label={t("srv.unit")} hint={t("srv.unit_hint")} className="sm:col-span-2"><Input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} placeholder="postfix, nginx, Service Control Manager" /></Field>}
              </form>
            </CardBody>
          </Card>
          {search.isPending && <Spinner />}
          {search.data?.map((r) => {
            const res = r.result;
            const n = res ? (res.lines?.length ?? res.events?.length ?? res.matches?.length ?? 0) : 0;
            return (
              <Card key={r.node_id}>
                <RowHead r={r}>{r.error ? <Badge tone="offline">{r.error}</Badge> : <Badge tone={n ? "accent" : "neutral"}>{t("srv.hits", { count: n })}</Badge>}</RowHead>
                {res && n > 0 && (
                  <CardBody>
                    {res.lines && <Output text={res.lines.join("\n")} />}
                    {res.events && <Output text={res.events.map((e) => `${e.time} ${e.log} ${e.level} ${e.provider}[${e.id}]: ${e.message}${e.expected_restart ? `  (${t("srv.expectedRestart", { version: e.expected_restart })})` : ""}`).join("\n")} />}
                    {res.matches && <Output text={res.matches.map((m) => `${m.file}:${m.line}: ${m.text}`).join("\n")} />}
                    {res.truncated && <p className="mt-2 text-xs text-muted">{t("srv.truncated")}</p>}
                  </CardBody>
                )}
              </Card>
            );
          })}
        </Tabs.Content>
        <Tabs.Content value="errors" className="space-y-6">
          <Card>
            <CardBody>
              <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); errors.mutate(); }}>
                <Field label={t("px.since")}><Input value={since} onChange={(e) => setSince(e.target.value)} /></Field>
                <Button type="submit" variant="dark" loading={errors.isPending}><Play /> {t("srv.find_errors")}</Button>
                <p className="text-sm text-muted">{t("srv.errors_desc")}</p>
              </form>
            </CardBody>
          </Card>
          {errors.data?.map((r) => {
            const res = r.result;
            const bad = !!(res && (res.failed_services?.length || res.error_count || res.findings.length));
            return (
              <Card key={r.node_id}>
                <RowHead r={r}>
                  {r.error ? <Badge tone="offline">{r.error}</Badge> : !bad ? <Badge tone="online" dot>{t("srv.healthy")}</Badge> : <>
                    {!!res?.failed_services?.length && <Badge tone="offline">{t("srv.failed_n", { count: res.failed_services.length })}</Badge>}
                    {!!res?.error_count && <Badge tone="warning">{t("srv.errors_n", { count: res.error_count })}</Badge>}
                  </>}
                </RowHead>
                {res && bad && (
                  <CardBody className="space-y-3">
                    {(res.findings.length > 0 || !!res.failed_services?.length) && (
                      <div className="flex flex-wrap gap-2">
                        {res.failed_services?.map((s) => <Badge key={s} tone="offline">{s}</Badge>)}
                        {res.findings.filter((x) => x !== "failed_services").map((x) => <Badge key={x} tone="warning">{t(`srv.att_${x}`, { defaultValue: x })}</Badge>)}
                      </div>
                    )}
                    {!!res.recent_errors?.length && <Output text={res.recent_errors.join("\n")} />}
                  </CardBody>
                )}
              </Card>
            );
          })}
        </Tabs.Content>
      </Tabs.Root>
    </>
  );
}

/* ------------------------------------------------------------------ software + CVE */
export function SoftwarePage() {
  const { t } = useTranslation();
  const [tg, setTg] = useState<Targets>({ mode: "all", environment: "", os: "all", servers: [] });
  const [pkg, setPkg] = useState("");
  const [fixed, setFixed] = useState("");
  const check = useMutation({
    mutationFn: () => post<FanOutRow<CveResult>[]>("api/servers/cve-check", { ...targetBody(tg), package: pkg.trim(), fixed_version: fixed.trim() || null }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rows = check.data ?? [];
  const vuln = rows.reduce((a, r) => a + (r.result?.vulnerable ?? 0), 0);
  const withPkg = rows.filter((r) => r.result?.installed.length).length;
  return (
    <>
      <PageHeader title={t("srv.sw_title")} description={t("srv.sw_subtitle")} />
      <Card className="mb-6">
        <CardBody className="space-y-4">
          <TargetPicker value={tg} onChange={setTg} />
          <form className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]" onSubmit={(e) => { e.preventDefault(); if (pkg.trim()) check.mutate(); }}>
            <Field label={t("srv.sw_package")} hint={t("srv.sw_package_hint")}><Input value={pkg} onChange={(e) => setPkg(e.target.value)} placeholder="nginx*, openssl, *7-zip*, KB5*" /></Field>
            <Field label={t("srv.sw_fixed")} hint={t("srv.sw_fixed_hint")}><Input className="mono" value={fixed} onChange={(e) => setFixed(e.target.value)} placeholder="1.26.3-3" /></Field>
            <div className="flex items-end pb-5"><Button type="submit" variant="dark" loading={check.isPending} disabled={!pkg.trim()}><ShieldAlert /> {t("srv.sw_check")}</Button></div>
          </form>
        </CardBody>
      </Card>
      {check.data && (
        <div className="mb-6 grid gap-4 sm:grid-cols-3">
          <Stat label={t("srv.sw_machines")} value={rows.length} icon={<Monitor />} tone="info" />
          <Stat label={t("srv.sw_with_pkg")} value={withPkg} icon={<Search />} tone="neutral" />
          <Stat label={t("srv.sw_vulnerable")} value={fixed ? vuln : "–"} hint={fixed ? undefined : t("srv.sw_no_fixed")} icon={<ShieldAlert />} tone={vuln ? "offline" : "online"} />
        </div>
      )}
      {rows.length > 0 && (
        <Card>
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("srv.col_server")}</Th><Th>{t("px.package")}</Th><Th>{t("px.installed")}</Th><Th>{t("srv.sw_status")}</Th><Th>{t("px.new_version")}</Th></tr></thead>
              <tbody>
                {rows.flatMap((r) => {
                  const head = <span className="flex items-center gap-2"><StatusDot online={r.online} /><Link to={r.kind === "proxmox" ? `/proxmox/nodes/${r.node_id}` : `/servers/${r.node_id}`} className="font-semibold hover:underline">{r.server}</Link></span>;
                  if (r.error || !r.result?.installed.length) {
                    return [<Tr key={r.node_id}><Td>{head}</Td><Td colSpan={4} className="text-sm text-muted">{r.error ?? t("srv.sw_not_installed")}</Td></Tr>];
                  }
                  return r.result.installed.map((p, i) => (
                    <Tr key={`${r.node_id}-${p.name}-${i}`}>
                      <Td>{i === 0 ? head : null}</Td>
                      <Td className="mono text-[12.5px] font-semibold">{p.name}</Td>
                      <Td className="mono text-[12.5px]">{p.version}</Td>
                      <Td>{p.status === "vulnerable" ? <Badge tone="offline" dot>{t("srv.sw_vuln")}</Badge> : p.status === "fixed" ? <Badge tone="online" dot>{t("srv.sw_ok")}</Badge> : <span className="text-muted">–</span>}</Td>
                      <Td className="mono text-[12.5px]">{p.update_available ? <>{p.update_available}{p.fixed_by_pending_update && <Badge tone="info" className="ml-2">{t("srv.sw_fixes")}</Badge>}</> : "–"}</Td>
                    </Tr>
                  ));
                })}
              </tbody>
            </Table>
          </div>
        </Card>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ data copies */
export function CopyPage() {
  const { t, i18n } = useTranslation();
  const machines = useAllMachines();
  const qc = useQueryClient();
  const [f, setF] = useState({ source: "", paths: "", target: "", target_path: "", overwrite: false, reason: "" });
  const [open, setOpen] = useState<Transfer | null>(null);
  const list = useQuery({ queryKey: ["transfers"], queryFn: () => get<Transfer[]>("api/transfers?limit=50"), refetchInterval: (q) => (q.state.data?.some((x) => x.status === "running") ? 3000 : 30_000) });
  const online = useMemo(() => (machines.data ?? []).filter((n) => n.online), [machines.data]);
  const src = online.find((n) => n.name === f.source);
  const dst = online.find((n) => n.name === f.target);
  const start = useMutation({
    mutationFn: () => post<Transfer>("api/transfers", { ...f, paths: f.paths.split(/\n+/).map((p) => p.trim()).filter(Boolean), reason: f.reason || null }),
    onSuccess: (tr) => { toast.success(t("srv.copy_started")); setOpen(tr); qc.invalidateQueries({ queryKey: ["transfers"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const current = open ? list.data?.find((x) => x.id === open.id) ?? open : null;
  return (
    <>
      <PageHeader title={t("srv.copy_title")} description={t("srv.copy_subtitle")} />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Card>
          <CardHeader icon={<FolderSync />} title={t("srv.copy_new")} />
          <CardBody>
            <form className="grid gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); start.mutate(); }}>
              <div className="space-y-3">
                <Field label={t("srv.copy_from")}>
                  <Select value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>
                    <option value="">–</option>
                    {online.map((n) => <option key={n.id} value={n.name}>{n.name} ({n.environment_name})</option>)}
                  </Select>
                </Field>
                <Field label={t("srv.copy_paths")} hint={t("srv.copy_paths_hint")}>
                  <textarea className="mono min-h-24 w-full rounded-md border border-line bg-raised px-3 py-2 text-[13px]" value={f.paths}
                    onChange={(e) => setF({ ...f, paths: e.target.value })} placeholder={src?.kind === "windows" ? "D:\\Shares\\Finance" : "/var/www/site\n/etc/nginx"} />
                </Field>
              </div>
              <div className="space-y-3">
                <Field label={t("srv.copy_to")}>
                  <Select value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })}>
                    <option value="">–</option>
                    {online.map((n) => <option key={n.id} value={n.name}>{n.name} ({n.environment_name})</option>)}
                  </Select>
                </Field>
                <Field label={t("srv.copy_dest")} hint={t("srv.copy_dest_hint")}>
                  <Input className="mono" value={f.target_path} onChange={(e) => setF({ ...f, target_path: e.target.value })} placeholder={dst?.kind === "windows" ? "D:\\Restore" : "/srv/restore"} />
                </Field>
                <Switch checked={f.overwrite} onCheckedChange={(v) => setF({ ...f, overwrite: v })} label={t("srv.copy_overwrite")} description={t("srv.copy_overwrite_hint")} />
              </div>
              <Field label={t("srv.copy_reason")} className="md:col-span-2"><Input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} placeholder={t("srv.copy_reason_ph")} /></Field>
              <div className="flex items-center gap-3 md:col-span-2">
                <Button type="submit" variant="primary" loading={start.isPending} disabled={!f.source || !f.target || !f.paths.trim() || !f.target_path.trim()}><Copy /> {t("srv.copy_start")}</Button>
                <p className="text-xs text-muted">{t("srv.copy_note")}</p>
              </div>
            </form>
          </CardBody>
        </Card>
        <Alert tone="info" title={t("srv.copy_how_title")}>{t("srv.copy_how")}</Alert>
      </div>
      <Card className="mt-6">
        <CardHeader icon={<FolderSync />} title={t("srv.copy_recent")} />
        {!list.data?.length ? <EmptyState icon={<FolderSync />} title={t("srv.copy_empty")} /> : (
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("srv.copy_from")}</Th><Th>{t("srv.copy_to")}</Th><Th>{t("px.status")}</Th><Th>{t("srv.copy_size")}</Th><Th>{t("px.by")}</Th><Th>{t("px.started")}</Th><Th /></tr></thead>
              <tbody>
                {list.data.map((x) => (
                  <Tr key={x.id}>
                    <Td><div className="font-semibold">{x.source_server}</div><div className="mono max-w-64 truncate text-[11.5px] text-muted">{x.source_paths.join(", ")}</div></Td>
                    <Td><div className="font-semibold">{x.target_server}</div><div className="mono max-w-64 truncate text-[11.5px] text-muted">{x.target_path}</div></Td>
                    <Td><JobBadge status={x.status} />{x.status === "running" && <span className="ml-2 text-xs text-muted">{t(`srv.stage_${x.stage}`)}</span>}</Td>
                    <Td className="text-sm">{x.bytes != null ? `${formatBytes(x.bytes)} · ${x.files ?? 0} ${t("srv.files")}` : "–"}</Td>
                    <Td className="text-sm">{x.requested_by}{x.source === "mcp" && <Badge tone="info" className="ml-2"><Bot className="size-3" /> MCP</Badge>}</Td>
                    <Td className="text-sm text-muted">{timeAgo(x.started_at, i18n.language)}</Td>
                    <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => setOpen(x)}>{t("px.details")}</Button></Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      <Dialog open={!!current} onOpenChange={(v) => !v && setOpen(null)} wide title={current ? `${current.source_server} → ${current.target_server}` : ""}>
        {current && (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap items-center gap-2"><JobBadge status={current.status} />{current.reason && <span className="text-muted">{current.reason}</span>}</div>
            {current.error && <Alert tone="offline">{current.error}</Alert>}
            {current.send_job_id && <div><div className="mb-1 font-semibold">{t("srv.copy_send_job", { name: current.source_server })}</div><JobView jobId={current.send_job_id} /></div>}
            {current.receive_job_id && <div><div className="mb-1 font-semibold">{t("srv.copy_receive_job", { name: current.target_server })}</div><JobView jobId={current.receive_job_id} /></div>}
            {!current.send_job_id && <Spinner />}
          </div>
        )}
      </Dialog>
    </>
  );
}
