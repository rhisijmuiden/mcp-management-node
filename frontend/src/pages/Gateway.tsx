import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  Ban,
  Bot,
  Building2,
  CheckCircle2,
  Clock,
  Gauge,
  Hand,
  History,
  KeyRound,
  Mail,
  OctagonPause,
  Play,
  Plus,
  ShieldCheck,
  ShieldQuestion,
  Trash2,
  XCircle,
  Zap,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { CustomerKindIcon } from "@/components/customers";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { CustomerSummary, SystemInfo } from "@/lib/types";
import { appUrl, cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
export type Risk = "read" | "operate" | "change" | "destructive";
type Act = "allow" | "approve" | "block";
const RISKS: Risk[] = ["read", "operate", "change", "destructive"];

interface Reason { code: string; params: Record<string, string> }

export interface ApprovalRow {
  id: string;
  created_at: string;
  expires_at: string;
  status: "pending" | "approved" | "running" | "executed" | "failed" | "rejected" | "expired";
  tool: string;
  risk: Risk;
  target: string | null;
  customer_ids: string[];
  customer_names: string[];
  reasons: Reason[];
  reason_texts: string[];
  ai_reason: string | null;
  token_id: string | null;
  token_name: string | null;
  username: string | null;
  client: { name?: string; version?: string };
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  executed_at: string | null;
  error: string | null;
  args?: Record<string, unknown>;
  result?: unknown;
  description?: string | null;
}

interface Policy {
  scope: "global" | "customer" | "token";
  scope_id: string | null;
  risks: Partial<Record<Risk, string>>;
  tools: Record<string, string>;
  outside_window: string;
  paused: boolean;
  updated_at: string | null;
  updated_by: string | null;
  exists: boolean;
}

interface ToolInfo { name: string; risk: Risk; scope: string; description: string }

interface ActivityRow {
  id: number;
  at: string;
  tool: string;
  risk: Risk | null;
  target: string | null;
  username: string | null;
  token: string | null;
  client: string | null;
  state: "ok" | "failed" | "pending" | "blocked";
  approval_id: string | null;
  error: string | null;
  duration_ms: number | null;
}

interface SessionRow {
  token_id: string | null;
  token_name: string | null;
  username: string | null;
  client: string | null;
  started_at: string;
  last_at: string;
  calls: number;
  failed: number;
  pending: number;
  tools: [string, number][];
  targets: string[];
}

interface GatewayOverview {
  paused: boolean;
  pause_note: string | null;
  paused_by: string | null;
  paused_at: string | null;
  pending_count: number;
  customers: CustomerSummary[];
  clients: { token_id: string; name: string; kind: string; username: string; client: string | null; last_used_at: string; customer_ids: string[] | null; active: boolean }[];
  guardrails: { global: Policy; customer_policies: number; token_policies: number; tools_per_risk: Partial<Record<Risk, number>> };
  approvals?: ApprovalRow[];
  stats?: { calls_24h: number; failed_24h: number; blocked_24h: number; changes_24h: number; approved_24h: number; rejected_24h: number; active_sessions: number };
  activity?: ActivityRow[];
  sessions?: SessionRow[];
}

export interface GatewayStatus { paused: boolean; pause_note: string | null; paused_by: string | null }

/* ------------------------------------------------------------------ small pieces */
const riskTone = { read: "neutral", operate: "info", change: "warning", destructive: "offline" } as const;
const actTone = { allow: "online", approve: "warning", block: "offline", inherit: "neutral", paused: "offline", ignore: "neutral" } as const;

export function RiskBadge({ risk }: { risk: Risk | null | undefined }) {
  const { t } = useTranslation();
  if (!risk) return null;
  return <Badge tone={riskTone[risk]}>{t(`gw.risk.${risk}`)}</Badge>;
}

function ActBadge({ action }: { action: string }) {
  const { t } = useTranslation();
  return <Badge tone={actTone[action as keyof typeof actTone] ?? "neutral"}>{t(`gw.action.${action}`)}</Badge>;
}

const statusTone = { pending: "warning", approved: "info", running: "info", executed: "online", failed: "offline", rejected: "neutral", expired: "neutral" } as const;

function StatusBadge({ status }: { status: ApprovalRow["status"] }) {
  const { t } = useTranslation();
  return <Badge tone={statusTone[status]} dot>{t(`gw.status.${status}`)}</Badge>;
}

function ReasonLine({ r }: { r: Reason }) {
  const { t } = useTranslation();
  const p = r.params || {};
  const vars = {
    ...p,
    risk: p.risk ? t(`gw.risk.${p.risk}`) : "",
    action: p.action ? t(`gw.action.${p.action}`) : "",
    where: p.customer ? t("gw.reason.where_customer", { customer: p.customer }) : p.layer === "token" ? t("gw.reason.where_token") : t("gw.reason.where_default"),
  };
  const key = ["risk", "tool", "outside_window", "paused", "paused_customer", "paused_token"].includes(r.code) ? r.code : "other";
  return <span>{t(`gw.reason.${key}`, vars)}</span>;
}

function who(a: { client?: { name?: string } | null; token_name?: string | null; username?: string | null }) {
  return a.client?.name || a.token_name || a.username || "AI";
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="mono max-h-72 overflow-auto rounded-md bg-sunken p-3 text-[12px] leading-5 whitespace-pre-wrap break-all">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}

function useGatewayInvalidate() {
  const qc = useQueryClient();
  return () => {
    for (const k of ["gateway-overview", "approvals", "approvals-count", "approval", "gateway-status", "gateway-policies", "gateway-settings"]) {
      qc.invalidateQueries({ queryKey: [k] });
    }
  };
}

/* ------------------------------------------------------------------ kill switch */
export function PauseControl({ paused, compact }: { paused: boolean; compact?: boolean }) {
  const { t } = useTranslation();
  const invalidate = useGatewayInvalidate();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [reads, setReads] = useState(false);
  const pause = useMutation({
    mutationFn: () => post("api/gateway/pause", { note, pause_reads: reads }),
    onSuccess: () => { invalidate(); setOpen(false); toast.success(t("gw.pause.paused_toast")); },
  });
  const resume = useMutation({
    mutationFn: () => post("api/gateway/resume"),
    onSuccess: () => { invalidate(); toast.success(t("gw.pause.resumed_toast")); },
  });
  if (paused) {
    return (
      <Button size={compact ? "sm" : "md"} variant="dark" loading={resume.isPending} onClick={() => resume.mutate()}>
        <Play /> {t("gw.pause.resume")}
      </Button>
    );
  }
  return (
    <>
      <Button size={compact ? "sm" : "md"} variant="danger" onClick={() => { setNote(""); setReads(false); setOpen(true); }}>
        <OctagonPause /> {t("gw.pause.button")}
      </Button>
      <Dialog open={open} onOpenChange={setOpen} title={t("gw.pause.title")} description={t("gw.pause.desc")}
        footer={<><Button onClick={() => setOpen(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={pause.isPending} onClick={() => pause.mutate()}><OctagonPause /> {t("gw.pause.confirm")}</Button></>}>
        <div className="space-y-4">
          <Field label={t("gw.pause.note")} hint={t("gw.pause.note_hint")}>
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("gw.pause.note_ph")} maxLength={500} />
          </Field>
          <Switch checked={reads} onCheckedChange={setReads} label={t("gw.pause.reads")} description={t("gw.pause.reads_hint")} />
        </div>
      </Dialog>
    </>
  );
}

/** Banner shown on every page while AI actions are paused. */
export function PausedBanner() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const st = useQuery({ queryKey: ["gateway-status"], queryFn: () => get<GatewayStatus>("api/gateway/status"), refetchInterval: 60_000 });
  if (!st.data?.paused) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-offline/30 bg-offline-soft px-4 py-2.5 text-sm lg:px-8">
      <OctagonPause className="size-4 text-offline" />
      <span className="flex-1">
        <span className="font-bold">{t("gw.pause.banner")}</span>{" "}
        <span className="text-muted">{st.data.paused_by && t("gw.pause.banner_by", { name: st.data.paused_by })}{st.data.pause_note ? ` · ${st.data.pause_note}` : ""}</span>
      </span>
      {user?.role === "admin" && <PauseControl paused compact />}
    </div>
  );
}

/* ------------------------------------------------------------------ approval dialog */
export function ApprovalDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useGatewayInvalidate();
  const [note, setNote] = useState("");
  useEffect(() => setNote(""), [id]);
  const q = useQuery({
    queryKey: ["approval", id],
    queryFn: () => get<ApprovalRow>(`api/gateway/approvals/${id}`),
    enabled: !!id,
    refetchInterval: (query) => (["approved", "running"].includes(query.state.data?.status ?? "") ? 1500 : false),
  });
  const decide = useMutation({
    mutationFn: (approve: boolean) => post<ApprovalRow>(`api/gateway/approvals/${id}/${approve ? "approve" : "reject"}`, { note: note || null }),
    onSuccess: (_, approve) => { invalidate(); toast.success(approve ? t("gw.appr.approved_toast") : t("gw.appr.rejected_toast")); },
  });
  const a = q.data;
  return (
    <Dialog open={!!id} onOpenChange={(v) => !v && onClose()} wide
      title={a ? <span className="flex flex-wrap items-center gap-2"><span className="mono">{a.tool}</span><RiskBadge risk={a.risk} /><StatusBadge status={a.status} /></span> : t("gw.appr.title")}
      description={a?.description ?? undefined}
      footer={a?.status === "pending" ? (
        <>
          <Button loading={decide.isPending && decide.variables === false} onClick={() => decide.mutate(false)}><XCircle /> {t("gw.appr.reject")}</Button>
          <Button variant="primary" loading={decide.isPending && decide.variables === true} onClick={() => decide.mutate(true)}><CheckCircle2 /> {t("gw.appr.approve")}</Button>
        </>
      ) : <Button onClick={onClose}>{t("common.close")}</Button>}>
      {!a ? <Spinner /> : (
        <div className="space-y-4 text-sm">
          {decide.isError && <Alert tone="offline">{errorMessage(decide.error, t)}</Alert>}
          <div className="grid gap-3 sm:grid-cols-2">
            <Info label={t("gw.appr.requested_by")}>{who(a)}{a.token_name && a.client?.name && a.token_name !== a.client.name ? <span className="text-muted"> · {a.token_name}</span> : null}{a.username ? <span className="text-muted"> · {a.username}</span> : null}</Info>
            <Info label={t("gw.appr.customer")}>{a.customer_names.length ? a.customer_names.join(", ") : <span className="text-muted">{t("gw.appr.no_customer")}</span>}</Info>
            <Info label={t("gw.appr.target")}>{a.target ?? "–"}</Info>
            <Info label={t("gw.appr.asked")}>{formatDate(a.created_at, i18n.language)} · {a.status === "pending" ? t("gw.appr.expires", { when: timeAgo(a.expires_at, i18n.language) }) : ""}</Info>
          </div>
          {a.ai_reason && (
            <div className="rounded-md border border-line bg-sunken/60 px-4 py-3">
              <div className="text-[12px] font-bold uppercase tracking-wider text-muted">{t("gw.appr.ai_reason")}</div>
              <div className="mt-1">{a.ai_reason}</div>
            </div>
          )}
          <div>
            <div className="mb-1 text-[13px] font-semibold">{t("gw.appr.why")}</div>
            <ul className="list-inside list-disc space-y-0.5 text-muted">{a.reasons.map((r, i) => <li key={i}><ReasonLine r={r} /></li>)}</ul>
          </div>
          <div>
            <div className="mb-1 text-[13px] font-semibold">{t("gw.appr.args")}</div>
            <JsonBlock value={a.args ?? {}} />
          </div>
          {a.status === "pending" && (
            <Field label={t("gw.appr.note")} hint={t("gw.appr.note_hint")}>
              <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
            </Field>
          )}
          {a.decided_by && (
            <Alert tone={a.status === "rejected" ? "neutral" : a.status === "failed" ? "offline" : "online"}
              title={t(a.status === "rejected" ? "gw.appr.rejected_by" : "gw.appr.approved_by", { name: a.decided_by, when: formatDate(a.decided_at, i18n.language) })}>
              {a.decision_note}
            </Alert>
          )}
          {["approved", "running"].includes(a.status) && <div className="flex items-center gap-2 text-muted"><Spinner className="size-4" /> {t("gw.appr.running")}</div>}
          {a.status === "failed" && a.error && <Alert tone="offline" title={t("gw.appr.failed")}>{a.error}</Alert>}
          {a.status === "executed" && a.result !== undefined && a.result !== null && (
            <div><div className="mb-1 text-[13px] font-semibold">{t("gw.appr.result")}</div><JsonBlock value={a.result} /></div>
          )}
        </div>
      )}
    </Dialog>
  );
}

function Info({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div>
      <div className="text-[12px] font-semibold text-muted">{label}</div>
      <div className="mt-0.5 break-words">{children}</div>
    </div>
  );
}

function ApprovalLine({ a, onOpen }: { a: ApprovalRow; onOpen: () => void }) {
  const { t, i18n } = useTranslation();
  return (
    <button onClick={onOpen} className="flex w-full cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 text-left text-sm hover:bg-sunken/60">
      <span className={cn("grid size-8 shrink-0 place-items-center rounded-md", a.status === "pending" ? "bg-warning-soft text-warning" : "bg-sunken text-muted")}><Hand className="size-4" /></span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="mono font-semibold">{a.tool}</span>
          <RiskBadge risk={a.risk} />
          {a.status !== "pending" && <StatusBadge status={a.status} />}
        </span>
        <span className="block truncate text-xs text-muted">
          {who(a)} · {a.target ?? a.customer_names.join(", ") ?? "–"}{a.ai_reason ? ` · “${a.ai_reason}”` : ""}
        </span>
      </span>
      <span className="text-xs text-muted">{timeAgo(a.created_at, i18n.language)}</span>
      {a.status === "pending" && <span className="text-xs font-semibold text-accent-text">{t("gw.appr.review")} →</span>}
    </button>
  );
}

/* ------------------------------------------------------------------ command center */
export function CommandCenterPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const ov = useQuery({ queryKey: ["gateway-overview"], queryFn: () => get<GatewayOverview>("api/gateway/overview"), refetchInterval: 20_000 });
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => get<SystemInfo>("api/system/info") });
  const [openId, setOpenId] = useState<string | null>(null);
  const d = ov.data;
  const greeting = new Date().getHours() < 12 ? t("dashboard.morning") : new Date().getHours() < 18 ? t("dashboard.afternoon") : t("dashboard.evening");
  const url = appUrl("/mcp");
  const attention = (d?.customers ?? []).filter((c) => c.status === "attention").length;

  return (
    <>
      <PageHeader
        title={`${greeting}, ${user?.full_name?.split(" ")[0] || user?.username}`}
        description={t("gw.cc.subtitle")}
        actions={admin && d ? <PauseControl paused={d.paused} /> : undefined}
      />
      {!d ? <Spinner /> : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {admin && d.stats ? (
              <>
                <Link to="/ai/approvals"><Stat label={t("gw.cc.pending")} value={d.pending_count} hint={d.pending_count ? t("gw.cc.pending_hint") : t("gw.cc.pending_none")} tone={d.pending_count ? "accent" : "online"} icon={<Hand />} /></Link>
                <Stat label={t("gw.cc.calls")} value={d.stats.calls_24h} hint={t("gw.cc.calls_hint", { changes: d.stats.changes_24h, failed: d.stats.failed_24h })} tone="info" icon={<Zap />} />
                <Stat label={t("gw.cc.sessions")} value={d.stats.active_sessions} hint={t("gw.cc.sessions_hint", { count: d.clients.length })} tone={d.stats.active_sessions ? "online" : "neutral"} icon={<Bot />} />
                <Stat label={t("gw.cc.blocked")} value={d.stats.blocked_24h} hint={t("gw.cc.blocked_hint", { approved: d.stats.approved_24h, rejected: d.stats.rejected_24h })} tone={d.stats.blocked_24h ? "warning" : "neutral"} icon={<ShieldCheck />} />
              </>
            ) : (
              <>
                <Stat label={t("gw.cc.clients")} value={d.clients.length} tone="info" icon={<Bot />} />
                <Stat label={t("nav.customers")} value={d.customers.length} hint={attention ? t("gw.cc.attention", { count: attention }) : undefined} tone={attention ? "warning" : "online"} icon={<Building2 />} />
                <Stat label={t("dashboard.version")} value={<span className="mono text-[22px]">{info.data?.tag ?? "…"}</span>} tone="neutral" icon={<Gauge />} />
              </>
            )}
          </div>

          <div className="mt-6 grid gap-6 xl:grid-cols-3">
            <div className="min-w-0 space-y-6 xl:col-span-2">
              {admin && (
                <Card>
                  <CardHeader icon={<Hand />} title={t("gw.cc.approvals_title")} description={t("gw.cc.approvals_desc")}
                    action={<Link to="/ai/approvals" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
                  {d.approvals?.length ? (
                    <div className="divide-y divide-line">{d.approvals.map((a) => <ApprovalLine key={a.id} a={a} onOpen={() => setOpenId(a.id)} />)}</div>
                  ) : (
                    <CardBody className="flex items-center gap-3 text-sm text-muted"><CheckCircle2 className="size-5 text-online" /> {t("gw.cc.no_approvals")}</CardBody>
                  )}
                </Card>
              )}

              {admin && (
                <Card>
                  <CardHeader icon={<Activity />} title={t("gw.cc.activity_title")} description={t("gw.cc.activity_desc")}
                    action={<Link to="/ai/sessions" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
                  {d.activity?.length ? <ActivityList rows={d.activity} onOpenApproval={setOpenId} /> : (
                    <EmptyState icon={<Bot />} title={t("gw.cc.no_activity")} action={<Link to="/help/mcp"><Button>{t("ai.how_to_connect")}</Button></Link>}>{t("gw.cc.no_activity_hint")}</EmptyState>
                  )}
                </Card>
              )}

              <Card>
                <CardHeader icon={<Building2 />} title={t("gw.cc.reach_title")} description={t("gw.cc.reach_desc")}
                  action={<Link to="/customers" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
                {d.customers.length ? (
                  <div className="divide-y divide-line">
                    {d.customers.map((c) => (
                      <Link key={c.id} to={`/customers/${c.id}`} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm hover:bg-sunken/60">
                        <CustomerKindIcon kind={c.kind} className="size-4 text-muted" />
                        <span className="min-w-0 flex-1 truncate font-semibold">{c.name}</span>
                        <span className="text-muted">{t("gw.cc.machines", { online: c.online, total: c.machines })}</span>
                        {!!c.alerts && <Badge tone="offline">{t("gw.cc.alerts", { count: c.alerts })}</Badge>}
                        {!!c.unifi.connections && <Badge tone={c.unifi.errors ? "offline" : "neutral"}>UniFi {c.unifi.devices}</Badge>}
                        <Badge tone={c.status === "ok" ? "online" : c.status === "attention" ? "warning" : "neutral"} dot>{t(`gw.cc.status_${c.status}`)}</Badge>
                      </Link>
                    ))}
                  </div>
                ) : <CardBody className="text-sm text-muted">{t("gw.cc.no_customers")}</CardBody>}
              </Card>
            </div>

            <div className="min-w-0 space-y-6">
              <Card className="bg-nacht text-white">
                <CardBody className="py-5">
                  <div className="flex items-center gap-2 text-lucht"><Bot className="size-5" /><span className="text-sm font-bold uppercase tracking-wider">{t("gw.cc.endpoint")}</span></div>
                  <div className="display mt-3 text-xl">{t("gw.cc.endpoint_title")}</div>
                  <p className="mt-2 text-sm text-white/75">{t("gw.cc.endpoint_text")}</p>
                  <div className="mono mt-4 break-all rounded-md bg-white/10 px-3 py-2 text-[13px]">{url}</div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <CopyButton value={url} />
                    <Link to="/help/mcp" className="inline-flex h-8 items-center gap-2 rounded-md bg-koraal px-3 text-[13px] font-semibold text-nacht hover:brightness-95">
                      {t("dashboard.mcp_cta")} <ArrowRight className="size-4" />
                    </Link>
                  </div>
                </CardBody>
              </Card>

              <Card>
                <CardHeader icon={<Bot />} title={t("gw.cc.clients_title")} action={<Link to="/ai/connections" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
                {d.clients.length ? (
                  <div className="divide-y divide-line">
                    {d.clients.slice(0, 6).map((c) => (
                      <div key={c.token_id} className="flex items-center gap-3 px-5 py-2.5 text-sm">
                        <span className={cn("size-2 shrink-0 rounded-full", c.active ? "bg-online" : "bg-line")} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-semibold">{c.client || c.name}</span>
                          <span className="block truncate text-xs text-muted">{c.client && c.client !== c.name ? `${c.name} · ` : ""}{c.customer_ids ? t("gw.cc.limited", { count: c.customer_ids.length }) : t("cust.all_customers")}</span>
                        </span>
                        <span className="text-xs text-muted">{timeAgo(c.last_used_at, i18n.language)}</span>
                      </div>
                    ))}
                  </div>
                ) : <CardBody className="text-sm text-muted">{t("dashboard.ai_none")}</CardBody>}
              </Card>

              <Card>
                <CardHeader icon={<ShieldCheck />} title={t("gw.cc.guardrails_title")} action={admin ? <Link to="/ai/policies" className="text-sm font-semibold text-accent-text hover:underline">{t("gw.cc.edit")}</Link> : undefined} />
                <CardBody className="space-y-2.5 text-sm">
                  {RISKS.map((r) => (
                    <div key={r} className="flex items-center gap-2">
                      <RiskBadge risk={r} />
                      <span className="text-xs text-muted">{t("gw.cc.tools_count", { count: d.guardrails.tools_per_risk[r] ?? 0 })}</span>
                      <span className="ml-auto"><ActBadge action={d.guardrails.global.risks[r] ?? "allow"} /></span>
                    </div>
                  ))}
                  <div className="flex items-center gap-2 border-t border-line pt-2.5">
                    <Clock className="size-4 text-muted" /><span className="flex-1">{t("gw.cc.outside_window")}</span>
                    <ActBadge action={d.guardrails.global.outside_window} />
                  </div>
                  <p className="text-xs text-muted">{t("gw.cc.overrides", { customers: d.guardrails.customer_policies, tokens: d.guardrails.token_policies })}</p>
                </CardBody>
              </Card>

              {admin && !!d.sessions?.length && (
                <Card>
                  <CardHeader icon={<History />} title={t("gw.cc.recent_sessions")} action={<Link to="/ai/sessions" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
                  <div className="divide-y divide-line">
                    {d.sessions.slice(0, 5).map((s, i) => (
                      <div key={i} className="px-5 py-2.5 text-sm">
                        <div className="flex items-center gap-2"><span className="font-semibold">{s.client || s.token_name || s.username}</span><span className="ml-auto text-xs text-muted">{timeAgo(s.last_at, i18n.language)}</span></div>
                        <div className="text-xs text-muted">{t("gw.sess.calls", { count: s.calls })}{s.targets.length ? ` · ${s.targets.slice(0, 3).join(", ")}` : ""}</div>
                      </div>
                    ))}
                  </div>
                </Card>
              )}
            </div>
          </div>
        </>
      )}
      <ApprovalDialog id={openId} onClose={() => setOpenId(null)} />
    </>
  );
}

function ActivityList({ rows, onOpenApproval }: { rows: ActivityRow[]; onOpenApproval?: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const icon = { ok: <CheckCircle2 className="size-4 text-online" />, failed: <XCircle className="size-4 text-offline" />, pending: <Hand className="size-4 text-warning" />, blocked: <Ban className="size-4 text-offline" /> };
  return (
    <div className="divide-y divide-line">
      {rows.map((r) => (
        <div key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2.5 text-sm">
          {icon[r.state]}
          <span className="mono font-semibold">{r.tool}</span>
          <RiskBadge risk={r.risk} />
          {r.state === "pending" && r.approval_id && onOpenApproval ? (
            <button className="cursor-pointer text-xs font-semibold text-accent-text hover:underline" onClick={() => onOpenApproval(r.approval_id!)}>{t("gw.state.pending")}</button>
          ) : r.state !== "ok" ? <span className="text-xs text-muted">{t(`gw.state.${r.state}`)}</span> : null}
          {r.approval_id && r.state === "ok" && <Badge tone="info">{t("gw.state.approved_run")}</Badge>}
          <span className="min-w-0 flex-1 truncate text-muted">{r.target ?? ""}</span>
          <span className="text-xs text-muted">{r.client || r.token} · {timeAgo(r.at, i18n.language)}</span>
          {r.error && r.state !== "pending" && <div className="w-full truncate pl-7 text-xs text-offline">{r.error}</div>}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ approvals page */
export function ApprovalsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState<"pending" | "decided" | "all">("pending");
  const rows = useQuery({ queryKey: ["approvals", state], queryFn: () => get<ApprovalRow[]>(`api/gateway/approvals?state=${state}`), refetchInterval: 15_000 });
  const openId = params.get("id");
  const setOpen = (id: string | null) => {
    const p = new URLSearchParams(params);
    if (id) p.set("id", id); else p.delete("id");
    setParams(p, { replace: true });
  };
  return (
    <>
      <PageHeader title={t("gw.appr.page_title")} description={t("gw.appr.page_desc")}
        actions={<Link to="/ai/policies"><Button><ShieldCheck /> {t("nav.ai_policies")}</Button></Link>} />
      <div className="mb-4">
        <Segmented value={state} onChange={setState} options={[
          { value: "pending", label: t("gw.appr.tab_pending") },
          { value: "decided", label: t("gw.appr.tab_decided") },
          { value: "all", label: t("gw.appr.tab_all") },
        ]} />
      </div>
      <Card>
        {rows.isLoading ? <CardBody><Spinner /></CardBody> : !rows.data?.length ? (
          <EmptyState icon={<CheckCircle2 />} title={state === "pending" ? t("gw.appr.empty_pending") : t("gw.appr.empty")}>{t("gw.appr.empty_hint")}</EmptyState>
        ) : (
          <div className="divide-y divide-line">{rows.data.map((a) => <ApprovalLine key={a.id} a={a} onOpen={() => setOpen(a.id)} />)}</div>
        )}
      </Card>
      <Alert tone="info" className="mt-6" title={t("gw.appr.how_title")}>{t("gw.appr.how_text")}</Alert>
      <ApprovalDialog id={openId} onClose={() => setOpen(null)} />
    </>
  );
}

/* ------------------------------------------------------------------ policies page */
interface PoliciesData {
  global: Policy;
  customers: { customer_id: string; name: string; kind: string; maintenance_window: string | null; window_understood: boolean | null; in_window: boolean | null; policy: Policy }[];
  tokens: { token_id: string; name: string; kind: string; username: string; client: string | null; last_used_at: string | null; expired: boolean; policy: Policy }[];
  tools: ToolInfo[];
}

function PolicyEditor({ scope, value, onChange, tools }: { scope: Policy["scope"]; value: Policy; onChange: (p: Policy) => void; tools: ToolInfo[] }) {
  const { t } = useTranslation();
  const [addTool, setAddTool] = useState("");
  const options = (scope === "global" ? ["allow", "approve", "block"] : ["inherit", "allow", "approve", "block"]).map((v) => ({ value: v, label: t(`gw.action.${v}`) }));
  const toolMap = useMemo(() => Object.fromEntries(tools.map((x) => [x.name, x])), [tools]);
  const setRisk = (r: Risk, v: string) => {
    const risks = { ...value.risks };
    if (v === "inherit") delete risks[r]; else risks[r] = v;
    onChange({ ...value, risks });
  };
  const setTool = (name: string, v: string | null) => {
    const next = { ...value.tools };
    if (v === null) delete next[name]; else next[name] = v;
    onChange({ ...value, tools: next });
  };
  return (
    <div className="space-y-5">
      <div className="space-y-2.5">
        {RISKS.map((r) => (
          <div key={r} className="flex flex-wrap items-center gap-3">
            <div className="w-44 min-w-0">
              <RiskBadge risk={r} />
              <div className="mt-1 text-xs text-muted">{t(`gw.risk_hint.${r}`)}</div>
            </div>
            <Segmented value={value.risks[r] ?? (scope === "global" ? "allow" : "inherit")} onChange={(v) => setRisk(r, v)} options={options} />
          </div>
        ))}
      </div>
      {scope !== "token" && (
        <Field label={t("gw.pol.outside_window")} hint={t("gw.pol.outside_window_hint")}>
          <Select value={value.outside_window} onChange={(e) => onChange({ ...value, outside_window: e.target.value })} className="max-w-xs">
            {scope !== "global" && <option value="inherit">{t("gw.action.inherit")}</option>}
            <option value="ignore">{t("gw.action.ignore")}</option>
            <option value="approve">{t("gw.action.approve")}</option>
            <option value="block">{t("gw.action.block")}</option>
          </Select>
        </Field>
      )}
      <div>
        <div className="mb-1 text-[13px] font-semibold">{t("gw.pol.tool_rules")}</div>
        <p className="mb-2 text-xs text-muted">{t("gw.pol.tool_rules_hint")}</p>
        <div className="space-y-2">
          {Object.entries(value.tools).map(([name, v]) => (
            <div key={name} className="flex flex-wrap items-center gap-2">
              <span className="mono min-w-44 text-[13px]">{name}</span>
              <RiskBadge risk={toolMap[name]?.risk} />
              <Segmented value={v} onChange={(nv) => setTool(name, nv)} options={options.filter((o) => o.value !== "inherit")} />
              <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => setTool(name, null)}><Trash2 /></Button>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2">
            <Select value={addTool} onChange={(e) => setAddTool(e.target.value)} className="max-w-xs">
              <option value="">{t("gw.pol.pick_tool")}</option>
              {RISKS.map((r) => (
                <optgroup key={r} label={t(`gw.risk.${r}`)}>
                  {tools.filter((x) => x.risk === r && !(x.name in value.tools)).map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
                </optgroup>
              ))}
            </Select>
            <Button size="sm" disabled={!addTool} onClick={() => { setTool(addTool, "approve"); setAddTool(""); }}><Plus /> {t("gw.pol.add_rule")}</Button>
          </div>
          {addTool && toolMap[addTool] && <p className="text-xs text-muted">{toolMap[addTool].description}</p>}
        </div>
      </div>
      {scope !== "global" && (
        <Switch checked={value.paused} onCheckedChange={(v) => onChange({ ...value, paused: v })}
          label={t(scope === "customer" ? "gw.pol.pause_customer" : "gw.pol.pause_token")} description={t("gw.pol.pause_hint")} />
      )}
    </div>
  );
}

function PolicySummary({ p }: { p: Policy }) {
  const { t } = useTranslation();
  if (!p.exists) return <span className="text-xs text-muted">{t("gw.pol.uses_defaults")}</span>;
  const risks = Object.entries(p.risks);
  return (
    <span className="flex flex-wrap items-center gap-1">
      {p.paused && <Badge tone="offline">{t("gw.pol.paused")}</Badge>}
      {risks.map(([r, a]) => <Badge key={r} tone={actTone[a as Act] ?? "neutral"}>{t(`gw.risk.${r}`)}: {t(`gw.action.${a}`)}</Badge>)}
      {!!Object.keys(p.tools).length && <Badge tone="info">{t("gw.pol.n_tool_rules", { count: Object.keys(p.tools).length })}</Badge>}
      {p.outside_window !== "inherit" && <Badge tone="neutral">{t("gw.pol.window_short")}: {t(`gw.action.${p.outside_window}`)}</Badge>}
      {!p.paused && !risks.length && !Object.keys(p.tools).length && p.outside_window === "inherit" && <span className="text-xs text-muted">{t("gw.pol.uses_defaults")}</span>}
    </span>
  );
}

export function PoliciesPage() {
  const { t, i18n } = useTranslation();
  const invalidate = useGatewayInvalidate();
  const q = useQuery({ queryKey: ["gateway-policies"], queryFn: () => get<PoliciesData>("api/gateway/policies") });
  const [global, setGlobal] = useState<Policy | null>(null);
  useEffect(() => { if (q.data && !global) setGlobal(q.data.global); }, [q.data, global]);
  const saveGlobal = useMutation({
    mutationFn: () => put<Policy>("api/gateway/policies/global", { risks: global!.risks, tools: global!.tools, outside_window: global!.outside_window }),
    onSuccess: (p) => { setGlobal(p); invalidate(); toast.success(t("common.saved")); },
  });
  const [edit, setEdit] = useState<{ scope: "customer" | "token"; id: string; name: string; policy: Policy } | null>(null);
  const saveEdit = useMutation({
    mutationFn: () => put(`api/gateway/policies/${edit!.scope === "customer" ? "customers" : "tokens"}/${edit!.id}`,
      { risks: edit!.policy.risks, tools: edit!.policy.tools, outside_window: edit!.policy.outside_window, paused: edit!.policy.paused }),
    onSuccess: () => { invalidate(); setEdit(null); toast.success(t("common.saved")); },
  });
  const reset = useMutation({
    mutationFn: ({ scope, id }: { scope: string; id: string }) => del(`api/gateway/policies/${scope}/${id}`),
    onSuccess: () => { invalidate(); toast.success(t("gw.pol.reset_done")); },
  });
  if (!q.data || !global) return <Spinner />;
  const d = q.data;
  return (
    <>
      <PageHeader title={t("gw.pol.title")} description={t("gw.pol.desc")} />
      <div className="grid gap-6 xl:grid-cols-5">
        <div className="min-w-0 space-y-6 xl:col-span-3">
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("gw.pol.defaults")} description={t("gw.pol.defaults_desc")}
              action={<Button variant="primary" size="sm" loading={saveGlobal.isPending} onClick={() => saveGlobal.mutate()}>{t("common.save")}</Button>} />
            <CardBody>
              {saveGlobal.isError && <Alert tone="offline" className="mb-3">{errorMessage(saveGlobal.error, t)}</Alert>}
              <PolicyEditor scope="global" value={global} onChange={setGlobal} tools={d.tools} />
            </CardBody>
          </Card>

          <Card>
            <CardHeader icon={<Building2 />} title={t("gw.pol.customers")} description={t("gw.pol.customers_desc")} />
            <Table>
              <thead><tr><Th>{t("cust.customer")}</Th><Th>{t("gw.pol.window")}</Th><Th>{t("gw.pol.rules")}</Th><Th /></tr></thead>
              <tbody>
                {d.customers.map((c) => (
                  <Tr key={c.customer_id}>
                    <Td><span className="flex items-center gap-2 font-semibold"><CustomerKindIcon kind={c.kind} className="size-4 text-muted" />{c.name}</span></Td>
                    <Td>
                      {c.maintenance_window ? (
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm">{c.maintenance_window}</span>
                          {c.window_understood === false ? <Badge tone="warning">{t("gw.pol.window_unknown")}</Badge>
                            : c.in_window ? <Badge tone="online">{t("gw.pol.in_window")}</Badge> : <Badge tone="neutral">{t("gw.pol.outside")}</Badge>}
                        </span>
                      ) : <span className="text-xs text-muted">{t("gw.pol.no_window")}</span>}
                    </Td>
                    <Td><PolicySummary p={c.policy} /></Td>
                    <Td className="whitespace-nowrap text-right">
                      <Button size="sm" onClick={() => setEdit({ scope: "customer", id: c.customer_id, name: c.name, policy: c.policy })}>{t("common.edit")}</Button>
                      {c.policy.exists && <Button size="icon" variant="ghost" aria-label={t("gw.pol.reset")} title={t("gw.pol.reset")} onClick={() => reset.mutate({ scope: "customers", id: c.customer_id })}><Trash2 /></Button>}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Card>

          <Card>
            <CardHeader icon={<KeyRound />} title={t("gw.pol.tokens")} description={t("gw.pol.tokens_desc")} />
            {d.tokens.length ? (
              <Table>
                <thead><tr><Th>{t("gw.pol.token")}</Th><Th>{t("ai.last_used")}</Th><Th>{t("gw.pol.rules")}</Th><Th /></tr></thead>
                <tbody>
                  {d.tokens.map((x) => (
                    <Tr key={x.token_id}>
                      <Td><div className="font-semibold">{x.client || x.name}</div><div className="text-xs text-muted">{x.client && x.client !== x.name ? `${x.name} · ` : ""}{x.username}{x.expired ? ` · ${t("gw.pol.expired")}` : ""}</div></Td>
                      <Td className="text-sm text-muted">{x.last_used_at ? timeAgo(x.last_used_at, i18n.language) : "–"}</Td>
                      <Td><PolicySummary p={x.policy} /></Td>
                      <Td className="whitespace-nowrap text-right">
                        <Button size="sm" onClick={() => setEdit({ scope: "token", id: x.token_id, name: x.client || x.name, policy: x.policy })}>{t("common.edit")}</Button>
                        {x.policy.exists && <Button size="icon" variant="ghost" aria-label={t("gw.pol.reset")} title={t("gw.pol.reset")} onClick={() => reset.mutate({ scope: "tokens", id: x.token_id })}><Trash2 /></Button>}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : <CardBody className="text-sm text-muted">{t("gw.pol.no_tokens")}</CardBody>}
          </Card>
        </div>
        <div className="min-w-0 space-y-6 xl:col-span-2">
          <GatewaySettingsCard />
          <Card>
            <CardHeader icon={<ShieldQuestion />} title={t("gw.pol.how_title")} />
            <CardBody className="space-y-2 text-sm text-muted">
              <p>{t("gw.pol.how_1")}</p>
              <p>{t("gw.pol.how_2")}</p>
              <p>{t("gw.pol.how_3")}</p>
              <p>{t("gw.pol.how_4")}</p>
            </CardBody>
          </Card>
        </div>
      </div>
      <Dialog open={!!edit} onOpenChange={(v) => !v && setEdit(null)} wide
        title={edit ? t(edit.scope === "customer" ? "gw.pol.edit_customer" : "gw.pol.edit_token", { name: edit.name }) : ""}
        description={edit?.scope === "token" ? t("gw.pol.token_stricter") : t("gw.pol.customer_replaces")}
        footer={<><Button onClick={() => setEdit(null)}>{t("common.cancel")}</Button><Button variant="primary" loading={saveEdit.isPending} onClick={() => saveEdit.mutate()}>{t("common.save")}</Button></>}>
        {saveEdit.isError && <Alert tone="offline" className="mb-3">{errorMessage(saveEdit.error, t)}</Alert>}
        {edit && <PolicyEditor scope={edit.scope} value={edit.policy} onChange={(p) => setEdit({ ...edit, policy: p })} tools={d.tools} />}
      </Dialog>
    </>
  );
}

interface GatewaySettings {
  paused: boolean;
  approval_wait_seconds: number;
  approval_expiry_hours: number;
  notify_app: boolean;
  notify_mail: boolean;
  notify_mail_to: string;
  channels: string[];
  mail_enabled: boolean;
}

function GatewaySettingsCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["gateway-settings"], queryFn: () => get<GatewaySettings>("api/gateway/settings") });
  const [f, setF] = useState<GatewaySettings | null>(null);
  useEffect(() => { if (q.data && !f) setF(q.data); }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => put("api/gateway/settings", {
      approval_wait_seconds: Number(f!.approval_wait_seconds) || 0, approval_expiry_hours: Number(f!.approval_expiry_hours) || 24,
      notify_app: f!.notify_app, notify_mail: f!.notify_mail, notify_mail_to: f!.notify_mail_to,
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["gateway-settings"] }); toast.success(t("common.saved")); },
  });
  const test = useMutation({
    mutationFn: (ch: string) => post(`api/gateway/settings/test/${ch}`),
    onSuccess: () => toast.success(t("gw.set.test_sent")),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!f) return <Card><CardBody><Spinner /></CardBody></Card>;
  return (
    <Card>
      <CardHeader icon={<Mail />} title={t("gw.set.title")} description={t("gw.set.desc")}
        action={<Button size="sm" variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>} />
      <CardBody className="space-y-4">
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("gw.set.wait")} hint={t("gw.set.wait_hint")}>
            <Input type="number" min={0} max={120} value={f.approval_wait_seconds} onChange={(e) => setF({ ...f, approval_wait_seconds: Number(e.target.value) })} />
          </Field>
          <Field label={t("gw.set.expiry")} hint={t("gw.set.expiry_hint")}>
            <Input type="number" min={1} max={336} value={f.approval_expiry_hours} onChange={(e) => setF({ ...f, approval_expiry_hours: Number(e.target.value) })} />
          </Field>
        </div>
        <div className="flex items-start justify-between gap-3">
          <Switch checked={f.notify_app} onCheckedChange={(v) => setF({ ...f, notify_app: v })} label={t("gw.set.app")} description={t("gw.set.app_hint")} />
        </div>
        <div className="flex items-start justify-between gap-3">
          <Switch checked={f.notify_mail} onCheckedChange={(v) => setF({ ...f, notify_mail: v })} label={t("gw.set.mail")} description={f.mail_enabled ? t("gw.set.mail_hint") : t("gw.set.mail_off")} />
          <Button size="sm" disabled={!f.mail_enabled} loading={test.isPending && test.variables === "mail"} onClick={() => test.mutate("mail")}>{t("gw.set.test")}</Button>
        </div>
        {f.notify_mail && (
          <Field label={t("gw.set.mail_to")} hint={t("gw.set.mail_to_hint")}>
            <Input value={f.notify_mail_to} onChange={(e) => setF({ ...f, notify_mail_to: e.target.value })} placeholder="ops@example.com, me@example.com" />
          </Field>
        )}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ sessions page */
export function SessionsPage() {
  const { t, i18n } = useTranslation();
  const [days, setDays] = useState("7");
  const q = useQuery({ queryKey: ["gateway-sessions", days], queryFn: () => get<SessionRow[]>(`api/gateway/sessions?days=${days}`), refetchInterval: 30_000 });
  const [sel, setSel] = useState<SessionRow | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const calls = useQuery({
    queryKey: ["gateway-activity", sel?.token_id, sel?.started_at, sel?.last_at],
    queryFn: () => {
      const p = new URLSearchParams({ since: sel!.started_at, until: sel!.last_at, limit: "500" });
      if (sel!.token_id) p.set("token_id", sel!.token_id);
      return get<ActivityRow[]>(`api/gateway/activity?${p}`);
    },
    enabled: !!sel,
  });
  return (
    <>
      <PageHeader title={t("gw.sess.title")} description={t("gw.sess.desc")}
        actions={<Link to="/admin/audit"><Button><History /> {t("gw.sess.full_audit")}</Button></Link>} />
      <div className="mb-4">
        <Segmented value={days} onChange={setDays} options={[{ value: "1", label: t("gw.sess.d1") }, { value: "7", label: t("gw.sess.d7") }, { value: "30", label: t("gw.sess.d30") }]} />
      </div>
      <div className="grid gap-6 xl:grid-cols-5">
        <Card className="min-w-0 xl:col-span-2">
          <CardHeader icon={<History />} title={t("gw.sess.list")} />
          {q.isLoading ? <CardBody><Spinner /></CardBody> : !q.data?.length ? <EmptyState icon={<Bot />} title={t("gw.sess.empty")} /> : (
            <div className="divide-y divide-line">
              {q.data.map((s, i) => {
                const active = sel && sel.started_at === s.started_at && sel.token_id === s.token_id;
                return (
                  <button key={i} onClick={() => setSel(s)} className={cn("block w-full cursor-pointer px-5 py-3 text-left text-sm hover:bg-sunken/60", active && "bg-sunken")}>
                    <div className="flex items-center gap-2">
                      <Bot className="size-4 text-muted" />
                      <span className="font-semibold">{s.client || s.token_name || s.username}</span>
                      {!!s.pending && <Badge tone="warning">{t("gw.sess.pending", { count: s.pending })}</Badge>}
                      {!!s.failed && <Badge tone="offline">{t("gw.sess.failed", { count: s.failed })}</Badge>}
                      <span className="ml-auto text-xs text-muted">{timeAgo(s.last_at, i18n.language)}</span>
                    </div>
                    <div className="mt-1 text-xs text-muted">{formatDate(s.started_at, i18n.language)} · {t("gw.sess.calls", { count: s.calls })}{s.token_name && s.client && s.token_name !== s.client ? ` · ${s.token_name}` : ""}</div>
                    <div className="mt-1.5 flex flex-wrap gap-1">{s.tools.slice(0, 5).map(([n, c]) => <span key={n} className="mono rounded bg-sunken px-1.5 py-0.5 text-[11px]">{n}{c > 1 ? ` ×${c}` : ""}</span>)}</div>
                  </button>
                );
              })}
            </div>
          )}
        </Card>
        <Card className="min-w-0 xl:col-span-3">
          <CardHeader icon={<Activity />} title={sel ? t("gw.sess.calls_of", { name: sel.client || sel.token_name || sel.username }) : t("gw.sess.pick")}
            description={sel ? `${formatDate(sel.started_at, i18n.language)} – ${formatDate(sel.last_at, i18n.language)}` : undefined} />
          {!sel ? <CardBody className="text-sm text-muted">{t("gw.sess.pick_hint")}</CardBody>
            : calls.isLoading ? <CardBody><Spinner /></CardBody>
            : <ActivityList rows={[...(calls.data ?? [])].reverse()} onOpenApproval={setOpenId} />}
        </Card>
      </div>
      <ApprovalDialog id={openId} onClose={() => setOpenId(null)} />
    </>
  );
}
