import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, CheckCircle2, Clock, Globe, Lock, Pencil, Play, Plus, Radar, Settings2, ShieldCheck, Target, Trash2, XCircle, Zap,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
type Kind = "http" | "tcp" | "ping" | "dns" | "vpn";
type Status = "up" | "down" | "degraded" | "unknown" | "paused";
interface CheckConfig {
  method?: "GET" | "HEAD"; expect_status?: string; keyword?: string; keyword_absent?: boolean; verify_tls?: boolean;
  follow_redirects?: boolean; record_type?: string; expect?: string; server?: string; mode?: "ssl" | "tunnel";
}
interface Check {
  id: string; customer_id: string; customer_name: string | null; name: string; kind: Kind; target: string; port: number | null;
  label: string; config: CheckConfig; probe_node_id: string | null; probe_name: string | null; node_id: string | null;
  node_name: string | null; enabled: boolean; interval_seconds: number; timeout_seconds: number; fail_after: number;
  slow_ms: number | null; cert_warn_days: number; severity: "critical" | "warning"; in_sla: boolean; status: Status;
  status_since: string | null; failures: number; last_check_at: string | null; last_ms: number | null; last_error: string | null;
  last_code: number | null; cert_expires_at: string | null; cert_days: number | null;
  uptime_24h?: number | null; uptime_7d?: number | null; uptime_30d?: number | null; avg_ms_24h?: number | null;
}
interface ListOut { checks: Check[]; counts: Partial<Record<Status, number>>; kinds: Kind[]; record_types: string[] }
interface Incident { id: string; check_id: string; check: string; started_at: string; ended_at: string | null; duration_minutes: number; error: string | null; open: boolean }
interface Point { at: string; ms: number | null; ms_max: number | null; ok: number; failed: number }
interface Detail extends Check {
  history: { range: string; bucket_seconds: number; points: Point[]; availability: number | null; down_seconds: number | null;
    recent: { at: string; ok: boolean | null; ms: number | null; code: number | null; error: string | null }[]; incidents: Incident[] };
}
interface TestResult { ok: boolean | null; ms: number | null; code: number | null; error: string | null; cert_expires_at: string | null; values?: string[]; probe?: string }
interface Probe { id: string; name: string; kind: string; online: boolean }
interface SlaSettings { target_pct: number | null; target_source: "setting" | "sla_text" | null; exclude_maintenance: boolean; alert_breach: boolean; recipients: string[] }
interface SlaCheck { id: string; name: string; kind: Kind; target: string; in_sla: boolean; status: Status; availability: number | null; downtime_minutes: number; incidents: number }
interface SlaCustomer {
  customer_id: string; customer: string; availability: number | null; target_pct: number | null; target_source: string | null;
  met: boolean | null; allowed_downtime_minutes: number | null; downtime_minutes: number; exclude_maintenance: boolean;
  maintenance_window: string | null; maintenance_understood: boolean | null; checks: SlaCheck[]; incidents?: Incident[]; settings: SlaSettings;
}

const TONE: Record<Status, "online" | "offline" | "warning" | "neutral" | "info"> = {
  up: "online", down: "offline", degraded: "warning", unknown: "neutral", paused: "neutral",
};
const KINDS: Kind[] = ["http", "tcp", "ping", "dns", "vpn"];
const RECORD_TYPES = ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "SOA", "CAA", "SRV", "PTR"];

// Rounded down, so a little downtime never shows as 100%.
const pct = (v: number | null | undefined, digits = 2) => {
  if (v == null) return "–";
  const f = 10 ** digits;
  return `${(Math.floor(v * f) / f).toFixed(v >= 100 ? 0 : digits)}%`;
};
const ms = (v: number | null | undefined) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const pctTone = (v: number | null | undefined, target?: number | null) =>
  v == null ? "neutral" : v >= (target ?? 99.5) ? "online" : v >= 98 ? "warning" : "offline";

function qs(path: string, params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => { for (const k of ["uptime-checks", "uptime-check", "uptime-sla"]) qc.invalidateQueries({ queryKey: [k] }); };
}

function StatusBadge({ s }: { s: Status }) {
  const { t } = useTranslation();
  return <Badge tone={TONE[s]} dot>{t(`up.status.${s}`)}</Badge>;
}

/* ------------------------------------------------------------------ page */
export function UptimePage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { customerId } = useCustomer();
  const [tab, setTab] = useState("checks");
  const [edit, setEdit] = useState<Check | "new" | null>(null);
  const q = useQuery({
    queryKey: ["uptime-checks", customerId],
    queryFn: () => get<ListOut>(qs("api/uptime/checks", { customer_id: customerId })),
    refetchInterval: 30_000,
  });
  const sla = useQuery({ queryKey: ["uptime-sla", customerId, ""], queryFn: () => get<{ customers: SlaCustomer[] }>(qs("api/uptime/sla", {})) });
  const counts = q.data?.counts ?? {};
  const withChecks = (sla.data?.customers ?? []).filter((c) => c.checks.length);
  const missed = withChecks.filter((c) => c.met === false).length;
  const avg = withChecks.length && withChecks.some((c) => c.availability != null)
    ? withChecks.reduce((a, c) => a + (c.availability ?? 0), 0) / withChecks.filter((c) => c.availability != null).length : null;

  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><Radar className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("up.title")}</span></span>}
        description={t("up.desc")}
        actions={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("up.add")}</Button>} />
      <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("up.k_up")} value={(counts.up ?? 0) + (counts.degraded ?? 0)} hint={counts.degraded ? t("up.k_slow", { count: counts.degraded }) : t("up.k_up_hint")} tone="online" icon={<CheckCircle2 />} />
        <Stat label={t("up.k_down")} value={counts.down ?? 0} hint={t("up.k_down_hint")} tone={counts.down ? "offline" : "neutral"} icon={<XCircle />} />
        <Stat label={t("up.k_sla")} value={pct(avg)} hint={t("up.k_sla_hint")} tone="info" icon={<Target />} />
        <Stat label={t("up.k_missed")} value={missed} hint={t("up.k_missed_hint")} tone={missed ? "warning" : "neutral"} icon={<ShieldCheck />} />
      </div>
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="checks" className={tabCls}>{t("up.tab_checks")}</Tabs.Trigger>
          <Tabs.Trigger value="sla" className={tabCls}>{t("up.tab_sla")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="checks">
          {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : (
            <ChecksList checks={q.data?.checks ?? []} admin={admin} onEdit={setEdit} onAdd={() => setEdit("new")} />
          )}
        </Tabs.Content>
        <Tabs.Content value="sla"><SlaTab admin={admin} /></Tabs.Content>
      </Tabs.Root>
      {admin && edit && <CheckDialog check={edit === "new" ? null : edit} onClose={() => setEdit(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ checks */
function ChecksList({ checks, admin, onEdit, onAdd }: { checks: Check[]; admin: boolean; onEdit: (c: Check) => void; onAdd: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "problems">("all");
  const run = useMutation({
    mutationFn: (c: Check) => post<TestResult & { status: Status }>(`api/uptime/checks/${c.id}/run`),
    onSuccess: (r) => { invalidate(); (r.ok ? toast.success : toast.error)(r.ok ? t("up.run_ok", { ms: ms(r.ms) }) : t("up.run_failed", { error: r.error ?? "–" })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: (c: Check) => del(`api/uptime/checks/${c.id}`),
    onSuccess: () => { invalidate(); toast.success(t("up.deleted")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const shown = filter === "all" ? checks : checks.filter((c) => c.status === "down" || c.status === "degraded" || (c.cert_days != null && c.cert_days <= c.cert_warn_days));
  return (
    <Card>
      <CardHeader icon={<Activity />} title={t("up.checks_title")} description={t("up.checks_desc")}
        action={<Segmented value={filter} onChange={setFilter} options={[{ value: "all", label: t("up.f_all") }, { value: "problems", label: t("up.f_problems") }]} />} />
      {!checks.length ? (
        <EmptyState icon={<Radar />} title={t("up.empty")} action={admin && <Button variant="primary" onClick={onAdd}><Plus /> {t("up.add")}</Button>}>{t("up.empty_hint")}</EmptyState>
      ) : !shown.length ? (
        <EmptyState icon={<CheckCircle2 />} title={t("up.no_problems")} />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <thead><Tr><Th>{t("up.col_status")}</Th><Th>{t("up.col_check")}</Th><Th>{t("up.col_response")}</Th><Th>24h</Th><Th>7d</Th><Th>30d</Th><Th>{t("up.col_last")}</Th>{admin && <Th />}</Tr></thead>
            <tbody>
              {shown.map((c) => (
                <Tr key={c.id} className="cursor-pointer" onClick={() => setOpen(c.id)}>
                  <Td><StatusBadge s={c.status} />{c.status === "down" && c.status_since && <div className="mt-1 text-xs text-muted">{t("up.since", { when: timeAgo(c.status_since, i18n.language) })}</div>}</Td>
                  <Td className="max-w-md">
                    <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{c.name}</span><Badge tone="neutral">{t(`up.kind.${c.kind}`)}</Badge>{!c.in_sla && <Badge tone="neutral">{t("up.not_in_sla")}</Badge>}</div>
                    <div className="mono truncate text-xs text-muted" title={c.label}>{c.label}</div>
                    <div className="text-xs text-muted">{c.customer_name}{c.probe_name ? ` · ${t("up.via", { probe: c.probe_name })}` : ""}</div>
                    {c.last_error && c.status !== "up" && <div className="mt-1 text-xs text-offline">{c.last_error}</div>}
                    {c.cert_days != null && c.cert_days <= c.cert_warn_days && <div className="mt-1 flex items-center gap-1 text-xs text-warning"><Lock className="size-3" />{c.cert_days < 0 ? t("up.cert_expired") : t("up.cert_expires", { count: c.cert_days })}</div>}
                  </Td>
                  <Td className="whitespace-nowrap"><div className="font-semibold">{ms(c.last_ms)}</div><div className="text-xs text-muted">{t("up.avg", { ms: ms(c.avg_ms_24h) })}</div></Td>
                  {(["uptime_24h", "uptime_7d", "uptime_30d"] as const).map((k) => <Td key={k}><Badge tone={pctTone(c[k])}>{pct(c[k], c[k] === 100 ? 0 : 2)}</Badge></Td>)}
                  <Td className="whitespace-nowrap text-xs text-muted" title={c.last_check_at ? formatDate(c.last_check_at, i18n.language) : ""}>{c.last_check_at ? timeAgo(c.last_check_at, i18n.language) : "–"}</Td>
                  {admin && (
                    <Td onClick={(e) => e.stopPropagation()}>
                      <div className="flex justify-end gap-1">
                        <Button size="icon" variant="ghost" title={t("up.run_now")} loading={run.isPending && run.variables?.id === c.id} onClick={() => run.mutate(c)}>{!(run.isPending && run.variables?.id === c.id) && <Play />}</Button>
                        <Button size="icon" variant="ghost" title={t("common.edit")} onClick={() => onEdit(c)}><Pencil /></Button>
                        <Button size="icon" variant="ghost" title={t("common.delete")} onClick={() => { if (window.confirm(t("up.confirm_delete", { name: c.name }))) remove.mutate(c); }}><Trash2 /></Button>
                      </div>
                    </Td>
                  )}
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      {open && <DetailDialog id={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}

/* ------------------------------------------------------------------ detail */
function ResponseChart({ points, bucket }: { points: Point[]; bucket: number }) {
  const { t, i18n } = useTranslation();
  const W = 1000, H = 190, L = 56, R = 8, TOP = 10, BAR = 10, GAP = 12;
  if (!points.length) return <div className="py-10 text-center text-sm text-muted">{t("up.no_data")}</div>;
  const top = Math.max(50, ...points.map((p) => p.ms ?? 0)) * 1.15;
  const plotH = H - TOP - BAR - GAP;
  const n = points.length;
  const x = (i: number) => L + (n === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (n - 1));
  const y = (v: number) => TOP + plotH - (v * plotH) / top;
  const line = points.map((p, i) => (p.ms == null ? null : `${x(i).toFixed(1)},${y(p.ms).toFixed(1)}`)).filter(Boolean).join(" ");
  const area = line ? `${L},${TOP + plotH} ${line} ${x(n - 1).toFixed(1)},${TOP + plotH}` : "";
  const bw = Math.max(2, (W - L - R) / n - 1.5);
  const ticks = [0, 0.25, 0.5, 0.75].map((f) => (top / 1.15) * (f + 0.25));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={t("up.chart_label")}>
      <defs>
        <linearGradient id="up-fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="var(--koraal)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--koraal)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {ticks.map((v) => (
        <g key={v}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeDasharray="3 5" />
          <text x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="12" fill="var(--ink-muted)">{ms(v)}</text>
        </g>
      ))}
      {area && <polygon points={area} fill="url(#up-fill)" />}
      {line && <polyline points={line} fill="none" stroke="var(--koraal)" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />}
      {points.map((p, i) => {
        const tone = p.ok + p.failed === 0 ? "var(--line)" : p.failed === 0 ? "var(--status-online)" : p.failed >= p.ok ? "var(--status-offline)" : "var(--status-warning)";
        return (
          <rect key={p.at} x={x(i) - bw / 2} y={H - BAR} width={bw} height={BAR} rx={2} fill={tone}>
            <title>{`${formatDate(p.at, i18n.language)} · ${t("up.bucket_line", { ok: p.ok, failed: p.failed, ms: ms(p.ms), minutes: Math.round(bucket / 60) })}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function DetailDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [range, setRange] = useState<"24h" | "7d" | "30d">("24h");
  const q = useQuery({ queryKey: ["uptime-check", id, range], queryFn: () => get<Detail>(qs(`api/uptime/checks/${id}`, { range })), refetchInterval: 30_000 });
  const d = q.data;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} xl title={d ? d.name : t("up.loading")} description={d ? `${t(`up.kind.${d.kind}`)} · ${d.label}` : undefined}>
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : d && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label={t("up.col_status")} value={<StatusBadge s={d.status} />} hint={d.status_since ? t("up.since", { when: timeAgo(d.status_since, i18n.language) }) : undefined} />
            <Stat label={t("up.availability_range", { range })} value={pct(d.history.availability)} tone={pctTone(d.history.availability)} icon={<Target />} />
            <Stat label={t("up.col_response")} value={ms(d.last_ms)} hint={t("up.avg", { ms: ms(d.avg_ms_24h) })} icon={<Zap />} />
            <Stat label={t("up.cert")} value={d.cert_days == null ? "–" : d.cert_days < 0 ? t("up.cert_expired") : t("up.days", { count: d.cert_days })} hint={d.cert_expires_at ? formatDate(d.cert_expires_at, i18n.language) : t("up.cert_none")} icon={<Lock />} />
          </div>
          <Card>
            <CardHeader icon={<Clock />} title={t("up.response_title")} description={t("up.response_desc")}
              action={<Segmented value={range} onChange={setRange} options={[{ value: "24h", label: "24h" }, { value: "7d", label: "7d" }, { value: "30d", label: "30d" }]} />} />
            <CardBody><ResponseChart points={d.history.points} bucket={d.history.bucket_seconds} /></CardBody>
          </Card>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader icon={<XCircle />} title={t("up.incidents_title")} />
              {!d.history.incidents.length ? <EmptyState icon={<CheckCircle2 />} title={t("up.no_incidents")} /> : (
                <div className="max-h-80 divide-y divide-line overflow-y-auto">
                  {d.history.incidents.map((i) => (
                    <div key={i.id} className="px-5 py-3 text-sm">
                      <div className="flex flex-wrap items-center gap-2"><Badge tone={i.open ? "offline" : "neutral"}>{i.open ? t("up.open") : t("up.resolved")}</Badge><span className="font-semibold">{formatDate(i.started_at, i18n.language)}</span><span className="text-muted">{t("up.minutes", { count: Math.round(i.duration_minutes) })}</span></div>
                      {i.error && <div className="mt-1 text-xs text-muted">{i.error}</div>}
                    </div>
                  ))}
                </div>
              )}
            </Card>
            <Card>
              <CardHeader icon={<Activity />} title={t("up.recent_title")} />
              <div className="max-h-80 divide-y divide-line overflow-y-auto">
                {d.history.recent.map((r) => (
                  <div key={r.at} className="flex items-start gap-3 px-5 py-2 text-sm">
                    <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", r.ok === null ? "bg-muted" : r.ok ? "bg-online" : "bg-offline")} />
                    <div className="min-w-0 flex-1"><div className="text-xs text-muted">{formatDate(r.at, i18n.language)}</div>{r.error && <div className="break-words text-xs">{r.error}</div>}</div>
                    <div className="shrink-0 text-xs font-semibold">{r.code ? `${r.code} · ` : ""}{ms(r.ms)}</div>
                  </div>
                ))}
              </div>
            </Card>
          </div>
          <p className="text-xs text-muted">{t("up.settings_line", { interval: d.interval_seconds, timeout: d.timeout_seconds, fail: d.fail_after })}{d.slow_ms ? ` · ${t("up.slow_line", { ms: d.slow_ms })}` : ""}{d.node_name ? ` · ${t("up.runs_on", { machine: d.node_name })}` : ""}</p>
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ editor */
interface Form {
  customer_id: string; name: string; kind: Kind; target: string; port: string; config: CheckConfig; probe_node_id: string;
  node_id: string; enabled: boolean; interval_seconds: string; timeout_seconds: string; fail_after: string; slow_ms: string;
  cert_warn_days: string; severity: "critical" | "warning"; in_sla: boolean;
}

function toForm(c: Check | null, customerId: string): Form {
  return {
    customer_id: c?.customer_id ?? customerId, name: c?.name ?? "", kind: c?.kind ?? "http", target: c?.target ?? "",
    port: c?.port ? String(c.port) : "", config: c?.config ?? { method: "GET", expect_status: "200-399", verify_tls: true, follow_redirects: true, record_type: "A", mode: "ssl" },
    probe_node_id: c?.probe_node_id ?? "", node_id: c?.node_id ?? "", enabled: c?.enabled ?? true,
    interval_seconds: String(c?.interval_seconds ?? 60), timeout_seconds: String(c?.timeout_seconds ?? 10), fail_after: String(c?.fail_after ?? 2),
    slow_ms: c?.slow_ms ? String(c.slow_ms) : "", cert_warn_days: String(c?.cert_warn_days ?? 14), severity: c?.severity ?? "critical", in_sla: c?.in_sla ?? true,
  };
}

function bodyOf(f: Form) {
  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  return {
    customer_id: f.customer_id, name: f.name.trim(), kind: f.kind, target: f.target.trim(), port: num(f.port), config: f.config,
    probe_node_id: f.probe_node_id || null, node_id: f.node_id || null, enabled: f.enabled,
    interval_seconds: Number(f.interval_seconds), timeout_seconds: Number(f.timeout_seconds), fail_after: Number(f.fail_after),
    slow_ms: num(f.slow_ms), cert_warn_days: Number(f.cert_warn_days || 0), severity: f.severity, in_sla: f.in_sla,
  };
}

function CheckDialog({ check, onClose }: { check: Check | null; onClose: () => void }) {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const [f, setF] = useState<Form>(() => toForm(check, customerId || ""));
  const [test, setTest] = useState<TestResult | null>(null);
  useEffect(() => {
    if (!f.customer_id && customers.data?.length) setF((x) => ({ ...x, customer_id: customers.data![0].id }));
  }, [customers.data, f.customer_id]);
  const probes = useQuery({ queryKey: ["uptime-probes", f.customer_id], queryFn: () => get<Probe[]>(qs("api/uptime/probes", { customer_id: f.customer_id })), enabled: !!f.customer_id });
  const set = <K extends keyof Form>(k: K, v: Form[K]) => { setF((x) => ({ ...x, [k]: v })); setTest(null); };
  const cfg = (patch: Partial<CheckConfig>) => { setF((x) => ({ ...x, config: { ...x.config, ...patch } })); setTest(null); };
  const save = useMutation({
    mutationFn: () => (check ? put<Check>(`api/uptime/checks/${check.id}`, bodyOf(f)) : post<Check>("api/uptime/checks", bodyOf(f))),
    onSuccess: () => { invalidate(); toast.success(t("up.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const runTest = useMutation({
    mutationFn: () => { const b = bodyOf(f); return post<TestResult>("api/uptime/test", { customer_id: b.customer_id, kind: b.kind, target: b.target, port: b.port, config: b.config, probe_node_id: b.probe_node_id, timeout_seconds: b.timeout_seconds }); },
    onSuccess: setTest,
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const placeholder = { http: "https://www.example.com/", tcp: "mail.example.com", ping: "10.40.5.3", dns: "example.com", vpn: f.config.mode === "tunnel" ? "10.20.0.1" : "vpn.example.com" }[f.kind];
  const needsPort = f.kind === "tcp" || (f.kind === "vpn" && f.config.mode !== "tunnel");
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={check ? t("up.edit_title") : t("up.add_title")} description={t("up.edit_desc")}
      footer={<>
        <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
        <Button loading={runTest.isPending} disabled={!f.target.trim() || !f.customer_id} onClick={() => runTest.mutate()}>{!runTest.isPending && <Play />} {t("up.test")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!f.name.trim() || !f.target.trim() || !f.customer_id} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("up.f_customer")}>
            <Select value={f.customer_id} disabled={!!check} onChange={(e) => setF((x) => ({ ...x, customer_id: e.target.value, probe_node_id: "", node_id: "" }))}>
              {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
          <Field label={t("up.f_name")}><Input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder={t("up.f_name_ph")} maxLength={120} /></Field>
        </div>
        <Field label={t("up.f_kind")}>
          <div className="overflow-x-auto"><Segmented value={f.kind} onChange={(v) => set("kind", v)} options={KINDS.map((k) => ({ value: k, label: t(`up.kind.${k}`) }))} /></div>
        </Field>
        {f.kind === "vpn" && (
          <Field label={t("up.f_vpn_mode")} hint={t(`up.vpn_hint_${f.config.mode ?? "ssl"}`)}>
            <Segmented value={f.config.mode ?? "ssl"} onChange={(v) => cfg({ mode: v })} options={[{ value: "ssl", label: t("up.vpn_ssl") }, { value: "tunnel", label: t("up.vpn_tunnel") }]} />
          </Field>
        )}
        <div className="grid gap-4 sm:grid-cols-[1fr_140px]">
          <Field label={t(`up.f_target_${f.kind}`)}><Input value={f.target} onChange={(e) => set("target", e.target.value)} placeholder={placeholder} className="mono" /></Field>
          {needsPort && <Field label={t("up.f_port")}><Input type="number" min={1} max={65535} value={f.port} onChange={(e) => set("port", e.target.value)} placeholder={f.kind === "vpn" ? "443" : "587"} /></Field>}
        </div>
        {f.kind === "http" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("up.f_expect_status")} hint={t("up.f_expect_status_hint")}><Input value={f.config.expect_status ?? "200-399"} onChange={(e) => cfg({ expect_status: e.target.value })} /></Field>
            <Field label={t("up.f_method")}><Select value={f.config.method ?? "GET"} onChange={(e) => cfg({ method: e.target.value as "GET" | "HEAD" })}><option value="GET">GET</option><option value="HEAD">HEAD</option></Select></Field>
            <Field label={t("up.f_keyword")} hint={t("up.f_keyword_hint")}><Input value={f.config.keyword ?? ""} onChange={(e) => cfg({ keyword: e.target.value })} disabled={f.config.method === "HEAD"} maxLength={200} /></Field>
            <div className="space-y-3 pt-6">
              <Switch checked={!!f.config.keyword_absent} onCheckedChange={(v) => cfg({ keyword_absent: v })} label={t("up.f_keyword_absent")} disabled={!f.config.keyword} />
              <Switch checked={f.config.verify_tls !== false} onCheckedChange={(v) => cfg({ verify_tls: v })} label={t("up.f_verify_tls")} />
              <Switch checked={f.config.follow_redirects !== false} onCheckedChange={(v) => cfg({ follow_redirects: v })} label={t("up.f_follow")} />
            </div>
          </div>
        )}
        {f.kind === "dns" && (
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label={t("up.f_record_type")}><Select value={f.config.record_type ?? "A"} onChange={(e) => cfg({ record_type: e.target.value })}>{RECORD_TYPES.map((r) => <option key={r}>{r}</option>)}</Select></Field>
            <Field label={t("up.f_expect")} hint={t("up.f_expect_hint")}><Input value={f.config.expect ?? ""} onChange={(e) => cfg({ expect: e.target.value })} className="mono" /></Field>
            <Field label={t("up.f_server")} hint={t("up.f_server_hint")}><Input value={f.config.server ?? ""} onChange={(e) => cfg({ server: e.target.value })} placeholder="1.1.1.1" className="mono" /></Field>
          </div>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("up.f_probe")} hint={t("up.f_probe_hint")}>
            <Select value={f.probe_node_id} onChange={(e) => set("probe_node_id", e.target.value)}>
              <option value="">{t("up.probe_app")}</option>
              {(probes.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}{p.online ? "" : ` (${t("up.offline")})`}</option>)}
            </Select>
          </Field>
          <Field label={t("up.f_machine")} hint={t("up.f_machine_hint")}>
            <Select value={f.node_id} onChange={(e) => set("node_id", e.target.value)}>
              <option value="">{t("up.machine_none")}</option>
              {(probes.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          </Field>
        </div>
        <details className="rounded-md border border-line px-4 py-3">
          <summary className="cursor-pointer text-sm font-semibold">{t("up.advanced")}</summary>
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <Field label={t("up.f_interval")}><Select value={f.interval_seconds} onChange={(e) => set("interval_seconds", e.target.value)}>{[30, 60, 120, 300, 600, 900, 1800, 3600].map((s) => <option key={s} value={s}>{s < 60 ? `${s} s` : `${s / 60} min`}</option>)}</Select></Field>
            <Field label={t("up.f_timeout")}><Input type="number" min={1} max={60} value={f.timeout_seconds} onChange={(e) => set("timeout_seconds", e.target.value)} /></Field>
            <Field label={t("up.f_fail_after")} hint={t("up.f_fail_after_hint")}><Input type="number" min={1} max={10} value={f.fail_after} onChange={(e) => set("fail_after", e.target.value)} /></Field>
            <Field label={t("up.f_slow")} hint={t("up.f_slow_hint")}><Input type="number" min={1} value={f.slow_ms} onChange={(e) => set("slow_ms", e.target.value)} placeholder="2000" /></Field>
            {(f.kind === "http" || f.kind === "vpn") && <Field label={t("up.f_cert_days")} hint={t("up.f_cert_days_hint")}><Input type="number" min={0} max={120} value={f.cert_warn_days} onChange={(e) => set("cert_warn_days", e.target.value)} /></Field>}
            <Field label={t("up.f_severity")}><Select value={f.severity} onChange={(e) => set("severity", e.target.value as Form["severity"])}><option value="critical">{t("up.sev_critical")}</option><option value="warning">{t("up.sev_warning")}</option></Select></Field>
          </div>
          <div className="mt-4 space-y-3">
            <Switch checked={f.in_sla} onCheckedChange={(v) => set("in_sla", v)} label={t("up.f_in_sla")} description={t("up.f_in_sla_hint")} />
            <Switch checked={f.enabled} onCheckedChange={(v) => set("enabled", v)} label={t("up.f_enabled")} />
          </div>
        </details>
        {test && (
          <Alert tone={test.ok ? "online" : test.ok === null ? "warning" : "offline"} title={test.ok ? t("up.test_ok", { ms: ms(test.ms) }) : t("up.test_failed")}>
            {[test.code ? `HTTP ${test.code}` : null, test.error, test.values?.length ? test.values.join(", ") : null,
              test.cert_expires_at ? t("up.test_cert", { date: formatDate(test.cert_expires_at) }) : null, test.probe ? t("up.via", { probe: test.probe }) : null].filter(Boolean).join(" · ") || null}
          </Alert>
        )}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ SLA */
function currentMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function SlaTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const [period, setPeriod] = useState(currentMonth());
  const [open, setOpen] = useState<string | null>(null);
  const [settings, setSettings] = useState<SlaCustomer | null>(null);
  const q = useQuery({ queryKey: ["uptime-sla", period], queryFn: () => get<{ period: string; customers: SlaCustomer[] }>(qs("api/uptime/sla", { period })) });
  const detail = useQuery({ queryKey: ["uptime-sla", period, open], queryFn: () => get<{ customers: SlaCustomer[] }>(qs("api/uptime/sla", { period, customer_id: open })), enabled: !!open });
  const rows = useMemo(() => (q.data?.customers ?? []).slice().sort((a, b) => Number(!a.checks.length) - Number(!b.checks.length) || a.customer.localeCompare(b.customer)), [q.data]);
  const d = detail.data?.customers[0];
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Target />} title={t("up.sla_title")} description={t("up.sla_desc")}
          action={<Input type="month" value={period} max={currentMonth()} onChange={(e) => e.target.value && setPeriod(e.target.value)} className="w-40" />} />
        {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !rows.length ? <EmptyState icon={<Globe />} title={t("up.sla_empty")} /> : (
          <div className="overflow-x-auto">
            <Table>
              <thead><Tr><Th>{t("up.col_customer")}</Th><Th>{t("up.col_measured")}</Th><Th>{t("up.col_target")}</Th><Th>{t("up.col_downtime")}</Th><Th>{t("up.col_checks")}</Th>{admin && <Th />}</Tr></thead>
              <tbody>
                {rows.map((c) => (
                  <Tr key={c.customer_id} className={cn(c.checks.length && "cursor-pointer", open === c.customer_id && "bg-sunken")} onClick={() => c.checks.length && setOpen(open === c.customer_id ? null : c.customer_id)}>
                    <Td><div className="font-semibold">{c.customer}</div>{c.maintenance_window && <div className="text-xs text-muted">{c.exclude_maintenance ? t("up.maint_excluded", { window: c.maintenance_window }) : t("up.maint_counted", { window: c.maintenance_window })}{c.maintenance_understood === false && ` · ${t("up.maint_unreadable")}`}</div>}</Td>
                    <Td>{c.checks.length ? <Badge tone={pctTone(c.availability, c.target_pct)}>{pct(c.availability, 3)}</Badge> : <span className="text-xs text-muted">{t("up.no_checks")}</span>}</Td>
                    <Td>{c.target_pct != null ? <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{c.target_pct}%</span>{c.met === true && <Badge tone="online">{t("up.met")}</Badge>}{c.met === false && <Badge tone="offline">{t("up.missed")}</Badge>}</div> : <span className="text-xs text-muted">{t("up.no_target")}</span>}{c.target_source === "sla_text" && <div className="text-xs text-muted">{t("up.from_sla_text")}</div>}</Td>
                    <Td className="whitespace-nowrap text-sm">{t("up.minutes", { count: Math.round(c.downtime_minutes) })}{c.allowed_downtime_minutes != null && <div className="text-xs text-muted">{t("up.allowed", { count: Math.round(c.allowed_downtime_minutes) })}</div>}</Td>
                    <Td>{c.checks.length}</Td>
                    {admin && <Td onClick={(e) => e.stopPropagation()}><Button size="icon" variant="ghost" title={t("up.sla_settings")} onClick={() => setSettings(c)}><Settings2 /></Button></Td>}
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      {open && (
        <Card>
          <CardHeader icon={<Activity />} title={d ? t("up.sla_detail", { customer: d.customer }) : t("up.loading")} />
          {detail.isLoading || !d ? <CardBody><Spinner /></CardBody> : (
            <div className="grid gap-0 lg:grid-cols-2 lg:divide-x lg:divide-line">
              <div className="divide-y divide-line">
                {d.checks.map((c) => (
                  <div key={c.id} className="flex items-center gap-3 px-5 py-3 text-sm">
                    <div className="min-w-0 flex-1"><div className="font-semibold">{c.name}{!c.in_sla && <span className="ml-2 text-xs font-normal text-muted">({t("up.not_in_sla")})</span>}</div><div className="mono truncate text-xs text-muted">{c.target}</div></div>
                    <div className="text-right"><Badge tone={pctTone(c.availability, d.target_pct)}>{pct(c.availability, 3)}</Badge><div className="mt-1 text-xs text-muted">{t("up.incidents_count", { count: c.incidents })} · {t("up.minutes", { count: Math.round(c.downtime_minutes) })}</div></div>
                  </div>
                ))}
              </div>
              <div className="max-h-96 divide-y divide-line overflow-y-auto border-t border-line lg:border-t-0">
                {!(d.incidents ?? []).length ? <EmptyState icon={<CheckCircle2 />} title={t("up.no_incidents")} /> : (d.incidents ?? []).map((i) => (
                  <div key={i.id} className="px-5 py-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{i.check}</span><span className="text-muted">{formatDate(i.started_at, i18n.language)}</span><Badge tone={i.open ? "offline" : "neutral"}>{t("up.minutes", { count: Math.round(i.duration_minutes) })}</Badge></div>
                    {i.error && <div className="mt-1 text-xs text-muted">{i.error}</div>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}
      <p className="text-xs text-muted"><ShieldCheck className="mr-1 inline size-3.5" />{t("up.sla_note")}</p>
      {admin && settings && <SlaDialog c={settings} onClose={() => setSettings(null)} />}
    </div>
  );
}

function SlaDialog({ c, onClose }: { c: SlaCustomer; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const s = c.settings;
  const [target, setTarget] = useState(s.target_source === "setting" && s.target_pct != null ? String(s.target_pct) : "");
  const [exclude, setExclude] = useState(s.exclude_maintenance);
  const [alertBreach, setAlertBreach] = useState(s.alert_breach);
  const [recipients, setRecipients] = useState(s.recipients.join(", "));
  const save = useMutation({
    mutationFn: () => put<SlaSettings>(`api/uptime/customers/${c.customer_id}/sla`, {
      target_pct: target.trim() ? Number(target.replace(",", ".")) : null, exclude_maintenance: exclude, alert_breach: alertBreach,
      recipients: recipients.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
    }),
    onSuccess: () => { invalidate(); toast.success(t("up.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("up.sla_settings_title", { customer: c.customer })} description={t("up.sla_settings_desc")}
      footer={<><Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("up.f_target_pct")} hint={s.target_source === "sla_text" && !target ? t("up.f_target_from_text", { pct: s.target_pct }) : t("up.f_target_pct_hint")}>
          <Input inputMode="decimal" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="99.9" />
        </Field>
        <Switch checked={exclude} onCheckedChange={setExclude} label={t("up.f_exclude")} description={c.maintenance_window ? t("up.f_exclude_hint", { window: c.maintenance_window }) : t("up.f_exclude_none")} />
        <Switch checked={alertBreach} onCheckedChange={setAlertBreach} label={t("up.f_alert_breach")} description={t("up.f_alert_breach_hint")} />
        <Field label={t("up.f_recipients")} hint={t("up.f_recipients_hint")}><Input value={recipients} onChange={(e) => setRecipients(e.target.value)} placeholder="ops@example.com" /></Field>
      </div>
    </Dialog>
  );
}
