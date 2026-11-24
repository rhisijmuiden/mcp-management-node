import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2, Eye, Hand, HeartPulse, Loader2, OctagonX, Pencil, Play, Plus, RefreshCw, Save, Settings2,
  ShieldCheck, Sparkles, Trash2, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
type Mode = "auto" | "approve" | "notify";
type Trigger = "alert" | "finding";
type AttemptStatus = "running" | "waiting_approval" | "verifying" | "succeeded" | "failed" | "cancelled" | "skipped" | "notified";

interface Settings {
  enabled: boolean;
  paused: boolean;
  paused_by: string | null;
  paused_at: string | null;
  pause_note: string;
  max_runs_per_hour: number;
  max_failures_per_hour: number;
  service_check_minutes: number;
  verify_minutes: number;
  notify_app: boolean;
  notify_mail: "always" | "failure" | "never";
  mail_to: string;
}
interface Status {
  settings: Settings;
  last_24h: Record<string, number>;
  last_24h_total: number;
  rules: number;
  rules_enabled: number;
  active: number;
  alert_kinds: string[];
  finding_codes: string[];
  machine_types: string[];
  network?: { local_agents: number; needs_agent_update: string[] };
}
interface Match { kinds?: string[]; services?: string[]; adapters?: string[]; local?: boolean; codes?: string[]; machine?: string; machine_types?: string[]; min_severity?: string }

const splitList = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
interface Rule {
  id: string;
  name: string;
  description: string | null;
  customer_id: string | null;
  customer_name: string | null;
  enabled: boolean;
  trigger: Trigger;
  match: Match;
  runbook_id: string | null;
  runbook_name: string | null;
  params: Record<string, string>;
  mode: Mode;
  delay_minutes: number;
  cooldown_minutes: number;
  max_attempts: number;
  preset: string | null;
  updated_at: string;
  updated_by: string | null;
  stats?: { attempts: number; succeeded: number; failed: number; last_at: string | null };
}
interface Attempt {
  id: string;
  rule_id: string | null;
  rule_name: string;
  customer_id: string | null;
  customer_name: string | null;
  source: Trigger;
  target: string | null;
  problem: string;
  status: AttemptStatus;
  reason: { code?: string; params?: Record<string, unknown> };
  run_id: string | null;
  runbook_name: string | null;
  resolved: boolean | null;
  created_at: string;
  finished_at: string | null;
}
interface Preset { key: string; name: string; description: string; trigger: Trigger; match: Match }
interface RunbookLite { id: string; name: string; category: string; customer_id: string | null; enabled: boolean; params: { name: string; label: string; required: boolean; default: string | null }[] }

const TONE: Record<AttemptStatus, "online" | "warning" | "offline" | "info" | "neutral" | "accent"> = {
  running: "info", verifying: "info", waiting_approval: "accent", succeeded: "online", failed: "offline",
  cancelled: "neutral", skipped: "warning", notified: "neutral",
};

function qs(path: string, params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => { for (const k of ["remediation-status", "remediation-attempts", "remediation-rules"]) qc.invalidateQueries({ queryKey: [k] }); };
}

export function ReasonText({ reason }: { reason: Attempt["reason"] }) {
  const { t } = useTranslation();
  if (!reason?.code) return null;
  const p = reason.params ?? {};
  return <>{t(`rem.reason.${reason.code}`, { ...p, error: String(p.error ?? "–"), defaultValue: reason.code })}</>;
}

/* ------------------------------------------------------------------ page */
export function RemediationPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [tab, setTab] = useState("activity");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const invalidate = useInvalidate();
  const status = useQuery({ queryKey: ["remediation-status"], queryFn: () => get<Status>("api/remediation/status"), refetchInterval: 30_000 });
  const st = status.data;
  const s = st?.settings;
  const resume = useMutation({
    mutationFn: () => post<Settings>("api/remediation/pause", { paused: false }),
    onSuccess: () => { invalidate(); toast.success(t("rem.resumed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const check = useMutation({
    mutationFn: () => post<{ started: number; finished: number; services_checked: number }>("api/remediation/run-now"),
    onSuccess: (d) => { invalidate(); toast.success(t("rem.checked", { started: d.started, services: d.services_checked })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });

  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><HeartPulse className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("rem.title")}</span></span>}
        description={t("rem.desc")}
        actions={admin && s && (
          <>
            <Button onClick={() => setSettingsOpen(true)}><Settings2 /> {t("rem.settings")}</Button>
            <Button loading={check.isPending} disabled={s.paused || !s.enabled} onClick={() => check.mutate()}>{!check.isPending && <RefreshCw />} {t("rem.check_now")}</Button>
            {s.paused
              ? <Button variant="primary" loading={resume.isPending} onClick={() => resume.mutate()}>{!resume.isPending && <Play />} {t("rem.resume")}</Button>
              : <Button variant="danger" onClick={() => setStopOpen(true)}><OctagonX /> {t("rem.stop")}</Button>}
          </>
        )} />
      {status.isLoading ? <Spinner /> : status.isError ? <Alert tone="offline">{errorMessage(status.error, t)}</Alert> : st && s && (
        <div className="space-y-5">
          {s.paused && (
            <Alert tone="offline" title={t("rem.paused_title")}>
              {t("rem.paused_body", { by: s.paused_by ?? "–", when: s.paused_at ? timeAgo(s.paused_at) : "–" })}
              {s.pause_note && <div className="mt-1 font-semibold">{s.pause_note}</div>}
            </Alert>
          )}
          {!s.enabled && !s.paused && <Alert tone="warning" title={t("rem.disabled_title")}>{t("rem.disabled_body")}</Alert>}
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <Stat label={t("rem.k_fixed")} value={st.last_24h.succeeded ?? 0} hint={t("rem.k_24h")} tone="online" icon={<CheckCircle2 />} />
            <Stat label={t("rem.k_failed")} value={(st.last_24h.failed ?? 0) + (st.last_24h.skipped ?? 0)} hint={t("rem.k_failed_hint")} tone={(st.last_24h.failed ?? 0) ? "offline" : "neutral"} icon={<XCircle />} />
            <Stat label={t("rem.k_active")} value={st.active} hint={t("rem.k_active_hint", { waiting: st.last_24h.waiting ?? 0 })} tone={st.active ? "accent" : "neutral"} icon={<Hand />} />
            <Stat label={t("rem.k_rules")} value={st.rules_enabled} hint={t("rem.k_rules_hint", { total: st.rules })} tone="info" icon={<ShieldCheck />} />
          </div>
          <Tabs.Root value={tab} onValueChange={setTab}>
            <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
              <Tabs.Trigger value="activity" className={tabCls}>{t("rem.tab_activity")}</Tabs.Trigger>
              <Tabs.Trigger value="rules" className={tabCls}>{t("rem.tab_rules")}</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="activity"><Activity /></Tabs.Content>
            <Tabs.Content value="rules"><Rules admin={admin} status={st} /></Tabs.Content>
          </Tabs.Root>
          <p className="text-xs text-muted"><ShieldCheck className="mr-1 inline size-3.5" />{t("rem.guardrails")}</p>
        </div>
      )}
      {admin && s && <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} settings={s} />}
      {admin && <StopDialog open={stopOpen} onClose={() => setStopOpen(false)} />}
    </div>
  );
}

function StopDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [note, setNote] = useState("");
  const invalidate = useInvalidate();
  const stop = useMutation({
    mutationFn: () => post<Settings & { cancelled: number }>("api/remediation/pause", { paused: true, note }),
    onSuccess: (d) => { invalidate(); toast.success(t("rem.stopped", { count: d.cancelled })); onClose(); setNote(""); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()} title={t("rem.stop_title")} description={t("rem.stop_desc")}
      footer={<><Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button><Button variant="danger" loading={stop.isPending} onClick={() => stop.mutate()}>{!stop.isPending && <OctagonX />} {t("rem.stop")}</Button></>}>
      <Field label={t("rem.stop_note")}><Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("rem.stop_note_ph")} maxLength={500} /></Field>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ activity */
function Activity() {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const [filter, setFilter] = useState<"all" | "active" | "failed">("all");
  const q = useQuery({
    queryKey: ["remediation-attempts", customerId, filter],
    queryFn: () => get<Attempt[]>(qs("api/remediation/attempts", { customer_id: customerId, status: filter === "all" ? undefined : filter, limit: 200 })),
    refetchInterval: 20_000,
  });
  return (
    <Card>
      <CardHeader icon={<HeartPulse />} title={t("rem.activity_title")} description={t("rem.activity_desc")}
        action={<Segmented value={filter} onChange={setFilter} options={[
          { value: "all", label: t("rem.f_all") }, { value: "active", label: t("rem.f_active") }, { value: "failed", label: t("rem.f_failed") },
        ]} />} />
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !q.data?.length ? (
        <EmptyState icon={<Sparkles />} title={t("rem.activity_empty")}>{t("rem.activity_empty_hint")}</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <thead><Tr><Th>{t("rem.col_when")}</Th><Th>{t("rem.col_target")}</Th><Th>{t("rem.col_problem")}</Th><Th>{t("rem.col_rule")}</Th><Th>{t("rem.col_status")}</Th></Tr></thead>
            <tbody>
              {q.data.map((a) => (
                <Tr key={a.id}>
                  <Td className="whitespace-nowrap text-muted" title={formatDate(a.created_at, i18n.language)}>{timeAgo(a.created_at, i18n.language)}</Td>
                  <Td><div className="font-semibold">{a.target || "–"}</div><div className="text-xs text-muted">{a.customer_name || "–"}</div></Td>
                  <Td className="max-w-md"><div className="text-sm">{a.problem || "–"}</div><div className="text-xs text-muted">{t(`rem.source.${a.source}`)}</div></Td>
                  <Td><div className="text-sm">{a.rule_name}</div>{a.run_id ? <Link className="text-xs text-accent-text hover:underline" to={`/runbooks/runs/${a.run_id}`}>{a.runbook_name || t("rem.open_run")}</Link> : <div className="text-xs text-muted">{a.runbook_name || ""}</div>}</Td>
                  <Td>
                    <Badge tone={TONE[a.status]} dot>{(a.status === "running" || a.status === "verifying") && <Loader2 className="size-3 animate-spin" />}{t(`rem.status.${a.status}`)}</Badge>
                    {a.reason?.code && <div className="mt-1 max-w-xs text-xs text-muted"><ReasonText reason={a.reason} /></div>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ rules */
function ruleSummary(r: Rule, t: (k: string, o?: Record<string, unknown>) => string): string {
  const m = r.match || {};
  if (r.trigger === "alert") {
    const kinds = (m.kinds ?? []).map((k) => t(`rem.kind.${k}`)).join(", ");
    const extra = [...(m.services ?? []), ...(m.adapters ?? [])];
    return (extra.length ? `${kinds}: ${extra.join(", ")}` : kinds) + (m.local ? ` · ${t("rem.local_short")}` : "");
  }
  return (m.codes ?? []).map((c) => t(`rem.code.${c}`, { defaultValue: c })).join(", ");
}

function Rules({ admin, status }: { admin: boolean; status: Status }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const [edit, setEdit] = useState<Rule | "new" | null>(null);
  const [preview, setPreview] = useState<Rule | null>(null);
  const rules = useQuery({ queryKey: ["remediation-rules", customerId], queryFn: () => get<Rule[]>(qs("api/remediation/rules", { customer_id: customerId })) });
  const presets = useQuery({ queryKey: ["remediation-presets", i18n.language], queryFn: () => get<Preset[]>(qs("api/remediation/presets", { lang: i18n.language })) });
  const toggle = useMutation({
    mutationFn: (r: Rule) => put<Rule>(`api/remediation/rules/${r.id}`, { ...bodyOf(r), enabled: !r.enabled }),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: (r: Rule) => del(`api/remediation/rules/${r.id}`),
    onSuccess: () => { invalidate(); toast.success(t("rem.deleted")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const usedPresets = new Set((rules.data ?? []).filter((r) => (r.customer_id ?? "") === (customerId ?? "")).map((r) => r.preset));

  const outdated = status.network?.needs_agent_update ?? [];
  return (
    <div className="space-y-5">
      {outdated.length > 0 && <Alert tone="warning">{t("rem.local_outdated", { machines: outdated.join(", ") })}</Alert>}
      <Card>
        <CardHeader icon={<ShieldCheck />} title={t("rem.rules_title")} description={t("rem.rules_desc")}
          action={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("rem.add_rule")}</Button>} />
        {rules.isLoading ? <CardBody><Spinner /></CardBody> : !rules.data?.length ? (
          <EmptyState icon={<HeartPulse />} title={t("rem.rules_empty")}>{t("rem.rules_empty_hint")}</EmptyState>
        ) : (
          <div className="divide-y divide-line">
            {rules.data.map((r) => (
              <div key={r.id} className="flex flex-col gap-3 px-5 py-4 lg:flex-row lg:items-center">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold">{r.name}</span>
                    <Badge tone={r.mode === "auto" ? "online" : r.mode === "approve" ? "accent" : "neutral"}>{t(`rem.mode.${r.mode}`)}</Badge>
                    <Badge tone="neutral">{r.customer_name ?? t("rem.all_customers")}</Badge>
                    {!r.enabled && <Badge tone="warning">{t("rem.off")}</Badge>}
                  </div>
                  <div className="mt-1 text-sm text-muted">
                    {t(`rem.when_${r.trigger}`, { what: ruleSummary(r, t) })}{r.match.machine ? ` · ${t("rem.on_machines", { pattern: r.match.machine })}` : ""}
                    {" → "}{r.runbook_id ? <Link className="text-accent-text hover:underline" to={`/runbooks/${r.runbook_id}`}>{r.runbook_name ?? "?"}</Link> : <span className="text-offline">{t("rem.no_runbook")}</span>}
                  </div>
                  <div className="mt-1 text-xs text-muted">
                    {t("rem.limits_line", { delay: r.delay_minutes, cooldown: r.cooldown_minutes, max: r.max_attempts })}
                    {r.stats && ` · ${t("rem.stats_line", { attempts: r.stats.attempts, ok: r.stats.succeeded, failed: r.stats.failed })}`}
                  </div>
                </div>
                {admin && (
                  <div className="flex shrink-0 items-center gap-2">
                    <Switch checked={r.enabled} onCheckedChange={() => toggle.mutate(r)} disabled={toggle.isPending} />
                    {r.trigger === "alert" && <Button size="icon" variant="ghost" title={t("rem.preview")} onClick={() => setPreview(r)}><Eye /></Button>}
                    <Button size="icon" variant="ghost" title={t("common.edit")} onClick={() => setEdit(r)}><Pencil /></Button>
                    <Button size="icon" variant="ghost" title={t("common.delete")} onClick={() => { if (window.confirm(t("rem.confirm_delete", { name: r.name }))) remove.mutate(r); }}><Trash2 /></Button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {admin && (
        <Card>
          <CardHeader icon={<Sparkles />} title={t("rem.presets_title")} description={t("rem.presets_desc")} />
          <CardBody className="grid gap-3 md:grid-cols-2">
            {(presets.data ?? []).map((p) => <PresetCard key={p.key} p={p} used={usedPresets.has(p.key)} customerId={customerId || null} />)}
          </CardBody>
        </Card>
      )}
      {edit && <RuleDialog rule={edit === "new" ? null : edit} status={status} onClose={() => setEdit(null)} />}
      {preview && <PreviewDialog rule={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}

function PresetCard({ p, used, customerId }: { p: Preset; used: boolean; customerId: string | null }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const qc = useQueryClient();
  const [services, setServices] = useState((p.match.services ?? []).join(", "));
  const [adapters, setAdapters] = useState((p.match.adapters ?? []).join(", "));
  const [local, setLocal] = useState(!!p.match.local);
  const network = !!p.match.kinds?.includes("network");
  const add = useMutation({
    mutationFn: () => post<Rule>(`api/remediation/presets/${p.key}`, {
      customer_id: customerId, lang: i18n.language,
      services: p.match.kinds?.includes("service") ? services.split(/[\s,]+/).filter(Boolean) : null,
      adapters: network ? splitList(adapters) : null, local: network ? local : null,
    }),
    onSuccess: () => { invalidate(); qc.invalidateQueries({ queryKey: ["runbooks"] }); toast.success(t("rem.preset_added", { name: p.name })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div className="flex flex-col rounded-lg border border-line p-4">
      <div className="font-bold">{p.name}</div>
      <div className="mt-1 flex-1 text-sm text-muted">{p.description}</div>
      {p.match.kinds?.includes("service") && (
        <Field label={t("rem.services")} hint={t("rem.services_hint")} className="mt-3">
          <Input value={services} onChange={(e) => setServices(e.target.value)} placeholder="nginx, postgresql" />
        </Field>
      )}
      {network && (
        <>
          <Field label={t("rem.adapters")} hint={t("rem.adapters_hint")} className="mt-3">
            <Input value={adapters} onChange={(e) => setAdapters(e.target.value)} placeholder="*X710*, Ethernet 2, eno1" />
          </Field>
          <div className="mt-3"><Checkbox checked={local} onCheckedChange={setLocal} label={t("rem.local")} description={t("rem.local_hint")} /></div>
        </>
      )}
      <div className="mt-3 flex items-center justify-between gap-2">
        <span className="text-xs text-muted">{customerId ? t("rem.preset_for_customer") : t("rem.preset_for_all")}</span>
        <Button size="sm" variant={used ? "secondary" : "dark"} loading={add.isPending} onClick={() => add.mutate()}>{!add.isPending && <Plus />} {used ? t("rem.add_again") : t("rem.add")}</Button>
      </div>
    </div>
  );
}

function bodyOf(r: Rule) {
  return {
    name: r.name, description: r.description, customer_id: r.customer_id, enabled: r.enabled, trigger: r.trigger, match: r.match,
    runbook_id: r.runbook_id, params: r.params, mode: r.mode, delay_minutes: r.delay_minutes, cooldown_minutes: r.cooldown_minutes,
    max_attempts: r.max_attempts,
  };
}

const PLACEHOLDERS: Record<Trigger, string[]> = {
  alert: ["{{machine.name}}", "{{alert.detail.service}}", "{{alert.detail.adapter}}", "{{alert.message}}", "{{customer.name}}"],
  finding: ["{{machine.name}}", "{{finding.target}}", "{{finding.params.disk}}", "{{customer.name}}"],
};

function RuleDialog({ rule, status, onClose }: { rule: Rule | null; status: Status; onClose: () => void }) {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const runbooks = useQuery({ queryKey: ["runbooks"], queryFn: () => get<RunbookLite[]>("api/runbooks") });
  const [f, setF] = useState(() => rule ? bodyOf(rule) : {
    name: "", description: "", customer_id: customerId || null, enabled: true, trigger: "alert" as Trigger,
    match: { kinds: ["service"], services: [] } as Match, runbook_id: null as string | null, params: {} as Record<string, string>,
    mode: "auto" as Mode, delay_minutes: 5, cooldown_minutes: 30, max_attempts: 3,
  });
  const [servicesText, setServicesText] = useState((f.match.services ?? []).join(", "));
  const [adaptersText, setAdaptersText] = useState((f.match.adapters ?? []).join(", "));
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((x) => ({ ...x, [k]: v }));
  const setMatch = (m: Partial<Match>) => setF((x) => ({ ...x, match: { ...x.match, ...m } }));
  const rbList = useMemo(() => (runbooks.data ?? []).filter((r) => r.enabled && (r.customer_id === null || r.customer_id === f.customer_id))
    .sort((a, b) => Number(b.category === "remediation") - Number(a.category === "remediation") || a.name.localeCompare(b.name)), [runbooks.data, f.customer_id]);
  const rb = rbList.find((r) => r.id === f.runbook_id);
  useEffect(() => {
    // Suggest the usual placeholders for a runbook's parameters.
    if (!rb) return;
    setF((x) => {
      const p: Record<string, string> = {};
      for (const prm of rb.params) {
        p[prm.name] = x.params[prm.name] ?? (prm.name === "server" || prm.name === "node" ? "{{machine.name}}" : prm.name === "service" || prm.name === "unit" ? "{{alert.detail.service}}" : prm.name === "adapter" ? "{{alert.detail.adapter}}" : "");
      }
      return { ...x, params: p };
    });
  }, [rb]);
  const toggleIn = (list: string[] | undefined, v: string) => (list ?? []).includes(v) ? (list ?? []).filter((x) => x !== v) : [...(list ?? []), v];
  const save = useMutation({
    mutationFn: () => {
      const match: Match = { ...f.match, services: servicesText.split(/[\s,]+/).filter(Boolean), adapters: splitList(adaptersText) };
      if (!(match.kinds ?? []).includes("network")) { delete match.adapters; delete match.local; }
      if (f.trigger === "finding") { delete match.kinds; delete match.services; delete match.adapters; delete match.local; } else delete match.codes;
      const body = { ...f, match, params: Object.fromEntries(Object.entries(f.params).filter(([, v]) => v !== "")) };
      return rule ? put<Rule>(`api/remediation/rules/${rule.id}`, body) : post<Rule>("api/remediation/rules", body);
    },
    onSuccess: () => { invalidate(); toast.success(t("rem.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const num = (v: string) => (v === "" ? 0 : Number(v));

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={rule ? t("rem.edit_rule") : t("rem.add_rule")} description={t("rem.rule_dialog_desc")}
      footer={<><Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{!save.isPending && <Save />} {t("common.save")}</Button></>}>
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("rem.f_name")}><Input value={f.name} onChange={(e) => set("name", e.target.value)} maxLength={120} /></Field>
          <Field label={t("rem.f_customer")}>
            <Select value={f.customer_id ?? ""} onChange={(e) => set("customer_id", e.target.value || null)}>
              <option value="">{t("rem.all_customers")}</option>
              {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
        </div>
        <Field label={t("rem.f_description")}><Textarea rows={2} value={f.description ?? ""} onChange={(e) => set("description", e.target.value)} /></Field>

        <div className="rounded-lg border border-line p-4">
          <div className="mb-3 font-bold">{t("rem.when")}</div>
          <Segmented value={f.trigger} onChange={(v) => set("trigger", v)} options={[{ value: "alert", label: t("rem.trigger.alert") }, { value: "finding", label: t("rem.trigger.finding") }]} />
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {f.trigger === "alert"
              ? status.alert_kinds.map((k) => <Checkbox key={k} checked={(f.match.kinds ?? []).includes(k)} onCheckedChange={() => setMatch({ kinds: toggleIn(f.match.kinds, k) })} label={t(`rem.kind.${k}`)} />)
              : status.finding_codes.map((c) => <Checkbox key={c} checked={(f.match.codes ?? []).includes(c)} onCheckedChange={() => setMatch({ codes: toggleIn(f.match.codes, c) })} label={t(`rem.code.${c}`, { defaultValue: c })} />)}
          </div>
          {f.trigger === "alert" && (f.match.kinds ?? []).includes("service") && (
            <Field label={t("rem.services")} hint={t("rem.services_hint")} className="mt-3">
              <Input value={servicesText} onChange={(e) => setServicesText(e.target.value)} placeholder="nginx, postgresql, Spooler" />
            </Field>
          )}
          {f.trigger === "alert" && (f.match.kinds ?? []).includes("network") && (
            <div className="mt-3 space-y-3">
              <Field label={t("rem.adapters")} hint={t("rem.adapters_hint")}>
                <Input value={adaptersText} onChange={(e) => setAdaptersText(e.target.value)} placeholder="*X710*, Ethernet 2, eno1" />
              </Field>
              <Checkbox checked={!!f.match.local} onCheckedChange={(v) => setMatch({ local: v })} label={t("rem.local")} description={t("rem.local_hint")} />
            </div>
          )}
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <Field label={t("rem.f_machine")} hint={t("rem.f_machine_hint")}><Input value={f.match.machine ?? ""} onChange={(e) => setMatch({ machine: e.target.value })} placeholder="web*, db01" /></Field>
            <Field label={t("rem.f_types")}>
              <div className="flex flex-wrap gap-3 pt-2">
                {status.machine_types.map((m) => <Checkbox key={m} checked={(f.match.machine_types ?? []).includes(m)} onCheckedChange={() => setMatch({ machine_types: toggleIn(f.match.machine_types, m) })} label={t(`rem.mtype.${m}`)} />)}
              </div>
            </Field>
          </div>
        </div>

        <div className="rounded-lg border border-line p-4">
          <div className="mb-3 font-bold">{t("rem.then")}</div>
          <Field label={t("rem.f_runbook")} hint={t("rem.f_runbook_hint")}>
            <Select value={f.runbook_id ?? ""} onChange={(e) => set("runbook_id", e.target.value || null)}>
              <option value="">{t("rem.choose_runbook")}</option>
              {rbList.map((r) => <option key={r.id} value={r.id}>{r.name}{r.category === "remediation" ? ` · ${t("rem.repair")}` : ""}</option>)}
            </Select>
          </Field>
          {rb && rb.params.length > 0 && (
            <div className="mt-3 space-y-3">
              {rb.params.map((p) => (
                <Field key={p.name} label={`${p.label}${p.required ? " *" : ""}`}>
                  <Input value={f.params[p.name] ?? ""} placeholder={p.default ?? ""} onChange={(e) => set("params", { ...f.params, [p.name]: e.target.value })} />
                </Field>
              ))}
              <div className="text-xs text-muted">{t("rem.placeholders")} {PLACEHOLDERS[f.trigger].map((x) => <code key={x} className="mono mr-1.5 rounded bg-sunken px-1">{x}</code>)}</div>
            </div>
          )}
          <div className="mt-4">
            <div className="mb-1.5 text-sm font-semibold">{t("rem.f_mode")}</div>
            <Segmented value={f.mode} onChange={(v) => set("mode", v)} options={[
              { value: "auto", label: t("rem.mode.auto") }, { value: "approve", label: t("rem.mode.approve") }, { value: "notify", label: t("rem.mode.notify") },
            ]} />
            <div className="mt-1.5 text-xs text-muted">{t(`rem.mode_hint.${f.mode}`)}</div>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label={t("rem.f_delay")} hint={t("rem.f_delay_hint")}><Input type="number" min={0} max={1440} value={f.delay_minutes} onChange={(e) => set("delay_minutes", num(e.target.value))} /></Field>
          <Field label={t("rem.f_cooldown")} hint={t("rem.f_cooldown_hint")}><Input type="number" min={1} max={10080} value={f.cooldown_minutes} onChange={(e) => set("cooldown_minutes", num(e.target.value))} /></Field>
          <Field label={t("rem.f_max")} hint={t("rem.f_max_hint")}><Input type="number" min={1} max={20} value={f.max_attempts} onChange={(e) => set("max_attempts", num(e.target.value))} /></Field>
        </div>
        <Switch checked={f.enabled} onCheckedChange={(v) => set("enabled", v)} label={t("rem.f_enabled")} />
      </div>
    </Dialog>
  );
}

function PreviewDialog({ rule, onClose }: { rule: Rule; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["remediation-preview", rule.id], queryFn: () => get<{ matches: { alert_id: string; message: string; target: string | null; customer: string | null; started_at: string; params: Record<string, unknown> | null; error: string | null }[] }>(`api/remediation/rules/${rule.id}/preview`) });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={t("rem.preview_title", { name: rule.name })} description={t("rem.preview_desc")}
      footer={<Button onClick={onClose}>{t("common.close")}</Button>}>
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : !q.data?.matches.length ? (
        <EmptyState icon={<CheckCircle2 />} title={t("rem.preview_none")} />
      ) : (
        <div className="space-y-3">
          {q.data.matches.map((m) => (
            <div key={m.alert_id} className="rounded-md border border-line p-3 text-sm">
              <div className="font-semibold">{m.target ?? "–"} <span className="font-normal text-muted">· {m.customer ?? "–"} · {timeAgo(m.started_at, i18n.language)}</span></div>
              <div>{m.message}</div>
              {m.error ? <div className="text-offline">{m.error}</div> : <pre className="mono mt-1 rounded bg-sunken p-2 text-[12px]">{JSON.stringify(m.params, null, 2)}</pre>}
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ settings */
function SettingsDialog({ open, onClose, settings }: { open: boolean; onClose: () => void; settings: Settings }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const [f, setF] = useState(settings);
  useEffect(() => { if (open) setF(settings); }, [open, settings]);
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setF((x) => ({ ...x, [k]: v }));
  const save = useMutation({
    mutationFn: () => put<Settings>("api/remediation/settings", {
      enabled: f.enabled, max_runs_per_hour: f.max_runs_per_hour, max_failures_per_hour: f.max_failures_per_hour,
      service_check_minutes: f.service_check_minutes, verify_minutes: f.verify_minutes, notify_app: f.notify_app,
      notify_mail: f.notify_mail, mail_to: f.mail_to,
    }),
    onSuccess: () => { invalidate(); toast.success(t("rem.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const n = (v: string) => (v === "" ? 0 : Number(v));
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()} wide title={t("rem.settings")} description={t("rem.settings_desc")}
      footer={<><Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{!save.isPending && <Save />} {t("common.save")}</Button></>}>
      <div className="space-y-5">
        <Switch checked={f.enabled} onCheckedChange={(v) => set("enabled", v)} label={t("rem.s_enabled")} description={t("rem.s_enabled_hint")} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("rem.s_runs")} hint={t("rem.s_runs_hint")}><Input type="number" min={1} max={500} value={f.max_runs_per_hour} onChange={(e) => set("max_runs_per_hour", n(e.target.value))} /></Field>
          <Field label={t("rem.s_failures")} hint={t("rem.s_failures_hint")}><Input type="number" min={1} max={100} value={f.max_failures_per_hour} onChange={(e) => set("max_failures_per_hour", n(e.target.value))} /></Field>
          <Field label={t("rem.s_verify")} hint={t("rem.s_verify_hint")}><Input type="number" min={1} max={240} value={f.verify_minutes} onChange={(e) => set("verify_minutes", n(e.target.value))} /></Field>
          <Field label={t("rem.s_services")} hint={t("rem.s_services_hint")}><Input type="number" min={1} max={60} value={f.service_check_minutes} onChange={(e) => set("service_check_minutes", n(e.target.value))} /></Field>
        </div>
        <div className="rounded-lg border border-line p-4 space-y-4">
          <div className="font-bold">{t("rem.s_notify")}</div>
          <Switch checked={f.notify_app} onCheckedChange={(v) => set("notify_app", v)} label={t("rem.s_notify_app")} />
          <div>
            <div className="mb-1.5 text-sm font-semibold">{t("rem.s_notify_mail")}</div>
            <Segmented value={f.notify_mail} onChange={(v) => set("notify_mail", v)} options={[
              { value: "always", label: t("rem.s_mail_always") }, { value: "failure", label: t("rem.s_mail_failure") }, { value: "never", label: t("rem.s_mail_never") },
            ]} />
          </div>
          <Field label={t("rem.s_mail_to")} hint={t("rem.s_mail_to_hint")}><Input value={f.mail_to} onChange={(e) => set("mail_to", e.target.value)} placeholder="it@example.com" /></Field>
        </div>
      </div>
    </Dialog>
  );
}
