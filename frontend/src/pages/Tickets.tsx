import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Bot, CheckCircle2, Clock, Inbox, LifeBuoy, Lock, Mail, MessageSquare, Pencil, Play, Plus, Search, Send,
  Server, Laptop, Trash2, UserRound, Users, XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { appUrl, cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
export type TicketStatus = "new" | "open" | "waiting_customer" | "on_hold" | "resolved" | "closed";
export type Priority = "low" | "normal" | "high" | "urgent";
type Verdict = "issues_found" | "no_issues" | "unreachable";
interface Machine { id: string; name: string | null }
export interface Ticket {
  id: string; number: number; ref: string; subject: string; category: "problem" | "question" | "request"; priority: Priority;
  status: TicketStatus; customer_id: string; customer_name: string | null; machines: Machine[]; reporter_name: string | null;
  reporter_email: string | null; created_at: string; updated_at: string; resolved_at: string | null; closed_at: string | null;
  last_customer_at: string | null; last_staff_at: string | null; messages: number; reporter_id?: string | null;
  created_by?: string | null; assignee_id?: string | null; assignee_name?: string | null; first_response_at?: string | null;
  diagnosis_status?: string | null; diagnosis_verdict?: Verdict | null; needs_reply?: boolean;
}
interface Counts { active: number; new: number; needs_reply: number; waiting_customer?: number; on_hold?: number; open?: number }
interface Message {
  id: string; author_kind: "customer" | "staff" | "ai" | "system"; author_name: string | null; body: string; created_at: string;
  internal?: boolean; event: { kind?: string; from?: string; to?: string; note?: string; verdict?: Verdict };
}
interface DiagMachine {
  node_id: string; name: string; status: "ok" | "issues" | "offline" | "error"; online: boolean; findings: string[];
  failed_services: string[]; recent_errors: string[]; top_cpu?: string[]; error?: string; role?: string;
}
interface Diagnosis {
  status?: "queued" | "running" | "done" | "failed"; started_at?: string; finished_at?: string; verdict?: Verdict; summary?: string[];
  next_steps?: string[]; machines?: DiagMachine[]; scanned_all?: boolean;
  scan?: { node_id: string; name: string; failed_services: string[]; findings: string[]; error_count: number; error?: string }[];
}
interface PortalMachine { id: string; name: string; type: "server" | "workstation"; online: boolean; kind: string }
export interface PortalUser {
  id: string; customer_id: string; customer_name: string | null; email: string; full_name: string | null; phone: string | null;
  role: "member" | "manager"; language: "en" | "nl"; is_active: boolean; notify_all: boolean; activated: boolean;
  created_at: string; invited_at: string | null; last_login_at: string | null;
}
interface Detail extends Ticket {
  thread: Message[]; diagnosis: Diagnosis; reporter: PortalUser | null; customer_machines: PortalMachine[];
  customer: { name?: string; email?: string | null; phone?: string | null; maintenance_window?: string | null; sla?: string | null };
}
interface Staff { id: string; name: string; role: string }
interface PortalSettings { enabled: boolean; staff_to: string; auto_diagnose: boolean; auto_close_days: number; welcome: string }

export const STATUS_TONE: Record<TicketStatus, "online" | "offline" | "warning" | "neutral" | "info" | "accent"> = {
  new: "accent", open: "info", waiting_customer: "warning", on_hold: "neutral", resolved: "online", closed: "neutral",
};
export const PRIORITY_TONE: Record<Priority, "online" | "offline" | "warning" | "neutral" | "info"> = {
  low: "neutral", normal: "info", high: "warning", urgent: "offline",
};
const STATUSES: TicketStatus[] = ["new", "open", "waiting_customer", "on_hold", "resolved", "closed"];
const PRIORITIES: Priority[] = ["low", "normal", "high", "urgent"];
const CATEGORIES = ["problem", "question", "request"] as const;

function qs(path: string, params: Record<string, string | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) u.set(k, v);
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => { for (const k of ["tickets", "ticket", "ticket-counts", "portal-users"]) qc.invalidateQueries({ queryKey: [k] }); };
}

export function StatusBadge({ s, portal }: { s: TicketStatus; portal?: boolean }) {
  const { t } = useTranslation();
  return <Badge tone={STATUS_TONE[s]} dot>{t(portal ? `portal.status.${s}` : `tk.status.${s}`)}</Badge>;
}

export function PriorityBadge({ p }: { p: Priority }) {
  const { t } = useTranslation();
  return <Badge tone={PRIORITY_TONE[p]}>{t(`tk.priority.${p}`)}</Badge>;
}

function VerdictIcon({ tk }: { tk: Ticket }) {
  const { t } = useTranslation();
  const s = tk.diagnosis_status;
  if (!s) return <span className="text-muted">–</span>;
  if (s === "running" || s === "queued") return <Spinner className="size-4" />;
  if (s === "failed") return <span title={t("tk.diag.failed")}><XCircle className="size-4 text-offline" /></span>;
  const v = tk.diagnosis_verdict;
  return v === "issues_found" ? <span title={t("tk.diag.v.issues_found")}><AlertTriangle className="size-4 text-warning" /></span>
    : v === "unreachable" ? <span title={t("tk.diag.v.unreachable")}><XCircle className="size-4 text-muted" /></span>
    : <span title={t("tk.diag.v.no_issues")}><CheckCircle2 className="size-4 text-online" /></span>;
}

/* ------------------------------------------------------------------ list page */
export function TicketsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [tab, setTab] = useState("tickets");
  const [creating, setCreating] = useState(false);
  const counts = useQuery({ queryKey: ["ticket-counts"], queryFn: () => get<Counts>("api/tickets/counts") });
  const c = counts.data;
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><LifeBuoy className="hidden size-6 shrink-0 text-koraal sm:block" /><span>{t("tk.title")}</span></span>}
        description={t("tk.desc")}
        actions={admin && <Button variant="primary" onClick={() => setCreating(true)}><Plus /> {t("tk.new")}</Button>} />
      <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("tk.k_reply")} value={c?.needs_reply ?? "–"} hint={t("tk.k_reply_hint")} tone={c?.needs_reply ? "warning" : "neutral"} icon={<MessageSquare />} />
        <Stat label={t("tk.k_new")} value={c?.new ?? "–"} hint={t("tk.k_new_hint")} tone={c?.new ? "accent" : "neutral"} icon={<Inbox />} />
        <Stat label={t("tk.k_active")} value={c?.active ?? "–"} hint={t("tk.k_active_hint")} tone="info" icon={<LifeBuoy />} />
        <Stat label={t("tk.k_waiting")} value={c?.waiting_customer ?? 0} hint={t("tk.k_waiting_hint")} tone="neutral" icon={<Clock />} />
      </div>
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="tickets" className={tabCls}>{t("tk.tab_tickets")}</Tabs.Trigger>
          <Tabs.Trigger value="accounts" className={tabCls}>{t("tk.tab_accounts")}</Tabs.Trigger>
          {admin && <Tabs.Trigger value="settings" className={tabCls}>{t("tk.tab_settings")}</Tabs.Trigger>}
        </Tabs.List>
        <Tabs.Content value="tickets"><TicketList /></Tabs.Content>
        <Tabs.Content value="accounts"><PortalAccounts admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="settings"><PortalSettingsCard /></Tabs.Content>}
      </Tabs.Root>
      {creating && <NewTicketDialog onClose={() => setCreating(false)} />}
    </div>
  );
}

function TicketList() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { customerId } = useCustomer();
  const [status, setStatus] = useState<"active" | "waiting_customer" | "resolved" | "all">("active");
  const [assignee, setAssignee] = useState("");
  const [q, setQ] = useState("");
  const list = useQuery({
    queryKey: ["tickets", customerId, status, assignee, q],
    queryFn: () => get<{ tickets: Ticket[] }>(qs("api/tickets", { status: status === "all" ? "" : status, assignee, q: q.trim() })),
    refetchInterval: 60_000,
  });
  const rows = list.data?.tickets ?? [];
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
        <Segmented value={status} onChange={setStatus} options={[
          { value: "active", label: t("tk.f_active") }, { value: "waiting_customer", label: t("tk.f_waiting") },
          { value: "resolved", label: t("tk.f_resolved") }, { value: "all", label: t("tk.f_all") },
        ]} />
        <Select value={assignee} onChange={(e) => setAssignee(e.target.value)} className="h-9 w-auto min-w-40">
          <option value="">{t("tk.f_anyone")}</option>
          <option value="me">{t("tk.f_me")}</option>
          <option value="none">{t("tk.f_unassigned")}</option>
        </Select>
        <div className="relative ml-auto w-full sm:w-64">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("tk.search")} className="h-9 pl-9" />
        </div>
      </div>
      {list.isLoading ? <div className="p-6"><Spinner /></div> : list.isError ? <div className="p-5"><Alert tone="offline">{errorMessage(list.error, t)}</Alert></div>
        : !rows.length ? <EmptyState icon={<Inbox />} title={t("tk.empty")}>{t("tk.empty_hint")}</EmptyState> : (
          <Table>
            <thead><tr>
              <Th className="w-20">#</Th><Th>{t("tk.subject")}</Th><Th>{t("tk.status_label")}</Th><Th className="hidden md:table-cell">{t("tk.priority_label")}</Th>
              <Th className="hidden lg:table-cell">{t("tk.assignee")}</Th><Th className="hidden sm:table-cell" title={t("tk.diag.title")}><Bot className="size-4" /></Th><Th>{t("tk.updated")}</Th>
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <Tr key={r.id} className="cursor-pointer" onClick={() => navigate(`/tickets/${r.id}`)}>
                  <Td className="mono text-muted">{r.ref}</Td>
                  <Td>
                    <div className="flex items-center gap-2 font-semibold">
                      {r.needs_reply && <span className="size-2 shrink-0 rounded-full bg-koraal" title={t("tk.needs_reply")} />}
                      <Link to={`/tickets/${r.id}`} className="hover:underline" onClick={(e) => e.stopPropagation()}>{r.subject}</Link>
                    </div>
                    <div className="text-xs text-muted">{r.customer_name} · {r.reporter_name || r.created_by || "–"}{r.machines.length ? ` · ${r.machines.map((m) => m.name).join(", ")}` : ""}</div>
                  </Td>
                  <Td><StatusBadge s={r.status} /></Td>
                  <Td className="hidden md:table-cell"><PriorityBadge p={r.priority} /></Td>
                  <Td className="hidden text-sm lg:table-cell">{r.assignee_name || <span className="text-muted">{t("tk.unassigned")}</span>}</Td>
                  <Td className="hidden sm:table-cell"><VerdictIcon tk={r} /></Td>
                  <Td className="whitespace-nowrap text-sm text-muted" title={formatDate(r.updated_at, i18n.language)}>{timeAgo(r.updated_at, i18n.language)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
    </Card>
  );
}

/* ------------------------------------------------------------------ new ticket (staff) */
function MachinePicker({ machines, value, onChange }: { machines: PortalMachine[]; value: string[]; onChange: (v: string[]) => void }) {
  const { t } = useTranslation();
  if (!machines.length) return <p className="text-sm text-muted">{t("tk.no_machines")}</p>;
  return (
    <div className="grid max-h-56 gap-2 overflow-y-auto rounded-md border border-line p-3 sm:grid-cols-2">
      {machines.map((m) => (
        <Checkbox key={m.id} checked={value.includes(m.id)} onCheckedChange={(v) => onChange(v ? [...value, m.id] : value.filter((x) => x !== m.id))}
          label={<span className="flex items-center gap-1.5">{m.type === "workstation" ? <Laptop className="size-3.5" /> : <Server className="size-3.5" />}{m.name}</span>}
          description={m.online ? t("tk.online") : t("tk.offline")} />
      ))}
    </div>
  );
}

function NewTicketDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const customers = useCustomers();
  const { customerId } = useCustomer();
  const [f, setF] = useState({ customer_id: customerId || "", subject: "", body: "", category: "problem", priority: "normal",
    node_ids: [] as string[], reporter_id: "", reporter_name: "", reporter_email: "", assignee_id: "", diagnose: true });
  const machines = useQuery({ queryKey: ["ticket-machines", f.customer_id], queryFn: () => get<PortalMachine[]>(`api/tickets/machines?customer_id=${f.customer_id}`), enabled: !!f.customer_id });
  const accounts = useQuery({ queryKey: ["portal-users", f.customer_id], queryFn: () => get<PortalUser[]>(`api/tickets/portal-users?customer_id=${f.customer_id}`), enabled: !!f.customer_id });
  const staff = useQuery({ queryKey: ["ticket-staff"], queryFn: () => get<Staff[]>("api/tickets/staff") });
  const save = useMutation({
    mutationFn: () => post<Ticket>("api/tickets", { ...f, reporter_id: f.reporter_id || null, reporter_email: f.reporter_email || null,
      reporter_name: f.reporter_name || null, assignee_id: f.assignee_id || null }),
    onSuccess: (tk) => { invalidate(); toast.success(t("tk.created", { ref: tk.ref })); onClose(); navigate(`/tickets/${tk.id}`); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("tk.new")} description={t("tk.new_desc")} wide
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!f.customer_id || !f.subject.trim() || !f.body.trim()} onClick={() => save.mutate()}>{t("tk.create")}</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("tk.customer")}>
          <Select value={f.customer_id} onChange={(e) => setF({ ...f, customer_id: e.target.value, node_ids: [], reporter_id: "" })}>
            <option value="">–</option>
            {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </Field>
        <Field label={t("tk.reporter")} hint={t("tk.reporter_hint")}>
          <Select value={f.reporter_id} onChange={(e) => setF({ ...f, reporter_id: e.target.value })} disabled={!f.customer_id}>
            <option value="">{t("tk.reporter_other")}</option>
            {(accounts.data ?? []).filter((a) => a.is_active).map((a) => <option key={a.id} value={a.id}>{a.full_name || a.email}</option>)}
          </Select>
        </Field>
        {!f.reporter_id && <>
          <Field label={t("tk.reporter_name")}><Input value={f.reporter_name} onChange={(e) => setF({ ...f, reporter_name: e.target.value })} /></Field>
          <Field label={t("tk.reporter_email")}><Input type="email" value={f.reporter_email} onChange={(e) => setF({ ...f, reporter_email: e.target.value })} /></Field>
        </>}
        <Field label={t("tk.subject")} className="sm:col-span-2"><Input value={f.subject} maxLength={200} onChange={(e) => setF({ ...f, subject: e.target.value })} /></Field>
        <Field label={t("tk.description")} className="sm:col-span-2"><Textarea className="font-sans text-sm" rows={5} value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} /></Field>
        <Field label={t("tk.category_label")}>
          <Select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
            {CATEGORIES.map((c) => <option key={c} value={c}>{t(`tk.category.${c}`)}</option>)}
          </Select>
        </Field>
        <Field label={t("tk.priority_label")}>
          <Select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })}>
            {PRIORITIES.map((p) => <option key={p} value={p}>{t(`tk.priority.${p}`)}</option>)}
          </Select>
        </Field>
        <Field label={t("tk.assignee")}>
          <Select value={f.assignee_id} onChange={(e) => setF({ ...f, assignee_id: e.target.value })}>
            <option value="">{t("tk.unassigned")}</option>
            {(staff.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </Field>
        <div className="flex items-end pb-2"><Switch checked={f.diagnose} onCheckedChange={(v) => setF({ ...f, diagnose: v })} label={t("tk.run_diag")} /></div>
        {f.customer_id && <Field label={t("tk.machines")} className="sm:col-span-2"><MachinePicker machines={machines.data ?? []} value={f.node_ids} onChange={(v) => setF({ ...f, node_ids: v })} /></Field>}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ detail page */
export function TicketDetailPage() {
  const { id = "" } = useParams();
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const q = useQuery({ queryKey: ["ticket", id], queryFn: () => get<Detail>(`api/tickets/${id}`), refetchInterval: (qq) => {
    const s = qq.state.data?.diagnosis?.status;
    return s === "running" || s === "queued" ? 3000 : 60_000;
  } });
  const staff = useQuery({ queryKey: ["ticket-staff"], queryFn: () => get<Staff[]>("api/tickets/staff"), enabled: admin });
  const upd = useMutation({
    mutationFn: (body: Record<string, unknown>) => patch<Detail>(`api/tickets/${id}`, body),
    onSuccess: () => { invalidate(); toast.success(t("tk.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/tickets/${id}`),
    onSuccess: () => { invalidate(); navigate("/tickets"); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const [editMachines, setEditMachines] = useState<string[] | null>(null);
  const [confirmDel, setConfirmDel] = useState(false);
  if (q.isLoading) return <Spinner />;
  if (q.isError || !q.data) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const tk = q.data;
  return (
    <div>
      <div className="mb-2 text-sm"><Link to="/tickets" className="font-semibold text-accent-text hover:underline">← {t("tk.title")}</Link></div>
      <PageHeader
        title={<span className="break-words"><span className="mono mr-2 text-muted">{tk.ref}</span>{tk.subject}</span>}
        description={<span className="flex flex-wrap items-center gap-2">
          <StatusBadge s={tk.status} /><PriorityBadge p={tk.priority} /><Badge>{t(`tk.category.${tk.category}`)}</Badge>
          <Link to={`/customers/${tk.customer_id}`} className="font-semibold text-ink hover:underline">{tk.customer_name}</Link>
          <span>· {t("tk.opened", { when: timeAgo(tk.created_at, i18n.language) })}</span>
        </span>} />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-5">
          <DiagnosisCard tk={tk} admin={admin} />
          <Card>
            <CardHeader icon={<MessageSquare />} title={t("tk.conversation")} description={t("tk.conversation_desc")} />
            <CardBody className="space-y-3">
              {tk.thread.map((m) => <MessageBubble key={m.id} m={m} />)}
            </CardBody>
            {admin && <Composer tk={tk} />}
          </Card>
        </div>
        <div className="space-y-5">
          <Card>
            <CardHeader title={t("tk.details")} />
            <CardBody className="space-y-4">
              <Field label={t("tk.status_label")}>
                <Select value={tk.status} disabled={!admin || upd.isPending} onChange={(e) => upd.mutate({ status: e.target.value })}>
                  {STATUSES.map((s) => <option key={s} value={s}>{t(`tk.status.${s}`)}</option>)}
                </Select>
              </Field>
              <Field label={t("tk.priority_label")}>
                <Select value={tk.priority} disabled={!admin || upd.isPending} onChange={(e) => upd.mutate({ priority: e.target.value })}>
                  {PRIORITIES.map((p) => <option key={p} value={p}>{t(`tk.priority.${p}`)}</option>)}
                </Select>
              </Field>
              <Field label={t("tk.assignee")}>
                <Select value={tk.assignee_id ?? ""} disabled={!admin || upd.isPending} onChange={(e) => upd.mutate({ assignee_id: e.target.value || null })}>
                  <option value="">{t("tk.unassigned")}</option>
                  {(staff.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  {!admin && tk.assignee_name && <option value={tk.assignee_id ?? ""}>{tk.assignee_name}</option>}
                </Select>
              </Field>
              <Field label={t("tk.category_label")}>
                <Select value={tk.category} disabled={!admin || upd.isPending} onChange={(e) => upd.mutate({ category: e.target.value })}>
                  {CATEGORIES.map((c) => <option key={c} value={c}>{t(`tk.category.${c}`)}</option>)}
                </Select>
              </Field>
              <div>
                <div className="mb-1.5 flex items-center justify-between text-[13px] font-semibold">
                  {t("tk.machines")}
                  {admin && editMachines === null && <button className="cursor-pointer text-accent-text hover:underline" onClick={() => setEditMachines(tk.machines.map((m) => m.id))}><Pencil className="inline size-3.5" /> {t("tk.change")}</button>}
                </div>
                {editMachines !== null ? (
                  <div className="space-y-2">
                    <MachinePicker machines={tk.customer_machines} value={editMachines} onChange={setEditMachines} />
                    <div className="flex gap-2">
                      <Button size="sm" variant="dark" loading={upd.isPending} onClick={() => upd.mutate({ node_ids: editMachines }, { onSuccess: () => setEditMachines(null) })}>{t("common.save")}</Button>
                      <Button size="sm" onClick={() => setEditMachines(null)}>{t("common.cancel")}</Button>
                    </div>
                  </div>
                ) : tk.machines.length ? (
                  <ul className="space-y-1 text-sm">
                    {tk.machines.map((m) => {
                      const cm = tk.customer_machines.find((x) => x.id === m.id);
                      return (
                        <li key={m.id} className="flex items-center gap-2">
                          <span className={cn("size-2 rounded-full", cm?.online ? "bg-online" : "bg-line")} />
                          <Link className="hover:underline" to={cm?.type === "workstation" ? `/workstations/${m.id}` : `/servers/${m.id}`}>{m.name}</Link>
                        </li>
                      );
                    })}
                  </ul>
                ) : <p className="text-sm text-muted">{t("tk.no_linked")}</p>}
              </div>
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<UserRound />} title={t("tk.contact")} />
            <CardBody className="space-y-3 text-sm">
              <div>
                <div className="font-semibold">{tk.reporter_name || tk.created_by || "–"}</div>
                {tk.reporter_email && <a className="text-accent-text hover:underline" href={`mailto:${tk.reporter_email}`}>{tk.reporter_email}</a>}
                {tk.reporter?.phone && <div className="text-muted">{tk.reporter.phone}</div>}
                {!tk.reporter_id && tk.created_by && <div className="text-xs text-muted">{t("tk.created_by", { who: tk.created_by })}</div>}
              </div>
              {tk.customer?.maintenance_window && <div><div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("tk.window")}</div>{tk.customer.maintenance_window}</div>}
              {tk.customer?.sla && <div><div className="text-xs font-semibold uppercase tracking-wide text-muted">SLA</div>{tk.customer.sla}</div>}
              <div className="text-xs text-muted">
                {t("tk.created_at")}: {formatDate(tk.created_at, i18n.language)}<br />
                {tk.first_response_at && <>{t("tk.first_response")}: {formatDate(tk.first_response_at, i18n.language)}<br /></>}
                {tk.resolved_at && <>{t("tk.resolved_at")}: {formatDate(tk.resolved_at, i18n.language)}</>}
              </div>
            </CardBody>
          </Card>
          {admin && <Button variant="ghost" className="text-offline" onClick={() => setConfirmDel(true)}><Trash2 /> {t("tk.delete")}</Button>}
        </div>
      </div>
      <Dialog open={confirmDel} onOpenChange={setConfirmDel} title={t("tk.delete_title", { ref: tk.ref })} description={t("tk.delete_desc")}
        footer={<><Button onClick={() => setConfirmDel(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>{t("tk.delete")}</Button></>}>
        <p className="text-sm">{tk.subject}</p>
      </Dialog>
    </div>
  );
}

function MessageBubble({ m }: { m: Message }) {
  const { t, i18n } = useTranslation();
  if (m.author_kind === "system") {
    const e = m.event;
    const text = e.kind === "status" ? t("tk.ev.status", { who: m.author_name || "–", to: t(`tk.status.${e.to ?? "open"}`) })
      : e.kind === "priority" ? t("tk.ev.priority", { who: m.author_name || "–", to: t(`tk.priority.${e.to ?? "normal"}`) })
      : e.kind === "assign" ? t("tk.ev.assign", { who: m.author_name || "–" }) : m.body;
    return (
      <div className="flex items-center gap-3 py-1 text-xs text-muted">
        <div className="h-px flex-1 bg-line" /><span>{text}{e.note ? ` (${e.note})` : ""} · {timeAgo(m.created_at, i18n.language)}</span><div className="h-px flex-1 bg-line" />
      </div>
    );
  }
  const customer = m.author_kind === "customer";
  return (
    <div className={cn("flex", customer ? "justify-start" : "justify-end")}>
      <div className={cn("max-w-[92%] rounded-lg border px-4 py-3 sm:max-w-[80%]",
        m.internal ? "border-warning/40 bg-warning-soft" : customer ? "border-line bg-sunken" : "border-line bg-raised shadow-card")}>
        <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
          {m.author_kind === "ai" ? <Bot className="size-3.5 text-koraal" /> : customer ? <UserRound className="size-3.5 text-muted" /> : <Users className="size-3.5 text-muted" />}
          <span className="font-semibold">{m.author_name || "–"}</span>
          {customer && <Badge className="py-0 text-[10px]">{t("tk.from_customer")}</Badge>}
          {m.internal && <Badge tone="warning" className="py-0 text-[10px]"><Lock className="size-3" /> {t("tk.internal")}</Badge>}
          <span className="text-muted" title={formatDate(m.created_at, i18n.language)}>{timeAgo(m.created_at, i18n.language)}</span>
        </div>
        <div className="whitespace-pre-wrap break-words text-sm">{m.body}</div>
      </div>
    </div>
  );
}

function Composer({ tk }: { tk: Detail }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const [body, setBody] = useState("");
  const [mode, setMode] = useState<"public" | "internal">("public");
  const [after, setAfter] = useState("");
  const send = useMutation({
    mutationFn: () => post(`api/tickets/${tk.id}/messages`, { body, internal: mode === "internal", status: after || null }),
    onSuccess: () => { invalidate(); setBody(""); setAfter(""); toast.success(mode === "internal" ? t("tk.note_saved") : t("tk.reply_sent")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div className="border-t border-line px-5 py-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <Segmented value={mode} onChange={setMode} options={[
          { value: "public", label: <span className="flex items-center gap-1.5"><Mail className="size-3.5" />{t("tk.reply_public")}</span> },
          { value: "internal", label: <span className="flex items-center gap-1.5"><Lock className="size-3.5" />{t("tk.reply_internal")}</span> },
        ]} />
        <span className="text-xs text-muted">{mode === "public" ? t("tk.public_hint") : t("tk.internal_hint")}</span>
      </div>
      <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} className={cn("font-sans text-sm", mode === "internal" && "bg-warning-soft")}
        placeholder={mode === "public" ? t("tk.reply_placeholder") : t("tk.note_placeholder")} />
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
        <Select value={after} onChange={(e) => setAfter(e.target.value)} className="h-10 w-auto">
          <option value="">{t("tk.after_keep")}</option>
          {STATUSES.filter((s) => s !== tk.status && s !== "new").map((s) => <option key={s} value={s}>{t("tk.after_set", { status: t(`tk.status.${s}`) })}</option>)}
        </Select>
        <Button variant="primary" loading={send.isPending} disabled={!body.trim()} onClick={() => send.mutate()}>
          <Send /> {mode === "public" ? t("tk.send") : t("tk.save_note")}
        </Button>
      </div>
    </div>
  );
}

function DiagnosisCard({ tk, admin }: { tk: Detail; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const d = tk.diagnosis;
  const run = useMutation({
    mutationFn: () => post(`api/tickets/${tk.id}/diagnose`),
    onSuccess: () => { invalidate(); toast.success(t("tk.diag.started")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const busy = d.status === "running" || d.status === "queued";
  const tone = d.verdict === "issues_found" ? "warning" : d.verdict === "unreachable" ? "neutral" : "online";
  return (
    <Card>
      <CardHeader icon={<Bot />} title={t("tk.diag.title")} description={d.finished_at ? t("tk.diag.when", { when: timeAgo(d.finished_at, i18n.language) }) : t("tk.diag.desc")}
        action={admin && <Button size="sm" onClick={() => run.mutate()} loading={run.isPending} disabled={busy}><Play /> {d.status ? t("tk.diag.again") : t("tk.diag.run")}</Button>} />
      <CardBody className="space-y-4">
        {!d.status ? <p className="text-sm text-muted">{t("tk.diag.none")}</p>
          : busy ? <div className="flex items-center gap-2 text-sm text-muted"><Spinner className="size-4" /> {t("tk.diag.running")}</div>
          : d.status === "failed" ? <Alert tone="offline">{t("tk.diag.failed")}</Alert>
          : <>
            <Alert tone={tone} title={t(`tk.diag.v.${d.verdict ?? "no_issues"}`)}>
              {d.scanned_all ? t("tk.diag.scanned_all") : t("tk.diag.scanned", { count: d.machines?.length ?? 0 })}
            </Alert>
            {!!d.summary?.length && (
              <ul className="space-y-1 text-sm">
                {d.summary.map((s, i) => <li key={i} className="flex gap-2"><AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" /><span className="break-words">{s}</span></li>)}
              </ul>
            )}
            {!!d.next_steps?.length && (
              <div>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{t("tk.diag.next")}</div>
                <ul className="list-disc space-y-1 pl-5 text-sm">{d.next_steps.map((s, i) => <li key={i}>{s}</li>)}</ul>
              </div>
            )}
            {(d.machines ?? []).map((m) => (
              <details key={m.node_id} className="rounded-md border border-line px-3 py-2">
                <summary className="flex cursor-pointer items-center gap-2 text-sm font-semibold">
                  <span className={cn("size-2 rounded-full", m.status === "ok" ? "bg-online" : m.status === "issues" ? "bg-warning" : "bg-offline")} />
                  {m.name}<span className="font-normal text-muted">· {t(`tk.diag.m.${m.status}`)}</span>
                </summary>
                <div className="mt-2 space-y-2 text-sm">
                  {m.error && <p className="text-offline">{m.error}</p>}
                  {!!m.findings.length && <ul className="list-disc pl-5">{m.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>}
                  {!!m.failed_services.length && <p><b>{t("tk.diag.failed_services")}:</b> {m.failed_services.join(", ")}</p>}
                  {!!m.top_cpu?.length && <p><b>{t("tk.diag.top_cpu")}:</b> {m.top_cpu.join(", ")}</p>}
                  {!!m.recent_errors.length && <pre className="mono max-h-48 overflow-auto rounded bg-sunken p-2 text-[12px]">{m.recent_errors.join("\n")}</pre>}
                </div>
              </details>
            ))}
          </>}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ portal accounts */
function PortalAccounts({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const [edit, setEdit] = useState<PortalUser | "new" | null>(null);
  const list = useQuery({ queryKey: ["portal-users", customerId], queryFn: () => get<PortalUser[]>("api/tickets/portal-users") });
  const invite = useMutation({
    mutationFn: (u: PortalUser) => post<{ kind: string }>(`api/tickets/portal-users/${u.id}/invite`),
    onSuccess: (r) => { invalidate(); toast.success(r.kind === "invite" ? t("tk.acc.invite_sent") : t("tk.acc.reset_sent")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const portalUrl = appUrl("/portal");
  return (
    <div className="space-y-5">
      <Card>
        <CardBody className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold">{t("tk.acc.url")}</div>
            <div className="mono break-all text-sm text-muted">{portalUrl}</div>
          </div>
          <CopyButton value={portalUrl} />
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<Users />} title={t("tk.acc.title")} description={t("tk.acc.desc")}
          action={admin && <Button variant="dark" size="sm" onClick={() => setEdit("new")}><Plus /> {t("tk.acc.add")}</Button>} />
        {list.isLoading ? <div className="p-6"><Spinner /></div> : !(list.data ?? []).length ? (
          <EmptyState icon={<Users />} title={t("tk.acc.empty")}>{t("tk.acc.empty_hint")}</EmptyState>
        ) : (
          <Table>
            <thead><tr><Th>{t("tk.acc.person")}</Th><Th>{t("tk.customer")}</Th><Th>{t("tk.acc.role")}</Th><Th>{t("tk.status_label")}</Th><Th className="hidden md:table-cell">{t("tk.acc.last_login")}</Th>{admin && <Th />}</tr></thead>
            <tbody>
              {(list.data ?? []).map((u) => (
                <Tr key={u.id}>
                  <Td><div className="font-semibold">{u.full_name || u.email}</div><div className="text-xs text-muted">{u.email}</div></Td>
                  <Td className="text-sm">{u.customer_name}</Td>
                  <Td><Badge tone={u.role === "manager" ? "info" : "neutral"}>{t(`tk.acc.role_${u.role}`)}</Badge></Td>
                  <Td>{!u.is_active ? <Badge tone="offline">{t("tk.acc.disabled")}</Badge> : u.activated ? <Badge tone="online" dot>{t("tk.acc.active")}</Badge> : u.invited_at ? <Badge tone="warning">{t("tk.acc.invited")}</Badge> : <Badge>{t("tk.acc.not_invited")}</Badge>}</Td>
                  <Td className="hidden text-sm text-muted md:table-cell">{u.last_login_at ? timeAgo(u.last_login_at, i18n.language) : "–"}</Td>
                  {admin && <Td className="whitespace-nowrap text-right">
                    <Button size="sm" variant="ghost" disabled={!u.is_active} loading={invite.isPending && invite.variables?.id === u.id} onClick={() => invite.mutate(u)} title={u.activated ? t("tk.acc.send_reset") : t("tk.acc.resend")}><Mail /></Button>
                    <Button size="sm" variant="ghost" onClick={() => setEdit(u)} title={t("tk.change")}><Pencil /></Button>
                  </Td>}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {admin && edit && <AccountDialog account={edit === "new" ? null : edit} onClose={() => setEdit(null)} />}
    </div>
  );
}

function AccountDialog({ account, onClose }: { account: PortalUser | null; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const customers = useCustomers();
  const { customerId } = useCustomer();
  const [f, setF] = useState({
    customer_id: account?.customer_id ?? customerId ?? "", email: account?.email ?? "", full_name: account?.full_name ?? "",
    phone: account?.phone ?? "", role: account?.role ?? "member", language: account?.language ?? (document.documentElement.lang === "en" ? "en" : "nl"),
    notify_all: account?.notify_all ?? false, is_active: account?.is_active ?? true, send_invite: true,
  });
  const [confirmDel, setConfirmDel] = useState(false);
  const save = useMutation({
    mutationFn: () => account
      ? patch(`api/tickets/portal-users/${account.id}`, { full_name: f.full_name || null, phone: f.phone || null, role: f.role, language: f.language, notify_all: f.notify_all, is_active: f.is_active })
      : post<{ invite_sent: boolean }>("api/tickets/portal-users", { ...f, full_name: f.full_name || null, phone: f.phone || null }),
    onSuccess: (r) => {
      invalidate();
      const sent = (r as { invite_sent?: boolean } | undefined)?.invite_sent;
      toast.success(!account && f.send_invite ? (sent ? t("tk.acc.invite_sent") : t("tk.acc.invite_failed")) : t("tk.saved"));
      onClose();
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/tickets/portal-users/${account?.id}`),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={account ? t("tk.acc.edit") : t("tk.acc.add")} description={t("tk.acc.dialog_desc")}
      footer={<>
        {account && <Button variant="ghost" className="mr-auto text-offline" onClick={() => setConfirmDel(true)}><Trash2 /> {t("tk.acc.delete")}</Button>}
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!f.customer_id || !f.email.includes("@")} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="space-y-4">
        <Field label={t("tk.customer")}>
          <Select value={f.customer_id} disabled={!!account} onChange={(e) => setF({ ...f, customer_id: e.target.value })}>
            <option value="">–</option>
            {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </Field>
        <Field label={t("tk.acc.email")}><Input type="email" value={f.email} disabled={!!account} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("tk.acc.name")}><Input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></Field>
          <Field label={t("tk.acc.phone")}><Input value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} /></Field>
          <Field label={t("tk.acc.role")} hint={t(`tk.acc.role_${f.role}_hint`)}>
            <Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as "member" | "manager" })}>
              <option value="member">{t("tk.acc.role_member")}</option>
              <option value="manager">{t("tk.acc.role_manager")}</option>
            </Select>
          </Field>
          <Field label={t("tk.acc.language")}>
            <Select value={f.language} onChange={(e) => setF({ ...f, language: e.target.value as "en" | "nl" })}>
              <option value="nl">Nederlands</option><option value="en">English</option>
            </Select>
          </Field>
        </div>
        {f.role === "manager" && <Switch checked={f.notify_all} onCheckedChange={(v) => setF({ ...f, notify_all: v })} label={t("tk.acc.notify_all")} description={t("tk.acc.notify_all_hint")} />}
        {account ? <Switch checked={f.is_active} onCheckedChange={(v) => setF({ ...f, is_active: v })} label={t("tk.acc.enabled")} description={t("tk.acc.enabled_hint")} />
          : <Switch checked={f.send_invite} onCheckedChange={(v) => setF({ ...f, send_invite: v })} label={t("tk.acc.send_invite")} description={t("tk.acc.send_invite_hint")} />}
      </div>
      <Dialog open={confirmDel} onOpenChange={setConfirmDel} title={t("tk.acc.delete")} description={t("tk.acc.delete_desc")}
        footer={<><Button onClick={() => setConfirmDel(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>{t("tk.acc.delete")}</Button></>}>
        <p className="text-sm">{account?.email}</p>
      </Dialog>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ settings */
function PortalSettingsCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["portal-settings"], queryFn: () => get<PortalSettings>("api/tickets/settings") });
  const [f, setF] = useState<PortalSettings | null>(null);
  const v = f ?? s.data;
  const save = useMutation({
    mutationFn: () => put<PortalSettings>("api/tickets/settings", v),
    onSuccess: (r) => { qc.setQueryData(["portal-settings"], r); setF(null); toast.success(t("tk.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const set = useMemo(() => (patchv: Partial<PortalSettings>) => setF({ ...(f ?? s.data!), ...patchv }), [f, s.data]);
  if (!v) return <Spinner />;
  return (
    <Card>
      <CardHeader title={t("tk.set.title")} description={t("tk.set.desc")} />
      <CardBody className="max-w-2xl space-y-5">
        <Switch checked={v.enabled} onCheckedChange={(x) => set({ enabled: x })} label={t("tk.set.enabled")} description={t("tk.set.enabled_hint")} />
        <Switch checked={v.auto_diagnose} onCheckedChange={(x) => set({ auto_diagnose: x })} label={t("tk.set.diagnose")} description={t("tk.set.diagnose_hint")} />
        <Field label={t("tk.set.staff_to")} hint={t("tk.set.staff_to_hint")}><Input value={v.staff_to} onChange={(e) => set({ staff_to: e.target.value })} placeholder="support@example.com" /></Field>
        <Field label={t("tk.set.close_days")} hint={t("tk.set.close_days_hint")}><Input type="number" min={0} max={365} className="w-32" value={v.auto_close_days} onChange={(e) => set({ auto_close_days: Number(e.target.value) || 0 })} /></Field>
        <Field label={t("tk.set.welcome")} hint={t("tk.set.welcome_hint")}><Textarea className="font-sans text-sm" rows={3} value={v.welcome} onChange={(e) => set({ welcome: e.target.value })} /></Field>
        <Button variant="primary" loading={save.isPending} disabled={!f} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </CardBody>
    </Card>
  );
}
