import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive, ArrowUpCircle, Award, CalendarClock, CheckCheck, CheckCircle2, ChevronDown, Clock, HardDrive, Hand, HeartPulse,
  Info as InfoIcon, Mail, Play, Save, Settings2, ShieldAlert, Sparkles, Sunrise, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";
import { ApprovalDialog } from "@/pages/Gateway";

/* ------------------------------------------------------------------ types */
type Severity = "high" | "medium" | "low" | "info";
type Category = "updates" | "cves" | "disks" | "certificates" | "backups" | "other";
const CATEGORIES: Category[] = ["updates", "cves", "disks", "certificates", "backups", "other"];
const SEVERITIES: Severity[] = ["high", "medium", "low", "info"];
type PropStatus = "pending" | "scheduled" | "approved" | "running" | "executed" | "failed" | "rejected" | "expired" | "missing";

interface Proposal {
  tool: string;
  label: string;
  args: Record<string, unknown>;
  risk: string;
  approval_id: string;
  status: PropStatus;
  run_after: string | null;
  expires_at: string | null;
  error: string | null;
  decided_by: string | null;
}

interface Finding {
  id: string;
  customer_id: string;
  category: Category;
  severity: Severity;
  area: string;
  target: string | null;
  code: string | null;
  params: Record<string, string | number>;
  finding: string;
  suggestion: string;
  is_new: boolean;
  first_seen_at: string;
  proposal: Proposal | null;
}

interface Resolved { key: string; category: Category; severity: Severity; target: string | null; code: string | null; params: Record<string, string | number>; finding: string }

interface CustomerSummary {
  id: string;
  name: string;
  kind: string;
  counts: Record<Severity, number>;
  by_category: Record<Category, number>;
  total: number;
  new: number;
  resolved: Resolved[];
  proposals: number;
  errors: string[];
  maintenance_window: string | null;
  first_run?: boolean;
}

interface Totals { customers: number; findings: number; new: number; resolved: number; proposals: number; counts: Record<Severity, number>; by_category: Record<Category, number> }

interface Run {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: "running" | "done" | "failed";
  trigger: "schedule" | "manual" | "mcp";
  requested_by: string | null;
  summary_sent_at: string | null;
  error: string | null;
  customers: CustomerSummary[];
  totals: Totals;
  findings?: Finding[];
  pending_proposals?: number;
  remediation?: { total: number; counts: Record<string, number>; items: { id: string; target: string | null; customer_name: string | null; problem: string; rule_name: string; status: string; run_id: string | null; created_at: string }[] };
}

interface Latest { run: Run | null; running: boolean; running_id: string | null; next: { run: string | null; summary: string | null } }

interface NightlySettings {
  enabled: boolean;
  run_at: string;
  summary_at: string;
  notify_app: boolean;
  notify_mail: boolean;
  mail_to: string;
  checks: Record<Category, boolean>;
  cert_warn_days: number;
  cve_lookup: boolean;
  proposals: boolean;
  use_window: boolean;
  mail_when_clean: boolean;
}
interface CustomerCfg { id: string; name: string; kind: string; enabled: boolean; cert_endpoints: string[]; maintenance_window: string | null }

const SEV_TONE = { high: "offline", medium: "warning", low: "info", info: "neutral" } as const;
const CAT_ICON: Record<Category, ReactNode> = {
  updates: <ArrowUpCircle />, cves: <ShieldAlert />, disks: <HardDrive />, certificates: <Award />, backups: <Archive />, other: <Sparkles />,
};
const PROP_TONE: Record<PropStatus, "online" | "warning" | "offline" | "info" | "neutral"> = {
  pending: "warning", scheduled: "info", approved: "info", running: "info", executed: "online", failed: "offline",
  rejected: "neutral", expired: "neutral", missing: "neutral",
};

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    for (const k of ["nightly", "nightly-runs", "nightly-run", "approvals", "approvals-count", "approval"]) qc.invalidateQueries({ queryKey: [k] });
  };
}

/** The finding text in the interface language (the opt.* keys of the optimisation scan). */
function useFindingText() {
  const { t } = useTranslation();
  return (f: { code: string | null; params: Record<string, string | number>; target: string | null; finding: string; suggestion?: string }) => ({
    target: f.code ? t(`opt.${f.code}.t`, { ...f.params, defaultValue: f.target ?? "" }) : f.target ?? "",
    finding: f.code ? t(`opt.${f.code}.f`, { ...f.params, defaultValue: f.finding }) : f.finding,
    suggestion: f.code ? t(`opt.${f.code}.s`, { ...f.params, defaultValue: f.suggestion ?? "" }) : f.suggestion ?? "",
  });
}

export function SeverityBadge({ s }: { s: Severity }) {
  const { t } = useTranslation();
  return <Badge tone={SEV_TONE[s]} dot>{t(`nightly.sev.${s}`)}</Badge>;
}

/* ------------------------------------------------------------------ page */
export function NightlyPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "summary";
  const runId = params.get("run");
  const setTab = (v: string) => { const p = new URLSearchParams(params); p.set("tab", v); if (v === "summary") p.delete("run"); setParams(p, { replace: true }); };
  const [settingsOpen, setSettingsOpen] = useState(false);
  const invalidate = useInvalidate();

  const latest = useQuery({
    queryKey: ["nightly"],
    queryFn: () => get<Latest>("api/nightly"),
    refetchInterval: (q) => (q.state.data?.running ? 4000 : 60_000),
  });
  const other = useQuery({ queryKey: ["nightly-run", runId], queryFn: () => get<Run>(`api/nightly/runs/${runId}`), enabled: !!runId });
  const runNow = useMutation({
    mutationFn: () => post<{ id: string }>("api/nightly/run", {}),
    onSuccess: () => { invalidate(); toast.success(t("nightly.started")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const run = runId ? other.data : latest.data?.run;
  const send = useMutation({
    mutationFn: () => post<{ sent: Record<string, boolean> }>(`api/nightly/runs/${run!.id}/send`),
    onSuccess: (d) => { invalidate(); d.sent.mail === false ? toast.error(t("nightly.send_failed")) : toast.success(t("nightly.sent")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const running = !!latest.data?.running;
  const next = latest.data?.next;

  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><Sunrise className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("nightly.title")}</span></span>}
        description={run ? t("nightly.desc_run", { when: formatDate(run.started_at, i18n.language), ago: timeAgo(run.started_at, i18n.language) }) : t("nightly.desc")}
        actions={admin && (
          <>
            <Button onClick={() => setSettingsOpen(true)}><Settings2 /> {t("nightly.settings")}</Button>
            {run && run.status !== "running" && <Button loading={send.isPending} onClick={() => send.mutate()}>{!send.isPending && <Mail />} {t("nightly.send")}</Button>}
            <Button variant={run?.pending_proposals ? "secondary" : "primary"} loading={runNow.isPending} disabled={running} onClick={() => runNow.mutate()}>{!runNow.isPending && <Play />} {t("nightly.run_now")}</Button>
          </>
        )} />
      {running && <Alert tone="info" className="mb-5" title={<span className="flex items-center gap-2"><Spinner className="size-4" />{t("nightly.running")}</span>}>{t("nightly.running_hint")}</Alert>}
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="summary" className={tabCls}>{t("nightly.tab_summary")}</Tabs.Trigger>
          <Tabs.Trigger value="history" className={tabCls}>{t("nightly.tab_history")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="summary">
          {latest.isLoading || (runId && other.isLoading) ? <Spinner /> : latest.isError ? <Alert tone="offline">{errorMessage(latest.error, t)}</Alert> : !run ? (
            <Card>
              <EmptyState icon={<Sunrise />} title={t("nightly.empty")}
                action={admin && !running ? <Button variant="primary" onClick={() => runNow.mutate()}><Play /> {t("nightly.run_now")}</Button> : undefined}>
                {t("nightly.empty_hint", { run: next?.run ? formatDate(next.run, i18n.language) : "–" })}
              </EmptyState>
            </Card>
          ) : <Summary run={run} admin={admin} next={next} isOld={!!runId} onLatest={() => setTab("summary")} />}
        </Tabs.Content>
        <Tabs.Content value="history"><History onPick={(id) => { const p = new URLSearchParams(params); p.set("tab", "summary"); p.set("run", id); setParams(p); }} /></Tabs.Content>
      </Tabs.Root>
      {admin && <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ summary */
function Summary({ run, admin, next, isOld, onLatest }: { run: Run; admin: boolean; next?: Latest["next"]; isOld: boolean; onLatest: () => void }) {
  const { t, i18n } = useTranslation();
  const [cat, setCat] = useState<Category | "all">("all");
  const [view, setView] = useState<"all" | "new" | "proposals">("all");
  const [detail, setDetail] = useState<string | null>(null);
  const invalidate = useInvalidate();
  const findings = run.findings ?? [];
  const pending = findings.filter((f) => f.proposal?.status === "pending");
  const pendingIds = new Set(pending.map((f) => f.proposal!.approval_id));
  const approveAll = useMutation({
    mutationFn: (customer_id?: string) => post<{ approved: number; started: number; scheduled: number }>(`api/nightly/runs/${run.id}/approve`, { customer_id: customer_id ?? null }),
    onSuccess: (d) => { invalidate(); toast.success(t("nightly.approved_all", { count: d.approved, scheduled: d.scheduled })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const shown = findings.filter((f) => (cat === "all" || f.category === cat) && (view === "all" || (view === "new" ? f.is_new : !!f.proposal)));
  const byCustomer = useMemo(() => {
    const m = new Map<string, Finding[]>();
    for (const f of shown) m.set(f.customer_id, [...(m.get(f.customer_id) ?? []), f]);
    return m;
  }, [shown]);
  const customers = [...run.customers].sort((a, b) => (b.counts?.high ?? 0) - (a.counts?.high ?? 0) || (b.total ?? 0) - (a.total ?? 0) || a.name.localeCompare(b.name));
  const clean = customers.filter((c) => !c.total);
  const tot = run.totals;

  return (
    <div className="space-y-5">
      {isOld && <Alert tone="info">{t("nightly.old_run", { when: formatDate(run.started_at, i18n.language) })} <Button variant="link" onClick={onLatest}>{t("nightly.to_latest")}</Button></Alert>}
      {run.status === "failed" && <Alert tone="offline" title={t("nightly.failed")}>{run.error}</Alert>}
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("nightly.k_high")} value={tot.counts.high ?? 0} hint={t("nightly.k_high_hint", { medium: tot.counts.medium ?? 0, low: tot.counts.low ?? 0 })} tone={tot.counts.high ? "offline" : "online"} icon={<ShieldAlert />} />
        <Stat label={t("nightly.k_new")} value={tot.new} hint={t("nightly.k_new_hint")} tone={tot.new ? "warning" : "neutral"} icon={<Sparkles />} />
        <Stat label={t("nightly.k_resolved")} value={tot.resolved} hint={t("nightly.k_resolved_hint")} tone="online" icon={<CheckCircle2 />} />
        <Stat label={t("nightly.k_pending")} value={pending.length ? pendingIds.size : 0} hint={t("nightly.k_pending_hint")} tone={pending.length ? "accent" : "neutral"} icon={<Hand />} />
      </div>

      {admin && pendingIds.size > 0 && !isOld && (
        <div className="flex flex-col gap-3 rounded-lg border border-koraal/40 bg-koraal/10 px-5 py-4 sm:flex-row sm:items-center">
          <div className="flex-1">
            <div className="font-bold">{t("nightly.ready_title", { count: pendingIds.size })}</div>
            <div className="text-sm text-muted">{t("nightly.ready_hint")}</div>
          </div>
          <Button variant="primary" loading={approveAll.isPending && approveAll.variables === undefined} onClick={() => approveAll.mutate(undefined)}><CheckCheck /> {t("nightly.approve_all", { count: pendingIds.size })}</Button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setCat("all")} className={chip(cat === "all")}>{t("nightly.cat.all")} <span className="text-muted">{tot.findings}</span></button>
        {CATEGORIES.map((c) => (
          <button key={c} type="button" onClick={() => setCat(c)} className={chip(cat === c)}>
            {CAT_ICON[c]}{t(`nightly.cat.${c}`)} <span className="text-muted">{tot.by_category?.[c] ?? 0}</span>
          </button>
        ))}
        <div className="ml-auto">
          <Segmented value={view} onChange={setView} options={[
            { value: "all", label: t("nightly.v_all") }, { value: "new", label: t("nightly.v_new") }, { value: "proposals", label: t("nightly.v_proposals") },
          ]} />
        </div>
      </div>

      {customers.filter((c) => c.total || c.errors?.length).map((c) => (
        <CustomerCard key={c.id} c={c} rows={byCustomer.get(c.id) ?? []} admin={admin && !isOld} onDetail={setDetail}
          onApproveAll={() => approveAll.mutate(c.id)} approving={approveAll.isPending && approveAll.variables === c.id}
          pending={new Set(pending.filter((f) => f.customer_id === c.id).map((f) => f.proposal!.approval_id)).size} />
      ))}

      {!!run.remediation?.total && (
        <Card>
          <CardHeader icon={<HeartPulse />} title={t("nightly.recovery_title")}
            description={t("rem.stats_line", { attempts: run.remediation.total, ok: run.remediation.counts.succeeded ?? 0, failed: run.remediation.counts.failed ?? 0 })}
            action={<Link to="/remediation" className="text-sm font-semibold text-accent-text hover:underline">{t("rem.toast.open")}</Link>} />
          <CardBody className="space-y-2">
            {run.remediation.items.slice(0, 8).map((a) => (
              <div key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
                <Badge tone={a.status === "succeeded" ? "online" : a.status === "failed" ? "offline" : a.status === "skipped" ? "warning" : "info"}>{t(`rem.status.${a.status}`, { defaultValue: a.status })}</Badge>
                <span className="font-semibold">{a.target || "–"}</span>
                <span className="text-muted">{a.customer_name || ""}</span>
                <span className="min-w-0 flex-1">{a.problem}</span>
                <span className="text-xs text-muted">{a.rule_name}</span>
              </div>
            ))}
          </CardBody>
        </Card>
      )}

      {clean.length > 0 && (
        <Card>
          <CardHeader icon={<CheckCircle2 />} title={t("nightly.clean_title")} description={t("nightly.clean_hint")} />
          <CardBody className="flex flex-wrap gap-2">
            {clean.map((c) => <Badge key={c.id} tone="online"><CheckCircle2 className="size-3" />{c.name}</Badge>)}
          </CardBody>
        </Card>
      )}

      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
        <span><Clock className="mr-1 inline size-3.5" />{t("nightly.ran", { start: formatDate(run.started_at, i18n.language), end: run.finished_at ? formatDate(run.finished_at, i18n.language) : "…" })}</span>
        <span><Mail className="mr-1 inline size-3.5" />{run.summary_sent_at ? t("nightly.sent_at", { when: formatDate(run.summary_sent_at, i18n.language) }) : t("nightly.not_sent")}</span>
        {next?.run && <span><CalendarClock className="mr-1 inline size-3.5" />{t("nightly.next", { run: formatDate(next.run, i18n.language), summary: next.summary ? formatDate(next.summary, i18n.language) : "–" })}</span>}
      </div>
      <ApprovalDialog id={detail} onClose={() => { setDetail(null); invalidate(); }} />
    </div>
  );
}

const chip = (active: boolean) => cn(
  "inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1 text-[13px] font-semibold transition-colors [&_svg]:size-3.5",
  active ? "border-nacht bg-nacht text-white dark:border-lucht dark:bg-lucht/15 dark:text-ink [&_span]:text-white/70" : "border-line bg-raised text-ink hover:bg-sunken",
);

function CustomerCard({ c, rows, admin, pending, onDetail, onApproveAll, approving }: {
  c: CustomerSummary; rows: Finding[]; admin: boolean; pending: number; onDetail: (id: string) => void; onApproveAll: () => void; approving: boolean;
}) {
  const { t } = useTranslation();
  const text = useFindingText();
  const [showResolved, setShowResolved] = useState(false);
  const sev = SEVERITIES.filter((s) => c.counts?.[s]).map((s) => t(`nightly.sev_n.${s}`, { count: c.counts[s] })).join(" · ");
  return (
    <Card>
      <CardHeader
        title={<Link to={`/customers/${c.id}`} className="hover:underline">{c.name}</Link>}
        description={<>
          {sev || t("nightly.all_clear")}
          {c.new ? <> · <span className="font-semibold text-warning">{t("nightly.n_new", { count: c.new })}</span></> : null}
          {c.resolved?.length ? <> · <span className="text-online">{t("nightly.n_resolved", { count: c.resolved.length })}</span></> : null}
          <span className="block text-xs">{c.maintenance_window ? t("nightly.window", { window: c.maintenance_window }) : t("nightly.no_window")}</span>
        </>}
        action={admin && pending > 0 && <Button size="sm" loading={approving} onClick={onApproveAll}>{!approving && <CheckCheck />} {t("nightly.approve_customer", { count: pending })}</Button>} />
      {c.errors?.length > 0 && <div className="px-5 pt-4"><Alert tone="warning" title={t("nightly.errors")}>{c.errors.map((e, i) => <div key={i}>{e}</div>)}</Alert></div>}
      {!rows.length ? <div className="px-5 py-4 text-sm text-muted">{t("nightly.none_in_filter")}</div> : (
        <ul className="divide-y divide-line">
          {rows.map((f) => {
            const x = text(f);
            return (
              <li key={f.id} className="flex flex-col gap-3 px-5 py-3.5 lg:flex-row lg:items-start">
                <div className="flex shrink-0 items-center gap-2 lg:w-44 lg:flex-col lg:items-start">
                  <SeverityBadge s={f.severity} />
                  <span className="inline-flex items-center gap-1 text-xs text-muted [&_svg]:size-3.5">{CAT_ICON[f.category]}{t(`nightly.cat.${f.category}`)}</span>
                </div>
                <div className="min-w-0 flex-1 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{x.target}</span>
                    {f.is_new && <Badge tone="accent">{t("nightly.new")}</Badge>}
                  </div>
                  <div className="mt-0.5">{x.finding}</div>
                  <div className="mt-0.5 text-muted">{x.suggestion}</div>
                </div>
                <div className="shrink-0 lg:w-72">{f.proposal ? <ProposalCell p={f.proposal} admin={admin} onDetail={onDetail} /> : <span className="text-xs text-muted">{t("nightly.no_proposal")}</span>}</div>
              </li>
            );
          })}
        </ul>
      )}
      {c.resolved?.length > 0 && (
        <div className="border-t border-line px-5 py-3">
          <button type="button" className="inline-flex cursor-pointer items-center gap-1 text-sm font-semibold text-online" onClick={() => setShowResolved(!showResolved)}>
            <ChevronDown className={cn("size-4 transition-transform", !showResolved && "-rotate-90")} />{t("nightly.resolved_title", { count: c.resolved.length })}
          </button>
          {showResolved && (
            <ul className="mt-2 space-y-1 text-sm">
              {c.resolved.map((r) => { const x = text(r); return <li key={r.key} className="flex gap-2"><CheckCircle2 className="mt-0.5 size-4 shrink-0 text-online" /><span><span className="font-semibold">{x.target}</span> · <span className="text-muted line-through decoration-muted/50">{x.finding}</span></span></li>; })}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

function ProposalCell({ p, admin, onDetail }: { p: Proposal; admin: boolean; onDetail: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const decide = useMutation({
    mutationFn: (approve: boolean) => post<{ run_after?: string | null }>(`api/gateway/approvals/${p.approval_id}/${approve ? "approve" : "reject"}`, { note: null }),
    onSuccess: (res, approve) => {
      invalidate();
      const later = res.run_after && new Date(res.run_after).getTime() > Date.now();
      toast.success(!approve ? t("gw.appr.rejected_toast") : later ? t("gw.appr.scheduled_toast", { when: formatDate(res.run_after!, i18n.language) }) : t("gw.appr.approved_toast"));
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div className="space-y-1.5 rounded-md border border-line bg-sunken/50 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-semibold">{t(`nightly.prop.${p.label}`, { defaultValue: p.tool })}</span>
        <Badge tone={PROP_TONE[p.status]}>{t(`nightly.pst.${p.status}`)}</Badge>
      </div>
      {p.status === "pending" && (
        <div className="text-xs text-muted">{p.run_after ? t("nightly.runs_in_window", { when: formatDate(p.run_after, i18n.language) }) : t("nightly.runs_now")}</div>
      )}
      {p.status === "scheduled" && p.run_after && <div className="text-xs text-muted">{t("nightly.scheduled_for", { when: formatDate(p.run_after, i18n.language) })}</div>}
      {p.status === "failed" && p.error && <div className="text-xs text-offline">{p.error}</div>}
      <div className="flex items-center gap-1.5">
        {admin && p.status === "pending" && (
          <>
            <Button size="sm" variant="dark" loading={decide.isPending && decide.variables === true} onClick={() => decide.mutate(true)}>{!(decide.isPending && decide.variables) && <CheckCircle2 />} {t("nightly.approve")}</Button>
            <Button size="sm" variant="ghost" aria-label={t("gw.appr.reject")} title={t("gw.appr.reject")} loading={decide.isPending && decide.variables === false} onClick={() => decide.mutate(false)}><XCircle /></Button>
          </>
        )}
        {admin && <Button size="sm" variant="link" className="ml-auto" onClick={() => onDetail(p.approval_id)}><InfoIcon className="!size-3.5" /> {t("nightly.details")}</Button>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ history */
function History({ onPick }: { onPick: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["nightly-runs"], queryFn: () => get<Run[]>("api/nightly/runs?limit=60") });
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const rows = q.data ?? [];
  return (
    <Card>
      <CardHeader icon={<CalendarClock />} title={t("nightly.history")} description={t("nightly.history_hint")} />
      {!rows.length ? <EmptyState icon={<Sunrise />} title={t("nightly.empty")} /> : (
        <div className="overflow-x-auto">
          <Table>
            <thead><tr><Th>{t("nightly.h_started")}</Th><Th>{t("nightly.h_trigger")}</Th><Th>{t("nightly.h_status")}</Th><Th>{t("nightly.h_customers")}</Th><Th>{t("nightly.k_high")}</Th><Th>{t("nightly.h_findings")}</Th><Th>{t("nightly.k_new")}</Th><Th>{t("nightly.k_resolved")}</Th><Th>{t("nightly.h_proposals")}</Th><Th>{t("nightly.h_sent")}</Th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <Tr key={r.id} className={cn(r.status !== "running" && "cursor-pointer")} onClick={() => r.status !== "running" && onPick(r.id)}>
                  <Td className="whitespace-nowrap font-semibold">{formatDate(r.started_at, i18n.language)}</Td>
                  <Td className="text-sm">{t(`nightly.trigger.${r.trigger}`)}{r.requested_by ? <span className="text-muted"> · {r.requested_by}</span> : null}</Td>
                  <Td><Badge tone={r.status === "done" ? "online" : r.status === "failed" ? "offline" : "info"} dot>{t(`nightly.status.${r.status}`)}</Badge></Td>
                  <Td>{r.totals.customers}</Td>
                  <Td>{r.totals.counts.high ? <Badge tone="offline">{r.totals.counts.high}</Badge> : <span className="text-muted">0</span>}</Td>
                  <Td>{r.totals.findings}</Td>
                  <Td>{r.totals.new || <span className="text-muted">0</span>}</Td>
                  <Td>{r.totals.resolved || <span className="text-muted">0</span>}</Td>
                  <Td>{r.totals.proposals || <span className="text-muted">0</span>}</Td>
                  <Td className="text-sm text-muted">{r.summary_sent_at ? timeAgo(r.summary_sent_at, i18n.language) : "–"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ settings */
function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["nightly-settings"], queryFn: () => get<{ settings: NightlySettings; customers: CustomerCfg[]; next: Latest["next"] }>("api/nightly/settings"), enabled: open });
  const [s, setS] = useState<NightlySettings | null>(null);
  const [tab, setTab] = useState<"general" | "customers">("general");
  useEffect(() => { if (q.data) setS(q.data.settings); }, [q.data]);
  const save = useMutation({
    mutationFn: (v: NightlySettings) => put("api/nightly/settings", v),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["nightly-settings"] }); qc.invalidateQueries({ queryKey: ["nightly"] }); toast.success(t("nightly.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const set = <K extends keyof NightlySettings>(k: K, v: NightlySettings[K]) => setS((x) => (x ? { ...x, [k]: v } : x));
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()} wide title={t("nightly.settings_title")} description={t("nightly.settings_desc")}
      footer={tab === "general" ? (
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" loading={save.isPending} disabled={!s} onClick={() => s && save.mutate(s)}>{!save.isPending && <Save />} {t("common.save")}</Button>
        </>
      ) : <Button onClick={onClose}>{t("common.close")}</Button>}>
      {!s || !q.data ? <Spinner /> : (
        <div className="space-y-5">
          <Segmented value={tab} onChange={setTab} options={[{ value: "general", label: t("nightly.s_general") }, { value: "customers", label: t("nightly.s_customers") }]} />
          {tab === "general" ? (
            <>
              <Switch checked={s.enabled} onCheckedChange={(v) => set("enabled", v)} label={t("nightly.s_enabled")} description={q.data.next.run ? t("nightly.s_next", { when: formatDate(q.data.next.run, i18n.language) }) : undefined} />
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label={t("nightly.s_run_at")} hint={t("nightly.s_local")}><Input type="time" value={s.run_at} onChange={(e) => set("run_at", e.target.value)} /></Field>
                <Field label={t("nightly.s_summary_at")} hint={t("nightly.s_summary_hint")}><Input type="time" value={s.summary_at} onChange={(e) => set("summary_at", e.target.value)} /></Field>
                <Field label={t("nightly.s_cert_days")}><Input type="number" min={1} max={120} value={s.cert_warn_days} onChange={(e) => set("cert_warn_days", Number(e.target.value) || 21)} /></Field>
              </div>
              <div>
                <div className="mb-2 text-[13px] font-semibold">{t("nightly.s_checks")}</div>
                <div className="grid gap-3 sm:grid-cols-2">
                  {CATEGORIES.map((c) => (
                    <Switch key={c} checked={s.checks[c] !== false} onCheckedChange={(v) => set("checks", { ...s.checks, [c]: v })} label={t(`nightly.cat.${c}`)} description={t(`nightly.cat_hint.${c}`)} />
                  ))}
                </div>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <Switch checked={s.cve_lookup} onCheckedChange={(v) => set("cve_lookup", v)} label={t("nightly.s_cve")} description={t("nightly.s_cve_hint")} />
                <Switch checked={s.proposals} onCheckedChange={(v) => set("proposals", v)} label={t("nightly.s_proposals")} description={t("nightly.s_proposals_hint")} />
                <Switch checked={s.use_window} onCheckedChange={(v) => set("use_window", v)} label={t("nightly.s_window")} description={t("nightly.s_window_hint")} />
              </div>
              <div>
                <div className="mb-2 text-[13px] font-semibold">{t("nightly.s_notify")}</div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Switch checked={s.notify_app} onCheckedChange={(v) => set("notify_app", v)} label={t("nightly.s_app")} />
                  <Switch checked={s.notify_mail} onCheckedChange={(v) => set("notify_mail", v)} label={t("nightly.s_mail")} />
                  <Switch checked={s.mail_when_clean} onCheckedChange={(v) => set("mail_when_clean", v)} label={t("nightly.s_clean")} />
                </div>
                <Field className="mt-3" label={t("nightly.s_mail_to")} hint={t("nightly.s_mail_to_hint")}>
                  <Input value={s.mail_to} onChange={(e) => set("mail_to", e.target.value)} placeholder="it@example.nl" />
                </Field>
              </div>
            </>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-muted">{t("nightly.s_customers_hint")}</p>
              {q.data.customers.map((c) => <CustomerSettings key={c.id} c={c} />)}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

function CustomerSettings({ c }: { c: CustomerCfg }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [enabled, setEnabled] = useState(c.enabled);
  const [eps, setEps] = useState(c.cert_endpoints.join("\n"));
  const dirty = enabled !== c.enabled || eps.trim() !== c.cert_endpoints.join("\n");
  const save = useMutation({
    mutationFn: () => put(`api/nightly/customers/${c.id}`, { enabled, cert_endpoints: eps.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["nightly-settings"] }); toast.success(t("nightly.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div className="rounded-md border border-line p-4">
      <div className="flex items-center justify-between gap-3">
        <Switch checked={enabled} onCheckedChange={setEnabled} label={c.name} description={c.maintenance_window ? t("nightly.window", { window: c.maintenance_window }) : t("nightly.no_window")} />
        <Button size="sm" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>{!save.isPending && <Save />} {t("common.save")}</Button>
      </div>
      <Field className="mt-3" label={t("nightly.s_endpoints")} hint={t("nightly.s_endpoints_hint")}>
        <Textarea rows={2} value={eps} onChange={(e) => setEps(e.target.value)} placeholder={"portal.example.nl\nmail.example.nl:993"} className="mono text-[13px]" />
      </Field>
    </div>
  );
}
