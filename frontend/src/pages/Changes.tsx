import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, BellRing, CalendarClock, CalendarDays, CalendarRange, Check, ChevronLeft, ChevronRight, Clock, Copy, Info, Link2,
  ListChecks, Mail, Pencil, Play, Plus, Server, Settings2, Sparkles, Trash2, X, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
export type ChangeStatus = "draft" | "planned" | "in_progress" | "completed" | "failed" | "cancelled";
type Kind = "maintenance" | "update" | "change" | "migration" | "other";
type Impact = "none" | "minor" | "degraded" | "outage";
type Recipients = "contacts" | "portal" | "both" | "none";
const KINDS: Kind[] = ["maintenance", "update", "change", "migration", "other"];
const IMPACTS: Impact[] = ["none", "minor", "degraded", "outage"];
const RECIPIENTS: Recipients[] = ["both", "contacts", "portal", "none"];
const OFFSETS = [10080, 4320, 1440, 240, 60, 15];
const DURATIONS = [30, 60, 90, 120, 180, 240, 360, 480];
export const STATUS_TONE: Record<ChangeStatus, "online" | "warning" | "offline" | "info" | "neutral" | "accent"> = {
  draft: "neutral", planned: "info", in_progress: "accent", completed: "online", failed: "offline", cancelled: "neutral",
};
const IMPACT_TONE: Record<Impact, "online" | "warning" | "offline" | "neutral"> = { none: "online", minor: "neutral", degraded: "warning", outage: "offline" };

interface Notice { key: string; at: string; to?: number; ok?: boolean; skipped?: boolean; pending?: boolean; error?: string; recipients?: number }
interface Conflict { kind: string; severity: "warning" | "info"; window?: string | null; ref?: string; title?: string; starts_at?: string; name?: string; at?: string; minutes?: number; hours_left?: number; enabled?: boolean }
export interface Change {
  id: string; number: number; ref: string; customer_id: string; customer_name: string | null; title: string; description: string | null;
  kind: Kind; impact: Impact; status: ChangeStatus; starts_at: string; ends_at: string; minutes: number; machines: { id: string; name: string }[];
  outcome_note: string | null; internal_notes?: string | null; node_ids?: string[]; task_id?: string | null; task_name?: string | null;
  runbook_id?: string | null; runbook_name?: string | null; run_runbook?: boolean; runbook_run_id?: string | null; notify?: boolean;
  notify_before?: number[] | null; notices?: Notice[]; created_by?: string | null; updated_at?: string; updated_by?: string | null;
}
interface Detail extends Change {
  conflicts: Conflict[]; recipients: { email: string; lang: string; source: string }[]; maintenance_window: string | null;
  effective: { notify_before: number[]; notify_after: boolean; recipients: Recipients }; runbook_run_status?: string | null;
}
interface TaskRunAt { task_id: string; name: string; kind: string; at: string; customer_ids: string[] | null }
interface Calendar { changes: Change[]; tasks: TaskRunAt[]; windows: { customer_id: string; customer_name: string; window: string | null; understood: boolean }[]; timezone: string }
interface Check { conflicts: Conflict[]; suggestion: { starts_at: string; ends_at: string } | null; maintenance_window: string | null; minutes: number }
interface Settings { notify_before: number[]; notify_after: boolean; notify_cancel: boolean; recipients: Recipients; auto_status: boolean; default_minutes: number; ical_url: string | null }
interface CustomerSettings {
  customer_id: string; customer_name: string; maintenance_window: string | null; notify_before: number[] | null; notify_after: boolean | null;
  recipients: Recipients | null; extra: string[]; lang: "en" | "nl" | null; ical_url: string | null;
  effective: { notify_before: number[]; notify_after: boolean; recipients: Recipients }; recipient_list: { email: string; lang: string; source: string }[];
}

const TABS = ["calendar", "list", "settings"] as const;
type Tab = (typeof TABS)[number];

/* ------------------------------------------------------------------ helpers */
const loc = (lang: string) => (lang === "nl" ? "nl-NL" : "en-GB");

export function useOffsetLabel() {
  const { t } = useTranslation();
  return (m: number) => (m % 1440 === 0 ? t("chg.days", { count: m / 1440 }) : m % 60 === 0 ? t("chg.hours", { count: m / 60 }) : t("chg.minutes", { count: m }));
}

export function whenText(c: { starts_at: string; ends_at: string }, lang: string) {
  const s = new Date(c.starts_at), e = new Date(c.ends_at);
  const day = (d: Date) => d.toLocaleDateString(loc(lang), { weekday: "short", day: "numeric", month: "short" });
  const time = (d: Date) => d.toLocaleTimeString(loc(lang), { hour: "2-digit", minute: "2-digit" });
  return s.toDateString() === e.toDateString() || (e.getTime() - s.getTime() <= 86400000 && e.getHours() === 0 && e.getMinutes() === 0)
    ? `${day(s)} ${time(s)}–${time(e)}` : `${day(s)} ${time(s)} – ${day(e)} ${time(e)}`;
}

function toLocalInput(iso: string) {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function startOfWeek(d: Date) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

function useInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["changes"] });
}

function conflictText(c: Conflict, t: (k: string, o?: Record<string, unknown>) => string, lang: string) {
  switch (c.kind) {
    case "outside_window": return t("chg.cf.outside_window", { window: c.window ?? "" });
    case "no_window": return t(c.window ? "chg.cf.window_unreadable" : "chg.cf.no_window", { window: c.window ?? "" });
    case "overlap": return t("chg.cf.overlap", { ref: c.ref, title: c.title, when: c.starts_at ? formatDate(c.starts_at, lang) : "" });
    case "task": return t("chg.cf.task", { name: c.name, when: c.at ? formatDate(c.at, lang) : "" });
    case "task_outside": return t(c.enabled === false ? "chg.cf.task_disabled" : "chg.cf.task_outside", { name: c.name });
    case "short_notice": return t("chg.cf.short_notice", { hours: c.hours_left });
    case "past": return t("chg.cf.past");
    default: return c.kind;
  }
}

function Conflicts({ items, empty }: { items: Conflict[]; empty?: ReactNode }) {
  const { t, i18n } = useTranslation();
  if (!items.length) return empty ? <Alert tone="online">{empty}</Alert> : null;
  const sorted = [...items].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "warning" ? -1 : 1));
  return (
    <ul className="space-y-1.5">
      {sorted.map((c, i) => (
        <li key={i} className={cn("flex items-start gap-2 rounded-md px-3 py-2 text-sm", c.severity === "warning" ? "bg-warning-soft" : "bg-info-soft")}>
          {c.severity === "warning" ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" /> : <Info className="mt-0.5 size-4 shrink-0 text-accent-text" />}
          <span>{conflictText(c, t, i18n.language)}</span>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ page */
export function ChangesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const params = useParams();
  const navigate = useNavigate();
  const tab: Tab = (TABS as readonly string[]).includes(params.tab ?? "") ? (params.tab as Tab) : "calendar";
  const [editing, setEditing] = useState<{ change?: Detail; start?: string } | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><CalendarRange className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("chg.title")}</span></span>}
        description={t("chg.desc")}
        actions={admin ? <Button variant="primary" onClick={() => setEditing({})}><Plus /> {t("chg.new")}</Button> : undefined} />
      <Tabs.Root value={tab} onValueChange={(v) => navigate(`/changes/${v}`)}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          {TABS.filter((x) => x !== "settings" || admin).map((x) => <Tabs.Trigger key={x} value={x} className={tabCls}>{t(`chg.tab.${x}`)}</Tabs.Trigger>)}
        </Tabs.List>
        <Tabs.Content value="calendar"><CalendarTab onOpen={setOpenId} onNew={admin ? (start) => setEditing({ start }) : undefined} /></Tabs.Content>
        <Tabs.Content value="list"><ListTab onOpen={setOpenId} /></Tabs.Content>
        {admin && <Tabs.Content value="settings"><SettingsTab /></Tabs.Content>}
      </Tabs.Root>
      {openId && <DetailDialog id={openId} admin={admin} onClose={() => setOpenId(null)} onEdit={(c) => { setOpenId(null); setEditing({ change: c }); }} />}
      {editing && <EditDialog init={editing} onClose={() => setEditing(null)} onSaved={(c) => { setEditing(null); setOpenId(c.id); }} />}
    </div>
  );
}

/* ------------------------------------------------------------------ calendar */
function ChangeChip({ c, onClick, compact }: { c: Change; onClick: () => void; compact?: boolean }) {
  const { i18n } = useTranslation();
  const time = new Date(c.starts_at).toLocaleTimeString(loc(i18n.language), { hour: "2-digit", minute: "2-digit" });
  const tone: Record<ChangeStatus, string> = {
    draft: "border-dashed border-line bg-raised text-muted", planned: "border-lucht/60 bg-info-soft text-ink",
    in_progress: "border-koraal bg-koraal/20 text-ink", completed: "border-online/40 bg-online-soft text-ink",
    failed: "border-offline/50 bg-offline-soft text-ink", cancelled: "border-line bg-sunken text-muted line-through",
  };
  return (
    <button type="button" onClick={(e) => { e.stopPropagation(); onClick(); }} title={`${c.ref} ${c.title}`}
      className={cn("block w-full cursor-pointer truncate rounded border-l-[3px] border px-1.5 py-0.5 text-left text-[11.5px] font-medium hover:brightness-95", tone[c.status])}>
      <span className="tabular-nums text-muted">{time}</span> {c.title}{!compact && c.customer_name ? <span className="text-muted"> · {c.customer_name}</span> : null}
    </button>
  );
}

function CalendarTab({ onOpen, onNew }: { onOpen: (id: string) => void; onNew?: (start: string) => void }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const [month, setMonth] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); });
  const [showTasks, setShowTasks] = useState(false);
  const gridStart = startOfWeek(month);
  const gridEnd = new Date(gridStart); gridEnd.setDate(gridEnd.getDate() + 42);
  const q = useQuery({
    queryKey: ["changes", "calendar", gridStart.toISOString(), customerId],
    queryFn: () => get<Calendar>(`api/changes/calendar?start=${encodeURIComponent(gridStart.toISOString())}&end=${encodeURIComponent(gridEnd.toISOString())}${customerId ? `&customer_id=${customerId}` : ""}`),
  });
  const days = Array.from({ length: 42 }, (_, i) => { const d = new Date(gridStart); d.setDate(d.getDate() + i); return d; });
  const today = new Date();
  const weekdays = Array.from({ length: 7 }, (_, i) => days[i].toLocaleDateString(loc(i18n.language), { weekday: "short" }));
  const cal = q.data;
  const byDay = (d: Date) => (cal?.changes ?? []).filter((c) => sameDay(new Date(c.starts_at), d));
  const tasksOn = (d: Date) => (cal?.tasks ?? []).filter((x) => sameDay(new Date(x.at), d));
  const upcoming = (cal?.changes ?? []).filter((c) => new Date(c.ends_at) >= today && c.status !== "cancelled");
  const newAt = (d: Date) => { const x = new Date(d); x.setHours(22, 0, 0, 0); onNew?.(x.toISOString()); };
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <Button size="sm" variant="ghost" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))} title={t("chg.prev")}><ChevronLeft /></Button>
          <Button size="sm" variant="secondary" onClick={() => { const d = new Date(); setMonth(new Date(d.getFullYear(), d.getMonth(), 1)); }}>{t("chg.today")}</Button>
          <Button size="sm" variant="ghost" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))} title={t("chg.next")}><ChevronRight /></Button>
          <span className="ml-2 text-lg font-bold capitalize">{month.toLocaleDateString(loc(i18n.language), { month: "long", year: "numeric" })}</span>
        </div>
        <Switch checked={showTasks} onCheckedChange={setShowTasks} label={t("chg.show_tasks")} />
      </div>
      {q.isError && <Alert tone="offline">{errorMessage(q.error, t)}</Alert>}
      <Card className="hidden overflow-hidden md:block">
        <div className="grid grid-cols-7 border-b border-line bg-sunken text-center text-[11px] font-bold uppercase tracking-wide text-muted">
          {weekdays.map((w) => <div key={w} className="py-2">{w}</div>)}
        </div>
        <div className="grid grid-cols-7">
          {days.map((d, i) => {
            const items = byDay(d);
            const tk = showTasks ? tasksOn(d) : [];
            const other = d.getMonth() !== month.getMonth();
            return (
              <div key={i} onDoubleClick={() => onNew && newAt(d)}
                className={cn("group min-h-[108px] border-b border-r border-line p-1.5 [&:nth-child(7n)]:border-r-0", other && "bg-sunken/50")}>
                <div className="mb-1 flex items-center justify-between">
                  <span className={cn("grid size-6 place-items-center rounded-full text-xs font-semibold", sameDay(d, today) ? "bg-koraal text-nacht" : other ? "text-muted/60" : "text-muted")}>{d.getDate()}</span>
                  {onNew && <button type="button" onClick={() => newAt(d)} title={t("chg.new")} className="invisible cursor-pointer rounded p-0.5 text-muted hover:bg-sunken hover:text-ink group-hover:visible"><Plus className="size-3.5" /></button>}
                </div>
                <div className="space-y-0.5">
                  {items.slice(0, 4).map((c) => <ChangeChip key={c.id} c={c} onClick={() => onOpen(c.id)} compact={!!customerId} />)}
                  {items.length > 4 && <div className="px-1 text-[11px] text-muted">+{items.length - 4}</div>}
                  {tk.slice(0, 3).map((x, j) => (
                    <div key={j} className="truncate px-1 text-[11px] text-muted" title={x.name}>
                      <CalendarClock className="mr-0.5 inline size-3" />{new Date(x.at).toLocaleTimeString(loc(i18n.language), { hour: "2-digit", minute: "2-digit" })} {x.name}
                    </div>
                  ))}
                  {tk.length > 3 && <div className="px-1 text-[11px] text-muted">+{tk.length - 3}</div>}
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      {/* Phones: an agenda instead of the month grid. */}
      <Card className="md:hidden">
        <CardHeader icon={<CalendarDays />} title={t("chg.agenda")} />
        {q.isLoading ? <CardBody><Spinner /></CardBody> : !(cal?.changes.length) ? <EmptyState icon={<CalendarRange />} title={t("chg.empty_month")} /> : (
          <ul className="divide-y divide-line">
            {cal.changes.map((c) => (
              <li key={c.id}><button type="button" onClick={() => onOpen(c.id)} className="w-full cursor-pointer px-4 py-3 text-left hover:bg-sunken">
                <div className="flex items-center gap-2"><span className="font-semibold">{c.title}</span><Badge tone={STATUS_TONE[c.status]} dot>{t(`chg.st.${c.status}`)}</Badge></div>
                <div className="text-xs text-muted">{whenText(c, i18n.language)}{c.customer_name ? ` · ${c.customer_name}` : ""}</div>
              </button></li>
            ))}
          </ul>
        )}
      </Card>
      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader icon={<ListChecks />} title={t("chg.upcoming")} description={t("chg.upcoming_desc")} />
          {!upcoming.length ? <EmptyState icon={<CalendarRange />} title={t("chg.no_upcoming")}>{onNew ? t("chg.no_upcoming_hint") : null}</EmptyState> : (
            <ul className="divide-y divide-line">
              {upcoming.slice(0, 8).map((c) => (
                <li key={c.id}><button type="button" onClick={() => onOpen(c.id)} className="flex w-full cursor-pointer items-center gap-3 px-5 py-3 text-left hover:bg-sunken">
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-semibold">{c.title}</div>
                    <div className="truncate text-xs text-muted">{c.ref} · {whenText(c, i18n.language)}{c.customer_name && !customerId ? ` · ${c.customer_name}` : ""}</div>
                  </div>
                  <Badge tone={STATUS_TONE[c.status]} dot>{t(`chg.st.${c.status}`)}</Badge>
                </button></li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <CardHeader icon={<Clock />} title={t("chg.windows")} description={t("chg.windows_desc", { tz: cal?.timezone ?? "" })} />
          {!cal?.windows.length ? <CardBody><Spinner /></CardBody> : (
            <Table>
              <tbody>
                {cal.windows.map((w) => (
                  <Tr key={w.customer_id}>
                    <Td className="font-medium">{w.customer_name}</Td>
                    <Td>{w.window ? <span className={cn(!w.understood && "text-warning")}>{w.window}{!w.understood && ` (${t("chg.window_unreadable")})`}</span> : <span className="text-muted">{t("chg.no_window")}</span>}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ list */
function ListTab({ onOpen }: { onOpen: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const [state, setState] = useState("upcoming");
  const q = useQuery({
    queryKey: ["changes", "list", state, customerId],
    queryFn: () => get<{ changes: Change[]; counts: Record<string, number> }>(`api/changes?${state !== "all" ? `state=${state}&` : ""}${customerId ? `customer_id=${customerId}` : ""}`),
  });
  const rows = state === "all" ? [...(q.data?.changes ?? [])].reverse() : q.data?.changes ?? [];
  return (
    <Card>
      <CardHeader icon={<ListChecks />} title={t("chg.list_title")}
        action={<Select value={state} onChange={(e) => setState(e.target.value)} className="w-auto">
          {["upcoming", "active", "all", "draft", "completed", "failed", "cancelled"].map((s) => <option key={s} value={s}>{t(`chg.filter.${s}`)}</option>)}
        </Select>} />
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !rows.length ? (
        <EmptyState icon={<CalendarRange />} title={t("chg.no_changes")} />
      ) : (
        <Table>
          <thead><Tr><Th>{t("chg.col.change")}</Th><Th>{t("chg.col.when")}</Th>{!customerId && <Th>{t("chg.col.customer")}</Th>}<Th>{t("chg.col.impact")}</Th><Th>{t("chg.col.status")}</Th><Th>{t("chg.col.notices")}</Th></Tr></thead>
          <tbody>
            {rows.map((c) => {
              const sent = (c.notices ?? []).filter((n) => n.ok).length;
              const failed = (c.notices ?? []).some((n) => n.ok === false && !n.skipped);
              return (
                <Tr key={c.id} className="cursor-pointer" onClick={() => onOpen(c.id)}>
                  <Td><div className="font-semibold">{c.title}</div><div className="text-xs text-muted">{c.ref} · {t(`chg.kind.${c.kind}`)}{c.machines.length ? ` · ${c.machines.map((m) => m.name).join(", ")}` : ""}</div></Td>
                  <Td className="whitespace-nowrap text-sm">{whenText(c, i18n.language)}</Td>
                  {!customerId && <Td className="text-sm">{c.customer_name}</Td>}
                  <Td><Badge tone={IMPACT_TONE[c.impact]}>{t(`chg.impact.${c.impact}`)}</Badge></Td>
                  <Td><Badge tone={STATUS_TONE[c.status]} dot>{t(`chg.st.${c.status}`)}</Badge></Td>
                  <Td className="text-sm">{c.notify === false ? <span className="text-muted">{t("chg.notify_off")}</span> : <span className={cn(failed && "text-offline")}><Mail className="mr-1 inline size-3.5" />{sent}</span>}</Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ detail */
function noticeLabel(n: Notice, t: (k: string, o?: Record<string, unknown>) => string, off: (m: number) => string) {
  if (n.key.startsWith("before:")) return t("chg.notice.before", { when: off(Number(n.key.slice(7))) });
  return t(`chg.notice.${n.key}`, { defaultValue: n.key });
}

function DetailDialog({ id, admin, onClose, onEdit }: { id: string; admin: boolean; onClose: () => void; onEdit: (c: Detail) => void }) {
  const { t, i18n } = useTranslation();
  const off = useOffsetLabel();
  const invalidate = useInvalidate();
  const [finish, setFinish] = useState<null | "completed" | "failed" | "cancelled">(null);
  const [note, setNote] = useState("");
  const q = useQuery({ queryKey: ["changes", "detail", id], queryFn: () => get<Detail>(`api/changes/${id}`) });
  const status = useMutation({
    mutationFn: (v: { status: ChangeStatus; note?: string }) => post<Detail>(`api/changes/${id}/status`, v),
    onSuccess: () => { toast.success(t("chg.saved")); setFinish(null); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const notify = useMutation({
    mutationFn: (kind: string) => post<Detail & { notice: Notice }>(`api/changes/${id}/notify`, { kind }),
    onSuccess: (d) => { d.notice.ok ? toast.success(t("chg.notice_sent", { count: d.notice.to ?? 0 })) : toast.error(t(`chg.notice_err.${d.notice.error ?? "send_failed"}`)); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/changes/${id}`),
    onSuccess: () => { toast.success(t("chg.deleted")); invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const c = q.data;
  const open = !!c && (c.status === "draft" || c.status === "planned" || c.status === "in_progress");
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide
      title={c ? <span className="flex flex-wrap items-center gap-2">{c.title}<Badge tone={STATUS_TONE[c.status]} dot>{t(`chg.st.${c.status}`)}</Badge></span> : t("chg.loading")}
      description={c ? `${c.ref} · ${c.customer_name ?? ""} · ${t(`chg.kind.${c.kind}`)}` : undefined}
      footer={c && admin ? (
        <>
          {open && <Button variant="ghost" onClick={() => onEdit(c)}><Pencil /> {t("common.edit")}</Button>}
          {c.status === "draft" && <Button variant="primary" loading={status.isPending} onClick={() => status.mutate({ status: "planned" })}><Check /> {t("chg.act.plan")}</Button>}
          {c.status === "planned" && <Button variant="secondary" onClick={() => notify.mutate("announce")} loading={notify.isPending}><Mail /> {t("chg.act.announce")}</Button>}
          {c.status === "planned" && <Button variant="secondary" loading={status.isPending} onClick={() => status.mutate({ status: "in_progress" })}><Play /> {t("chg.act.start")}</Button>}
          {(c.status === "planned" || c.status === "in_progress") && <Button variant="primary" onClick={() => { setNote(c.outcome_note ?? ""); setFinish("completed"); }}><Check /> {t("chg.act.complete")}</Button>}
          {open && c.status !== "draft" && <Button variant="ghost" onClick={() => { setNote(""); setFinish("cancelled"); }}><XCircle /> {t("chg.act.cancel")}</Button>}
          {c.status === "cancelled" && new Date(c.ends_at) > new Date() && <Button variant="secondary" loading={status.isPending} onClick={() => status.mutate({ status: "planned" })}>{t("chg.act.replan")}</Button>}
          {(c.status === "draft" || c.status === "cancelled" || !(c.notices ?? []).some((n) => n.ok)) && (
            <Button variant="ghost" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 /> {t("common.delete")}</Button>
          )}
        </>
      ) : undefined}>
      {q.isLoading || !c ? <Spinner /> : (
        <div className="space-y-5">
          <div className="grid gap-3 text-sm sm:grid-cols-2">
            <Info2 icon={<CalendarDays />} k={t("chg.f.when")} v={<>{whenText(c, i18n.language)} <span className="text-muted">({t("chg.n_minutes", { count: c.minutes })})</span></>} />
            <Info2 icon={<AlertTriangle />} k={t("chg.f.impact")} v={<Badge tone={IMPACT_TONE[c.impact]}>{t(`chg.impact.${c.impact}`)}</Badge>} />
            <Info2 icon={<Server />} k={t("chg.f.machines")} v={c.machines.length ? c.machines.map((m) => m.name).join(", ") : <span className="text-muted">{t("chg.no_machines")}</span>} />
            <Info2 icon={<Clock />} k={t("chg.f.window")} v={c.maintenance_window || <span className="text-muted">{t("chg.no_window")}</span>} />
            {(c.task_name || c.runbook_name) && <Info2 icon={<Link2 />} k={t("chg.f.links")} v={<>
              {c.task_name && <div>{t("chg.task")}: {c.task_name}</div>}
              {c.runbook_name && <div>{t("chg.runbook")}: {c.runbook_name}{c.run_runbook ? ` (${t("chg.runs_at_start")})` : ""}{c.runbook_run_status ? ` · ${t(`rb.status.${c.runbook_run_status}`, { defaultValue: c.runbook_run_status })}` : ""}</div>}
            </>} />}
          </div>
          {open && <div><h3 className="mb-2 text-sm font-bold">{t("chg.checks")}</h3><Conflicts items={c.conflicts} empty={t("chg.no_conflicts")} /></div>}
          {c.description && <Section title={t("chg.f.description")} hint={t("chg.f.description_hint")}><p className="whitespace-pre-wrap text-sm">{c.description}</p></Section>}
          {c.internal_notes && <Section title={t("chg.f.internal")}><p className="whitespace-pre-wrap text-sm text-muted">{c.internal_notes}</p></Section>}
          {c.outcome_note && <Section title={t("chg.f.outcome")}><p className="whitespace-pre-wrap text-sm">{c.outcome_note}</p></Section>}
          <Section title={t("chg.notices")} hint={c.notify === false ? t("chg.notify_off_hint") : t("chg.notices_hint", {
            when: c.effective.notify_before.map(off).join(", ") || "–", count: c.recipients.length,
          })}>
            {!(c.notices ?? []).length ? <p className="text-sm text-muted">{t("chg.no_notices")}</p> : (
              <ul className="space-y-1 text-sm">
                {(c.notices ?? []).map((n, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2">
                    {n.pending ? <Clock className="size-4 text-muted" /> : n.skipped ? <X className="size-4 text-muted" /> : n.ok ? <Check className="size-4 text-online" /> : <XCircle className="size-4 text-offline" />}
                    <span className="font-medium">{noticeLabel(n, t, off)}</span>
                    <span className="text-muted">{formatDate(n.at, i18n.language)}</span>
                    {n.ok && <span className="text-muted">· {t("chg.sent_to", { count: n.to ?? 0 })}</span>}
                    {n.skipped && <span className="text-muted">· {t("chg.skipped")}</span>}
                    {n.pending && <span className="text-muted">· {t("chg.pending")}</span>}
                    {n.ok === false && !n.skipped && n.error && <span className="text-offline">· {t(`chg.notice_err.${n.error}`, { defaultValue: n.error })}</span>}
                  </li>
                ))}
              </ul>
            )}
            {c.recipients.length > 0 && <p className="mt-2 text-xs text-muted">{t("chg.recipients")}: {c.recipients.map((r) => r.email).join(", ")}</p>}
            {c.notify !== false && !c.recipients.length && <Alert tone="warning" className="mt-2">{t("chg.no_recipients")}</Alert>}
          </Section>
          <p className="text-xs text-muted">{t("chg.created_by", { by: c.created_by ?? "?", at: formatDate(c.updated_at ?? null, i18n.language), upd: c.updated_by ?? "?" })}</p>
        </div>
      )}
      {finish && c && (
        <Dialog open onOpenChange={(v) => !v && setFinish(null)} title={t(`chg.finish.${finish}`)} description={t(`chg.finish.${finish}_desc`)}
          footer={<>
            <Button variant="ghost" onClick={() => setFinish(null)}>{t("common.cancel")}</Button>
            {finish === "completed" && <Button variant="danger" loading={status.isPending} onClick={() => status.mutate({ status: "failed", note })}>{t("chg.act.failed")}</Button>}
            <Button variant="primary" loading={status.isPending} onClick={() => status.mutate({ status: finish, note: finish === "cancelled" ? undefined : note })}>{t(`chg.finish.${finish}_button`)}</Button>
          </>}>
          {finish !== "cancelled" ? (
            <Field label={t("chg.f.outcome")} hint={t("chg.f.outcome_hint")}><Textarea rows={4} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
          ) : <p className="text-sm">{(c.notices ?? []).some((n) => n.ok) ? t("chg.cancel_mail") : t("chg.cancel_no_mail")}</p>}
        </Dialog>
      )}
    </Dialog>
  );
}

function Info2({ icon, k, v }: { icon: ReactNode; k: ReactNode; v: ReactNode }) {
  return (
    <div className="flex gap-2.5">
      <span className="mt-0.5 text-muted [&_svg]:size-4">{icon}</span>
      <div><div className="text-xs font-semibold uppercase tracking-wide text-muted">{k}</div><div>{v}</div></div>
    </div>
  );
}

function Section({ title, hint, children }: { title: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div>
      <h3 className="text-sm font-bold">{title}</h3>
      {hint && <p className="mb-2 text-xs text-muted">{hint}</p>}
      <div className={cn(!hint && "mt-1.5")}>{children}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ create and edit */
interface Form {
  customer_id: string; title: string; description: string; internal_notes: string; kind: Kind; impact: Impact; start: string; minutes: number;
  node_ids: string[]; task_id: string; runbook_id: string; run_runbook: boolean; notify: boolean; custom: boolean; notify_before: number[]; draft: boolean;
}

function OffsetPicker({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const off = useOffsetLabel();
  return (
    <div className="flex flex-wrap gap-1.5">
      {OFFSETS.map((m) => {
        const on = value.includes(m);
        return (
          <button key={m} type="button" onClick={() => onChange(on ? value.filter((x) => x !== m) : [...value, m].sort((a, b) => b - a))}
            className={cn("cursor-pointer rounded-full border px-3 py-1 text-[13px] font-semibold", on ? "border-nacht bg-nacht text-white dark:border-lucht dark:bg-lucht dark:text-nacht" : "border-line text-muted hover:text-ink")}>
            {off(m)}
          </button>
        );
      })}
    </div>
  );
}

function EditDialog({ init, onClose, onSaved }: { init: { change?: Detail; start?: string }; onClose: () => void; onSaved: (c: Detail) => void }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const c = init.change;
  const settings = useQuery({ queryKey: ["changes", "settings"], queryFn: () => get<Settings>("api/changes/settings") });
  const [f, setF] = useState<Form>(() => ({
    customer_id: c?.customer_id ?? customerId ?? "", title: c?.title ?? "", description: c?.description ?? "", internal_notes: c?.internal_notes ?? "",
    kind: c?.kind ?? "maintenance", impact: c?.impact ?? "minor", start: c ? toLocalInput(c.starts_at) : init.start ? toLocalInput(init.start) : "",
    minutes: c?.minutes ?? 120, node_ids: c?.node_ids ?? [], task_id: c?.task_id ?? "", runbook_id: c?.runbook_id ?? "", run_runbook: c?.run_runbook ?? false,
    notify: c?.notify ?? true, custom: c?.notify_before != null, notify_before: c?.notify_before ?? [4320, 60], draft: false,
  }));
  useEffect(() => { if (!c && settings.data && !init.start) setF((x) => ({ ...x, minutes: settings.data.default_minutes })); }, [settings.data, c, init.start]);
  const set = (p: Partial<Form>) => setF((x) => ({ ...x, ...p }));
  const machines = useQuery({ queryKey: ["ticket-machines", f.customer_id], queryFn: () => get<{ id: string; name: string; kind: string }[]>(`api/tickets/machines?customer_id=${f.customer_id}`), enabled: !!f.customer_id });
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: () => get<{ tasks: { id: string; name: string; enabled: boolean }[] }>("api/tasks") });
  const runbooks = useQuery({ queryKey: ["runbooks", f.customer_id], queryFn: () => get<{ id: string; name: string; customer_id: string | null }[]>(`api/runbooks?customer_id=${f.customer_id}`), enabled: !!f.customer_id });
  const startIso = f.start ? new Date(f.start).toISOString() : null;
  const checkKey = useMemo(() => JSON.stringify([f.customer_id, startIso, f.minutes, f.node_ids, f.task_id, f.custom ? f.notify_before : null]), [f.customer_id, startIso, f.minutes, f.node_ids, f.task_id, f.custom, f.notify_before]);
  const check = useQuery({
    queryKey: ["changes", "check", checkKey, c?.id],
    queryFn: () => post<Check>("api/changes/check", {
      customer_id: f.customer_id, starts_at: startIso, minutes: f.minutes, node_ids: f.node_ids, task_id: f.task_id || null,
      notify_before: f.custom ? f.notify_before : null, exclude_id: c?.id ?? null,
    }),
    enabled: !!f.customer_id,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        title: f.title, description: f.description || null, internal_notes: f.internal_notes || null, kind: f.kind, impact: f.impact,
        starts_at: startIso, ends_at: new Date(new Date(f.start).getTime() + f.minutes * 60000).toISOString(), node_ids: f.node_ids,
        task_id: f.task_id || null, runbook_id: f.runbook_id || null, run_runbook: f.run_runbook && !!f.runbook_id, notify: f.notify,
        notify_before: f.custom ? f.notify_before : null,
      };
      return c ? put<Detail>(`api/changes/${c.id}`, body) : post<Detail>("api/changes", { ...body, customer_id: f.customer_id, status: f.draft ? "draft" : "planned" });
    },
    onSuccess: (d) => { toast.success(t("chg.saved")); invalidate(); onSaved(d); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const sug = check.data?.suggestion;
  const valid = !!f.customer_id && !!f.title.trim() && !!f.start && f.minutes >= 5;
  const durations = DURATIONS.includes(f.minutes) ? DURATIONS : [...DURATIONS, f.minutes].sort((a, b) => a - b);
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} xl title={c ? t("chg.edit_title", { ref: c.ref }) : t("chg.new_title")} description={t("chg.edit_desc")}
      footer={<>
        <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={!valid} loading={save.isPending} onClick={() => save.mutate()}>{c ? t("common.save") : f.draft ? t("chg.save_draft") : t("chg.save_plan")}</Button>
      </>}>
      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <div className="space-y-4">
          {!c && (
            <Field label={t("chg.f.customer")}>
              <Select value={f.customer_id} onChange={(e) => set({ customer_id: e.target.value, node_ids: [], runbook_id: "", run_runbook: false })}>
                <option value="">{t("chg.choose_customer")}</option>
                {(customers.data ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </Select>
            </Field>
          )}
          <Field label={t("chg.f.title")}><Input value={f.title} maxLength={200} onChange={(e) => set({ title: e.target.value })} placeholder={t("chg.f.title_ph")} /></Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("chg.f.kind")}><Select value={f.kind} onChange={(e) => set({ kind: e.target.value as Kind })}>{KINDS.map((k) => <option key={k} value={k}>{t(`chg.kind.${k}`)}</option>)}</Select></Field>
            <Field label={t("chg.f.impact")}><Select value={f.impact} onChange={(e) => set({ impact: e.target.value as Impact })}>{IMPACTS.map((k) => <option key={k} value={k}>{t(`chg.impact.${k}`)}</option>)}</Select></Field>
            <Field label={t("chg.f.start")}><Input type="datetime-local" value={f.start} onChange={(e) => set({ start: e.target.value })} /></Field>
            <Field label={t("chg.f.duration")}>
              <Select value={f.minutes} onChange={(e) => set({ minutes: Number(e.target.value) })}>
                {durations.map((m) => <option key={m} value={m}>{m < 60 ? t("chg.minutes", { count: m }) : t("chg.hours", { count: m / 60 })}</option>)}
              </Select>
            </Field>
          </div>
          <Field label={t("chg.f.description")} hint={t("chg.f.description_hint")}><Textarea rows={3} value={f.description} onChange={(e) => set({ description: e.target.value })} placeholder={t("chg.f.description_ph")} /></Field>
          <Field label={t("chg.f.internal")} hint={t("chg.f.internal_hint")}><Textarea rows={2} value={f.internal_notes} onChange={(e) => set({ internal_notes: e.target.value })} /></Field>
          {!!f.customer_id && (
            <Field label={t("chg.f.machines")}>
              {!machines.data?.length ? <p className="text-sm text-muted">{t("chg.no_customer_machines")}</p> : (
                <div className="grid max-h-40 gap-1.5 overflow-y-auto rounded-md border border-line p-2 sm:grid-cols-2">
                  {machines.data.map((m) => (
                    <Checkbox key={m.id} checked={f.node_ids.includes(m.id)} label={m.name}
                      onCheckedChange={(v) => set({ node_ids: v ? [...f.node_ids, m.id] : f.node_ids.filter((x) => x !== m.id) })} />
                  ))}
                </div>
              )}
            </Field>
          )}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("chg.f.task")} hint={t("chg.f.task_hint")}>
              <Select value={f.task_id} onChange={(e) => set({ task_id: e.target.value })}>
                <option value="">{t("chg.none")}</option>
                {(tasks.data?.tasks ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}{x.enabled ? "" : ` (${t("chg.disabled")})`}</option>)}
              </Select>
            </Field>
            <Field label={t("chg.f.runbook")}>
              <Select value={f.runbook_id} onChange={(e) => set({ runbook_id: e.target.value, run_runbook: e.target.value ? f.run_runbook : false })} disabled={!f.customer_id}>
                <option value="">{t("chg.none")}</option>
                {(runbooks.data ?? []).filter((x) => !x.customer_id || x.customer_id === f.customer_id).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </Select>
            </Field>
          </div>
          {f.runbook_id && <Switch checked={f.run_runbook} onCheckedChange={(v) => set({ run_runbook: v })} label={t("chg.f.run_runbook")} description={t("chg.f.run_runbook_hint")} />}
        </div>
        <div className="space-y-4">
          <div className="rounded-lg border border-line bg-sunken/60 p-4">
            <h3 className="mb-1 flex items-center gap-2 text-sm font-bold"><Sparkles className="size-4 text-koraal" />{t("chg.checks")}</h3>
            <p className="mb-3 text-xs text-muted">{check.data?.maintenance_window ? t("chg.window_is", { window: check.data.maintenance_window }) : f.customer_id ? t("chg.no_window") : t("chg.choose_customer_first")}</p>
            {check.isFetching && !check.data ? <Spinner /> : !f.start ? null : <Conflicts items={check.data?.conflicts ?? []} empty={t("chg.no_conflicts")} />}
            {sug && (
              <div className="mt-3 rounded-md border border-line bg-raised p-3 text-sm">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("chg.suggestion")}</div>
                <div className="my-1 font-semibold">{whenText(sug, i18n.language)}</div>
                <Button size="sm" variant="secondary" onClick={() => set({ start: toLocalInput(sug.starts_at) })}>{t("chg.use_suggestion")}</Button>
              </div>
            )}
          </div>
          <div className="space-y-3 rounded-lg border border-line p-4">
            <h3 className="flex items-center gap-2 text-sm font-bold"><BellRing className="size-4" />{t("chg.notices")}</h3>
            <Switch checked={f.notify} onCheckedChange={(v) => set({ notify: v })} label={t("chg.f.notify")} description={t("chg.f.notify_hint")} />
            {f.notify && (
              <>
                <Checkbox checked={f.custom} onCheckedChange={(v) => set({ custom: v })} label={t("chg.f.custom_times")} />
                {f.custom && <OffsetPicker value={f.notify_before} onChange={(v) => set({ notify_before: v })} />}
              </>
            )}
            {!c && <Switch checked={f.draft} onCheckedChange={(v) => set({ draft: v })} label={t("chg.f.draft")} description={t("chg.f.draft_hint")} />}
          </div>
        </div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ settings */
function CopyLink({ url }: { url: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2">
      <Input readOnly value={url} className="font-mono text-xs" onFocus={(e) => e.target.select()} />
      <Button size="sm" variant="secondary" onClick={() => { navigator.clipboard?.writeText(url); toast.success(t("chg.copied")); }}><Copy /></Button>
    </div>
  );
}

function SettingsTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["changes", "settings"], queryFn: () => get<Settings>("api/changes/settings") });
  const [f, setF] = useState<Settings | null>(null);
  useEffect(() => { if (q.data) setF(q.data); }, [q.data]);
  const save = useMutation({
    mutationFn: (v: Settings) => put<Settings>("api/changes/settings", v),
    onSuccess: (d) => { qc.setQueryData(["changes", "settings"], d); toast.success(t("chg.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const feed = useMutation({
    mutationFn: (enabled: boolean) => post<Settings>("api/changes/settings/ical", { enabled }),
    onSuccess: (d) => qc.setQueryData(["changes", "settings"], d),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!f) return <Spinner />;
  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <Card>
        <CardHeader icon={<Settings2 />} title={t("chg.set.title")} description={t("chg.set.desc")} />
        <CardBody className="space-y-4">
          <Field label={t("chg.set.before")} hint={t("chg.set.before_hint")}><OffsetPicker value={f.notify_before} onChange={(v) => setF({ ...f, notify_before: v })} /></Field>
          <Field label={t("chg.set.recipients")}>
            <Select value={f.recipients} onChange={(e) => setF({ ...f, recipients: e.target.value as Recipients })}>{RECIPIENTS.map((r) => <option key={r} value={r}>{t(`chg.rcpt.${r}`)}</option>)}</Select>
          </Field>
          <Switch checked={f.notify_after} onCheckedChange={(v) => setF({ ...f, notify_after: v })} label={t("chg.set.after")} description={t("chg.set.after_hint")} />
          <Switch checked={f.notify_cancel} onCheckedChange={(v) => setF({ ...f, notify_cancel: v })} label={t("chg.set.cancel")} />
          <Switch checked={f.auto_status} onCheckedChange={(v) => setF({ ...f, auto_status: v })} label={t("chg.set.auto")} description={t("chg.set.auto_hint")} />
          <Field label={t("chg.set.default_minutes")}><Input type="number" min={5} max={20160} value={f.default_minutes} onChange={(e) => setF({ ...f, default_minutes: Number(e.target.value) })} className="w-32" /></Field>
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(f)}>{t("common.save")}</Button>
        </CardBody>
      </Card>
      <div className="space-y-5">
        <Card>
          <CardHeader icon={<CalendarDays />} title={t("chg.set.feed")} description={t("chg.set.feed_desc")} />
          <CardBody className="space-y-3">
            {q.data?.ical_url ? <CopyLink url={q.data.ical_url} /> : <p className="text-sm text-muted">{t("chg.set.feed_off")}</p>}
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" loading={feed.isPending} onClick={() => feed.mutate(true)}>{q.data?.ical_url ? t("chg.set.feed_new") : t("chg.set.feed_on")}</Button>
              {q.data?.ical_url && <Button variant="ghost" onClick={() => feed.mutate(false)}>{t("chg.set.feed_disable")}</Button>}
            </div>
          </CardBody>
        </Card>
        <CustomerSettingsCard defaults={f} />
      </div>
    </div>
  );
}

function CustomerSettingsCard({ defaults }: { defaults: Settings }) {
  const { t } = useTranslation();
  const off = useOffsetLabel();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const qc = useQueryClient();
  const [cid, setCid] = useState(customerId);
  useEffect(() => { if (!cid && customers.data?.length) setCid(customers.data[0].id); }, [cid, customers.data]);
  const q = useQuery({ queryKey: ["changes", "customer-settings", cid], queryFn: () => get<CustomerSettings>(`api/changes/customers/${cid}/settings`), enabled: !!cid });
  const [f, setF] = useState<CustomerSettings | null>(null);
  const [extra, setExtra] = useState("");
  useEffect(() => { if (q.data) { setF(q.data); setExtra(q.data.extra.join(", ")); } }, [q.data]);
  const onDone = (d: CustomerSettings) => { qc.setQueryData(["changes", "customer-settings", cid], d); };
  const save = useMutation({
    mutationFn: (v: CustomerSettings) => put<CustomerSettings>(`api/changes/customers/${cid}/settings`, {
      notify_before: v.notify_before, notify_after: v.notify_after, recipients: v.recipients, lang: v.lang,
      extra: extra.split(/[,;\s]+/).filter(Boolean),
    }),
    onSuccess: (d) => { onDone(d); toast.success(t("chg.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const feed = useMutation({
    mutationFn: (enabled: boolean) => post<CustomerSettings>(`api/changes/customers/${cid}/ical`, { enabled }),
    onSuccess: onDone, onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Card>
      <CardHeader icon={<Mail />} title={t("chg.cset.title")} description={t("chg.cset.desc")}
        action={<Select value={cid} onChange={(e) => setCid(e.target.value)} className="w-auto">{(customers.data ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</Select>} />
      {!f ? <CardBody><Spinner /></CardBody> : (
        <CardBody className="space-y-4">
          <p className="text-sm">{f.maintenance_window ? t("chg.window_is", { window: f.maintenance_window }) : <span className="text-warning">{t("chg.cset.no_window")}</span>}</p>
          <Field label={t("chg.set.before")}>
            <Checkbox checked={f.notify_before != null} onCheckedChange={(v) => setF({ ...f, notify_before: v ? [...defaults.notify_before] : null })} label={t("chg.cset.own_times")} />
            <div className="mt-2">{f.notify_before != null ? <OffsetPicker value={f.notify_before} onChange={(v) => setF({ ...f, notify_before: v })} /> : <p className="text-xs text-muted">{t("chg.cset.default", { v: defaults.notify_before.map(off).join(", ") })}</p>}</div>
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("chg.set.recipients")}>
              <Select value={f.recipients ?? ""} onChange={(e) => setF({ ...f, recipients: (e.target.value || null) as Recipients | null })}>
                <option value="">{t("chg.cset.use_default", { v: t(`chg.rcpt.${defaults.recipients}`) })}</option>
                {RECIPIENTS.map((r) => <option key={r} value={r}>{t(`chg.rcpt.${r}`)}</option>)}
              </Select>
            </Field>
            <Field label={t("chg.cset.lang")}>
              <Select value={f.lang ?? ""} onChange={(e) => setF({ ...f, lang: (e.target.value || null) as "en" | "nl" | null })}>
                <option value="">{t("chg.cset.lang_default")}</option><option value="nl">Nederlands</option><option value="en">English</option>
              </Select>
            </Field>
          </div>
          <Field label={t("chg.set.after")}>
            <Select value={f.notify_after == null ? "" : f.notify_after ? "yes" : "no"} onChange={(e) => setF({ ...f, notify_after: e.target.value === "" ? null : e.target.value === "yes" })}>
              <option value="">{t("chg.cset.use_default", { v: defaults.notify_after ? t("chg.yes") : t("chg.no") })}</option><option value="yes">{t("chg.yes")}</option><option value="no">{t("chg.no")}</option>
            </Select>
          </Field>
          <Field label={t("chg.cset.extra")} hint={t("chg.cset.extra_hint")}><Input value={extra} onChange={(e) => setExtra(e.target.value)} placeholder="it@klant.nl, servicedesk@…" /></Field>
          <div className="rounded-md bg-sunken p-3 text-xs">
            <div className="mb-1 font-semibold">{t("chg.recipients")} ({f.recipient_list.length})</div>
            {f.recipient_list.length ? f.recipient_list.map((r) => <div key={r.email}>{r.email} <span className="text-muted">· {t(`chg.src.${r.source}`)} · {r.lang.toUpperCase()}</span></div>) : <div className="text-warning">{t("chg.no_recipients")}</div>}
          </div>
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(f)}>{t("common.save")}</Button>
          <div className="border-t border-line pt-4">
            <div className="mb-1 text-sm font-semibold">{t("chg.cset.feed")}</div>
            <p className="mb-2 text-xs text-muted">{t("chg.cset.feed_desc")}</p>
            {f.ical_url && <div className="mb-2"><CopyLink url={f.ical_url} /></div>}
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" loading={feed.isPending} onClick={() => feed.mutate(true)}>{f.ical_url ? t("chg.set.feed_new") : t("chg.set.feed_on")}</Button>
              {f.ical_url && <Button size="sm" variant="ghost" onClick={() => feed.mutate(false)}>{t("chg.set.feed_disable")}</Button>}
            </div>
          </div>
        </CardBody>
      )}
    </Card>
  );
}
