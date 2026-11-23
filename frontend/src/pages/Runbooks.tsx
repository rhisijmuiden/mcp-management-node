import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown, ArrowUp, BookOpenCheck, CheckCircle2, ChevronDown, Circle, CircleSlash, Clock, Copy, Hand, Hourglass,
  ListChecks, Loader2, Pencil, Play, Plus, RotateCcw, Save, SkipForward, Square, Trash2, User as UserIcon, Wrench, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";
import { ApprovalDialog } from "@/pages/Gateway";

/* ------------------------------------------------------------------ types */
type Category = "maintenance" | "onboarding" | "project" | "remediation" | "other";
const CATEGORIES: Category[] = ["maintenance", "onboarding", "project", "remediation", "other"];
type Kind = "tool" | "manual" | "wait";
type RunStatus = "running" | "waiting_approval" | "waiting_input" | "succeeded" | "failed" | "cancelled";
type StepStatus = "pending" | "running" | "waiting_approval" | "waiting_job" | "waiting_time" | "waiting_input" | "succeeded" | "failed" | "rejected" | "skipped" | "cancelled";
const FINAL: RunStatus[] = ["succeeded", "failed", "cancelled"];

interface Param { name: string; label: string; description?: string; required: boolean; default: string | null }
interface Step {
  id: string;
  kind: Kind;
  title: string;
  tool?: string;
  args?: Record<string, unknown>;
  approval?: "policy" | "always";
  on_failure?: "stop" | "continue";
  optional?: boolean;
  wait_for_job?: boolean;
  instructions?: string;
  assignee?: "human" | "ai";
  minutes?: number;
  risk?: string;
}
interface Runbook {
  id: string;
  name: string;
  description: string | null;
  category: Category;
  customer_id: string | null;
  customer_name: string | null;
  enabled: boolean;
  use_window: boolean;
  params: Param[];
  steps: Step[];
  version: number;
  template: string | null;
  updated_at: string;
  updated_by: string | null;
  runs?: number;
  last_run_at?: string | null;
  active?: number;
}
interface Template { key: string; category: Category; name: string; description: string; params: Param[]; steps: Step[] }
interface ToolInfo { name: string; risk: string; scope: string; description: string; source: string; params: { name: string; required: boolean; default?: unknown; type?: string; description?: string }[] }
interface StepState {
  id: string;
  status: StepStatus;
  approval_id: string | null;
  job_id: string | null;
  wait_until: string | null;
  started_at: string | null;
  finished_at: string | null;
  args: Record<string, unknown> | null;
  result: unknown;
  error: string | null;
  decided_by: string | null;
  note: string | null;
}
interface Run {
  id: string;
  runbook_id: string | null;
  runbook_name: string;
  runbook_version: number;
  customer_id: string | null;
  customer_name: string | null;
  status: RunStatus;
  current_step: number;
  steps_total: number;
  steps_done: number;
  trigger: "ui" | "mcp" | "remediation" | "schedule";
  reason: string | null;
  started_by: string | null;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  params?: Record<string, unknown>;
  steps?: (Step & { state: StepState })[];
}

const RUN_TONE: Record<RunStatus, "online" | "warning" | "offline" | "info" | "neutral"> = {
  running: "info", waiting_approval: "warning", waiting_input: "warning", succeeded: "online", failed: "offline", cancelled: "neutral",
};
const STEP_ICON: Record<StepStatus, ReactNode> = {
  pending: <Circle className="text-muted" />,
  running: <Loader2 className="animate-spin text-ink" />,
  waiting_approval: <Hand className="text-warning" />,
  waiting_job: <Loader2 className="animate-spin text-ink" />,
  waiting_time: <Hourglass className="text-ink" />,
  waiting_input: <UserIcon className="text-warning" />,
  succeeded: <CheckCircle2 className="text-online" />,
  failed: <XCircle className="text-offline" />,
  rejected: <XCircle className="text-offline" />,
  skipped: <SkipForward className="text-muted" />,
  cancelled: <CircleSlash className="text-muted" />,
};

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    for (const k of ["runbooks", "runbook", "runbook-runs", "runbook-run", "runbook-runs-count", "approvals", "approvals-count"]) qc.invalidateQueries({ queryKey: [k] });
  };
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const { t } = useTranslation();
  return <Badge tone={RUN_TONE[status]} dot>{t(`rb.status.${status}`)}</Badge>;
}

function CategoryBadge({ c }: { c: Category }) {
  const { t } = useTranslation();
  return <Badge tone={c === "remediation" ? "accent" : "neutral"}>{t(`rb.cat.${c}`)}</Badge>;
}

/* ------------------------------------------------------------------ list page */
export function RunbooksPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { customerId } = useCustomer();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "runbooks";
  const [start, setStart] = useState<Runbook | null>(null);
  const [tplOpen, setTplOpen] = useState(false);
  const navigate = useNavigate();
  const list = useQuery({
    queryKey: ["runbooks", customerId],
    queryFn: () => get<Runbook[]>(`api/runbooks${customerId ? `?customer_id=${customerId}` : ""}`),
  });
  const runs = useQuery({
    queryKey: ["runbook-runs", customerId],
    queryFn: () => get<Run[]>(`api/runbooks/runs?limit=100${customerId ? `&customer_id=${customerId}` : ""}`),
    refetchInterval: 15_000,
  });
  const active = (runs.data ?? []).filter((r) => !FINAL.includes(r.status));

  return (
    <div>
      <PageHeader
        title={t("rb.title")}
        description={t("rb.subtitle")}
        actions={admin && (
          <>
            <Button onClick={() => setTplOpen(true)}><Copy />{t("rb.from_template")}</Button>
            <Button variant="primary" onClick={() => navigate("/runbooks/new")}><Plus />{t("rb.new")}</Button>
          </>
        )}
      />
      {active.length > 0 && (
        <Card className="mb-5">
          <CardHeader icon={<Loader2 />} title={t("rb.active_title", { count: active.length })} description={t("rb.active_hint")} />
          <div className="divide-y divide-line">
            {active.map((r) => <RunLine key={r.id} r={r} />)}
          </div>
        </Card>
      )}
      <Tabs.Root value={tab} onValueChange={(v) => setParams(v === "runbooks" ? {} : { tab: v }, { replace: true })}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="runbooks" className={tabCls}>{t("rb.tab_runbooks")}</Tabs.Trigger>
          <Tabs.Trigger value="runs" className={tabCls}>{t("rb.tab_runs")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="runbooks">
          {list.isLoading ? <Spinner /> : list.isError ? <Alert tone="offline">{errorMessage(list.error, t)}</Alert> : !list.data?.length ? (
            <Card><CardBody>
              <EmptyState icon={<BookOpenCheck />} title={t("rb.empty_title")} action={admin && <Button variant="primary" onClick={() => setTplOpen(true)}><Copy />{t("rb.from_template")}</Button>}>
                {t("rb.empty_text")}
              </EmptyState>
            </CardBody></Card>
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {list.data.map((rb) => (
                <Card key={rb.id} className={cn("flex flex-col", !rb.enabled && "opacity-60")}>
                  <div className="flex-1 px-5 pt-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <CategoryBadge c={rb.category} />
                      <Badge tone={rb.customer_id ? "info" : "neutral"}>{rb.customer_name ?? t("rb.all_customers")}</Badge>
                      {!rb.enabled && <Badge>{t("rb.disabled")}</Badge>}
                    </div>
                    <h3 className="mt-2 text-[16px] font-bold">{rb.name}</h3>
                    {rb.description && <p className="mt-1 line-clamp-3 text-sm text-muted">{rb.description}</p>}
                    <ol className="mt-3 space-y-1 text-[13px]">
                      {rb.steps.slice(0, 5).map((s, i) => (
                        <li key={s.id} className="flex items-center gap-2">
                          <span className="grid size-5 shrink-0 place-items-center rounded-full bg-sunken text-[11px] font-bold">{i + 1}</span>
                          <span className="truncate">{s.title}</span>
                          {s.kind === "tool" && s.approval === "always" && <Hand className="size-3.5 shrink-0 text-warning" aria-label={t("rb.approval_always")} />}
                        </li>
                      ))}
                      {rb.steps.length > 5 && <li className="pl-7 text-muted">{t("rb.more_steps", { count: rb.steps.length - 5 })}</li>}
                    </ol>
                  </div>
                  <div className="mt-4 flex items-center justify-between gap-2 border-t border-line px-5 py-3">
                    <span className="text-xs text-muted">
                      {rb.last_run_at ? t("rb.last_run", { when: timeAgo(rb.last_run_at, i18n.language), count: rb.runs ?? 0 }) : t("rb.never_run")}
                    </span>
                    {admin && (
                      <span className="flex gap-1.5">
                        <Button size="sm" variant="ghost" onClick={() => navigate(`/runbooks/${rb.id}`)} aria-label={t("common.edit")}><Pencil /></Button>
                        <Button size="sm" variant="dark" disabled={!rb.enabled} onClick={() => setStart(rb)}><Play />{t("rb.run")}</Button>
                      </span>
                    )}
                  </div>
                </Card>
              ))}
            </div>
          )}
        </Tabs.Content>
        <Tabs.Content value="runs">
          <Card>
            {runs.isLoading ? <CardBody><Spinner /></CardBody> : !runs.data?.length ? (
              <CardBody><EmptyState icon={<ListChecks />} title={t("rb.no_runs")} /></CardBody>
            ) : (
              <Table>
                <thead><tr><Th>{t("rb.col_runbook")}</Th><Th>{t("rb.col_customer")}</Th><Th>{t("rb.col_status")}</Th><Th>{t("rb.col_progress")}</Th><Th>{t("rb.col_by")}</Th><Th>{t("rb.col_started")}</Th></tr></thead>
                <tbody>
                  {runs.data.map((r) => (
                    <Tr key={r.id} className="cursor-pointer" onClick={() => navigate(`/runbooks/runs/${r.id}`)}>
                      <Td className="font-semibold">{r.runbook_name}</Td>
                      <Td>{r.customer_name ?? "–"}</Td>
                      <Td><RunStatusBadge status={r.status} /></Td>
                      <Td>{r.steps_done}/{r.steps_total}</Td>
                      <Td>{r.started_by ?? "–"}{r.trigger !== "ui" && <span className="ml-1 text-xs text-muted">({t(`rb.trigger.${r.trigger}`)})</span>}</Td>
                      <Td className="text-muted">{formatDate(r.created_at, i18n.language)}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </Tabs.Content>
      </Tabs.Root>
      {start && <StartDialog rb={start} onClose={() => setStart(null)} />}
      <TemplateDialog open={tplOpen} onClose={() => setTplOpen(false)} />
    </div>
  );
}

function RunLine({ r }: { r: Run }) {
  const { t, i18n } = useTranslation();
  return (
    <Link to={`/runbooks/runs/${r.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 text-sm hover:bg-sunken/60">
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{r.runbook_name}</span>
          <RunStatusBadge status={r.status} />
        </span>
        <span className="block truncate text-xs text-muted">
          {r.customer_name ?? t("rb.all_customers")} · {t("rb.step_of", { n: Math.min(r.current_step + 1, r.steps_total), total: r.steps_total })} · {r.started_by ?? "–"}
        </span>
      </span>
      <span className="text-xs text-muted">{timeAgo(r.created_at, i18n.language)}</span>
      <span className="text-xs font-semibold text-accent-text">{t("rb.open")} →</span>
    </Link>
  );
}

/* ------------------------------------------------------------------ start a run */
function StartDialog({ rb, onClose }: { rb: Runbook; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const customers = useCustomers();
  const { customerId } = useCustomer();
  const [customer, setCustomer] = useState(rb.customer_id ?? customerId ?? "");
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(rb.params.map((p) => [p.name, p.default ?? ""])));
  const [reason, setReason] = useState("");
  const m = useMutation({
    mutationFn: () => post<Run>(`api/runbooks/${rb.id}/run`, {
      customer_id: customer || null,
      params: Object.fromEntries(Object.entries(values).filter(([, v]) => v !== "")),
      reason: reason || null,
    }),
    onSuccess: (r) => {
      invalidate();
      toast.success(t("rb.started"));
      navigate(`/runbooks/runs/${r.id}`);
    },
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("rb.start_title", { name: rb.name })} description={rb.description ?? undefined}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={m.isPending} onClick={() => m.mutate()}><Play />{t("rb.start")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("rb.customer")} hint={rb.customer_id ? t("rb.customer_fixed") : t("rb.customer_hint")}>
          <Select value={customer} disabled={!!rb.customer_id} onChange={(e) => setCustomer(e.target.value)}>
            <option value="">{t("rb.no_customer")}</option>
            {customers.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </Field>
        {rb.params.map((p) => (
          <Field key={p.name} label={<>{p.label}{p.required && <span className="text-offline"> *</span>}</>} hint={p.description || <span className="mono">{`{{params.${p.name}}}`}</span>}>
            <Input value={values[p.name] ?? ""} onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
          </Field>
        ))}
        <Field label={t("rb.reason")} hint={t("rb.reason_hint")}>
          <Input value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Alert tone="info">{t("rb.start_note")}</Alert>
        {m.isError && <Alert tone="offline">{errorMessage(m.error, t)}</Alert>}
      </div>
    </Dialog>
  );
}

function TemplateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["runbook-templates", i18n.language], queryFn: () => get<Template[]>(`api/runbooks/templates?lang=${i18n.language}`), enabled: open });
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()} wide title={t("rb.templates_title")} description={t("rb.templates_hint")}>
      {q.isLoading ? <Spinner /> : (
        <div className="grid gap-3 sm:grid-cols-2">
          {q.data?.map((tpl) => (
            <button key={tpl.key} type="button" onClick={() => { onClose(); navigate(`/runbooks/new?template=${tpl.key}`); }}
              className="cursor-pointer rounded-md border border-line p-4 text-left transition-colors hover:border-koraal hover:bg-sunken/50">
              <CategoryBadge c={tpl.category} />
              <div className="mt-2 font-bold">{tpl.name}</div>
              <p className="mt-1 text-sm text-muted">{tpl.description}</p>
              <div className="mt-2 text-xs text-muted">{t("rb.steps_count", { count: tpl.steps.length })}</div>
            </button>
          ))}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ editor */
const EMPTY: Omit<Runbook, "id" | "version" | "updated_at" | "updated_by" | "customer_name" | "template"> = {
  name: "", description: "", category: "other", customer_id: null, enabled: true, use_window: true, params: [], steps: [],
};

function newStep(kind: Kind, n: number): Step {
  if (kind === "manual") return { id: `s${n}`, kind, title: "", instructions: "", assignee: "human", on_failure: "stop" };
  if (kind === "wait") return { id: `s${n}`, kind, title: "", minutes: 5, on_failure: "stop" };
  return { id: `s${n}`, kind, title: "", tool: "", args: {}, approval: "policy", on_failure: "stop", optional: false, wait_for_job: false };
}

function uniqueId(steps: Step[]): string {
  let n = steps.length + 1;
  while (steps.some((s) => s.id === `s${n}`)) n++;
  return `s${n}`;
}

export function RunbookEditorPage() {
  const { t } = useTranslation();
  const { id } = useParams();
  const [search] = useSearchParams();
  const template = search.get("template");
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const { i18n } = useTranslation();
  const customers = useCustomers();
  const { customerId } = useCustomer();
  const isNew = !id || id === "new";
  const existing = useQuery({ queryKey: ["runbook", id], queryFn: () => get<Runbook>(`api/runbooks/${id}`), enabled: !isNew });
  const tpl = useQuery({ queryKey: ["runbook-templates", i18n.language], queryFn: () => get<Template[]>(`api/runbooks/templates?lang=${i18n.language}`), enabled: isNew && !!template });
  const tools = useQuery({ queryKey: ["runbook-tools"], queryFn: () => get<ToolInfo[]>("api/runbooks/tools"), staleTime: 5 * 60_000 });
  const [form, setForm] = useState<typeof EMPTY & { template?: string | null }>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (loaded) return;
    if (!isNew && existing.data) {
      const r = existing.data;
      setForm({ name: r.name, description: r.description ?? "", category: r.category, customer_id: r.customer_id, enabled: r.enabled, use_window: r.use_window, params: r.params, steps: r.steps, template: r.template });
      setLoaded(true);
    } else if (isNew && template && tpl.data) {
      const x = tpl.data.find((x) => x.key === template);
      if (x) setForm({ ...EMPTY, name: x.name, description: x.description, category: x.category, customer_id: customerId || null, params: x.params, steps: x.steps, template: x.key });
      setLoaded(true);
    } else if (isNew && !template) {
      setForm({ ...EMPTY, customer_id: customerId || null, steps: [newStep("tool", 1)] });
      setLoaded(true);
    }
  }, [existing.data, tpl.data, isNew, template, loaded, customerId]);

  const save = useMutation({
    mutationFn: () => {
      const body = { ...form, description: form.description || null, customer_id: form.customer_id || null };
      return isNew ? post<Runbook>("api/runbooks", body) : put<Runbook>(`api/runbooks/${id}`, body);
    },
    onSuccess: (r) => {
      invalidate();
      toast.success(t("common.saved"));
      navigate(isNew ? `/runbooks/${r.id}` : "/runbooks", { replace: isNew });
    },
  });
  const remove = useMutation({
    mutationFn: () => del(`api/runbooks/${id}`),
    onSuccess: () => { invalidate(); toast.success(t("rb.deleted")); navigate("/runbooks"); },
  });

  const setStep = (i: number, s: Step) => setForm({ ...form, steps: form.steps.map((x, j) => (j === i ? s : x)) });
  const move = (i: number, d: number) => {
    const steps = [...form.steps];
    const [s] = steps.splice(i, 1);
    steps.splice(i + d, 0, s);
    setForm({ ...form, steps });
  };

  if (!isNew && existing.isLoading) return <Spinner />;
  if (!isNew && existing.isError) return <Alert tone="offline">{errorMessage(existing.error, t)}</Alert>;

  return (
    <div>
      <PageHeader
        title={isNew ? t("rb.new") : t("rb.edit_title", { name: existing.data?.name ?? "" })}
        description={t("rb.editor_hint")}
        actions={<>
          <Button onClick={() => navigate("/runbooks")}>{t("common.cancel")}</Button>
          {!isNew && <Button variant="ghost" onClick={() => setConfirmDelete(true)}><Trash2 />{t("common.delete")}</Button>}
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}><Save />{t("common.save")}</Button>
        </>}
      />
      {save.isError && <Alert tone="offline" className="mb-4">{errorMessage(save.error, t)}</Alert>}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-5">
          <Card>
            <CardHeader icon={<BookOpenCheck />} title={t("rb.general")} />
            <CardBody className="grid gap-4 sm:grid-cols-2">
              <Field label={t("rb.name")} className="sm:col-span-2"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
              <Field label={t("rb.description")} className="sm:col-span-2">
                <Textarea className="min-h-16 font-sans! text-sm" value={form.description ?? ""} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
              <Field label={t("rb.category")}>
                <Select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value as Category })}>
                  {CATEGORIES.map((c) => <option key={c} value={c}>{t(`rb.cat.${c}`)}</option>)}
                </Select>
              </Field>
              <Field label={t("rb.customer")} hint={t("rb.customer_scope_hint")}>
                <Select value={form.customer_id ?? ""} onChange={(e) => setForm({ ...form, customer_id: e.target.value || null })}>
                  <option value="">{t("rb.all_customers")}</option>
                  {customers.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </Select>
              </Field>
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} label={t("rb.enabled")} />
              <Switch checked={form.use_window} onCheckedChange={(v) => setForm({ ...form, use_window: v })} label={t("rb.use_window")} description={t("rb.use_window_hint")} />
            </CardBody>
          </Card>

          <div className="space-y-3">
            <h2 className="text-[15px] font-bold">{t("rb.steps")}</h2>
            {form.steps.map((s, i) => (
              <StepEditor key={i} index={i} step={s} steps={form.steps} tools={tools.data ?? []}
                onChange={(x) => setStep(i, x)}
                onMove={(d) => move(i, d)}
                onRemove={() => setForm({ ...form, steps: form.steps.filter((_, j) => j !== i) })}
                last={i === form.steps.length - 1} />
            ))}
            <div className="flex flex-wrap gap-2">
              {(["tool", "manual", "wait"] as Kind[]).map((k) => (
                <Button key={k} size="sm" onClick={() => setForm({ ...form, steps: [...form.steps, { ...newStep(k, form.steps.length + 1), id: uniqueId(form.steps) }] })}>
                  <Plus />{t(`rb.add_${k}`)}
                </Button>
              ))}
            </div>
          </div>
        </div>

        <div className="space-y-5">
          <Card>
            <CardHeader title={t("rb.params")} description={t("rb.params_hint")}
              action={<Button size="sm" onClick={() => setForm({ ...form, params: [...form.params, { name: `param${form.params.length + 1}`, label: "", required: true, default: null }] })}><Plus /></Button>} />
            <CardBody className="space-y-3">
              {!form.params.length && <p className="text-sm text-muted">{t("rb.no_params")}</p>}
              {form.params.map((p, i) => (
                <div key={i} className="space-y-2 rounded-md border border-line p-3">
                  <div className="flex gap-2">
                    <Input className="mono h-9 text-[13px]" value={p.name} aria-label={t("rb.param_name")} placeholder={t("rb.param_name")}
                      onChange={(e) => setForm({ ...form, params: form.params.map((x, j) => (j === i ? { ...x, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_") } : x)) })} />
                    <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => setForm({ ...form, params: form.params.filter((_, j) => j !== i) })}><Trash2 /></Button>
                  </div>
                  <Input className="h-9" value={p.label} placeholder={t("rb.param_label")} onChange={(e) => setForm({ ...form, params: form.params.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })} />
                  <Input className="h-9" value={p.default ?? ""} placeholder={t("rb.param_default")} onChange={(e) => setForm({ ...form, params: form.params.map((x, j) => (j === i ? { ...x, default: e.target.value || null } : x)) })} />
                  <Checkbox checked={p.required} label={t("rb.param_required")} onCheckedChange={(v) => setForm({ ...form, params: form.params.map((x, j) => (j === i ? { ...x, required: v } : x)) })} />
                  <div className="mono text-xs text-muted">{`{{params.${p.name}}}`}</div>
                </div>
              ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={t("rb.placeholders")} />
            <CardBody className="space-y-1.5 text-[13px]">
              {["params.<name>", "customer.name", "customer.id", "run.id", "steps.<id>.result.<field>", "steps.<id>.note"].map((p) => (
                <div key={p}><span className="mono rounded bg-sunken px-1.5 py-0.5">{`{{${p}}}`}</span> <span className="text-muted">{t(`rb.ph.${p.split(".")[0]}${p.includes("note") ? "_note" : p.includes("customer.id") ? "_id" : ""}`)}</span></div>
              ))}
              <p className="pt-2 text-xs text-muted">{t("rb.ph_hint")}</p>
            </CardBody>
          </Card>
        </div>
      </div>
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete} title={t("rb.delete_title")} description={t("rb.delete_text")}
        footer={<><Button onClick={() => setConfirmDelete(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 />{t("common.delete")}</Button></>}>
        {remove.isError ? <Alert tone="offline">{errorMessage(remove.error, t)}</Alert> : <p className="text-sm">{existing.data?.name}</p>}
      </Dialog>
    </div>
  );
}

function argText(v: unknown): string {
  if (v === undefined || v === null) return "";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function parseArg(text: string, type?: string): unknown {
  const s = text.trim();
  if (/^\{\{.*\}\}$/.test(s)) return s;
  if (type === "bool" || type === "boolean") return s === "true" ? true : s === "false" ? false : s;
  if (type === "int" || type === "integer" || type === "number" || type === "float") return s !== "" && !Number.isNaN(Number(s)) ? Number(s) : s;
  if ((type?.startsWith("list") || type?.startsWith("dict") || type === "array" || type === "object") && /^[[{]/.test(s)) {
    try { return JSON.parse(s); } catch { return s; }
  }
  return text;
}

function StepEditor({ index, step, steps, tools, onChange, onMove, onRemove, last }: {
  index: number; step: Step; steps: Step[]; tools: ToolInfo[]; onChange: (s: Step) => void; onMove: (d: number) => void; onRemove: () => void; last: boolean;
}) {
  const { t } = useTranslation();
  const tool = tools.find((x) => x.name === step.tool);
  const byRisk = useMemo(() => {
    const g: Record<string, ToolInfo[]> = {};
    for (const x of tools) (g[x.risk] ??= []).push(x);
    return g;
  }, [tools]);
  const args = step.args ?? {};
  const knownNames = new Set(tool?.params.map((p) => p.name) ?? []);
  const extra = Object.keys(args).filter((k) => !knownNames.has(k));
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span className="grid size-7 place-items-center rounded-full bg-nacht text-[13px] font-bold text-white dark:bg-lucht dark:text-nacht">{index + 1}</span>
        <Segmented<Kind> value={step.kind} onChange={(k) => onChange({ ...newStep(k, index + 1), id: step.id, title: step.title })}
          options={[{ value: "tool", label: t("rb.kind.tool") }, { value: "manual", label: t("rb.kind.manual") }, { value: "wait", label: t("rb.kind.wait") }]} />
        <span className="mono text-xs text-muted">id: {step.id}</span>
        <span className="ml-auto flex gap-1">
          <Button size="icon" variant="ghost" disabled={index === 0} onClick={() => onMove(-1)} aria-label={t("rb.move_up")}><ArrowUp /></Button>
          <Button size="icon" variant="ghost" disabled={last} onClick={() => onMove(1)} aria-label={t("rb.move_down")}><ArrowDown /></Button>
          <Button size="icon" variant="ghost" disabled={steps.length === 1} onClick={onRemove} aria-label={t("common.delete")}><Trash2 /></Button>
        </span>
      </div>
      <CardBody className="grid gap-3 sm:grid-cols-2">
        <Field label={t("rb.step_title")} className="sm:col-span-2"><Input value={step.title} onChange={(e) => onChange({ ...step, title: e.target.value })} /></Field>
        {step.kind === "tool" && (
          <>
            <Field label={t("rb.tool")} className="sm:col-span-2" hint={tool?.description}>
              <Select value={step.tool ?? ""} onChange={(e) => onChange({ ...step, tool: e.target.value, args: {} })}>
                <option value="">{t("rb.choose_tool")}</option>
                {step.tool && !tool && <option value={step.tool}>{step.tool}</option>}
                {["read", "operate", "change", "destructive"].map((r) => byRisk[r]?.length ? (
                  <optgroup key={r} label={t(`gw.risk.${r}`)}>
                    {byRisk[r].map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
                  </optgroup>
                ) : null)}
              </Select>
            </Field>
            {tool?.params.map((p) => (
              <Field key={p.name} label={<span className="mono">{p.name}{p.required && <span className="text-offline"> *</span>}</span>} hint={p.description || (p.type ? <span className="mono">{p.type}{p.default !== undefined && p.default !== null ? ` = ${argText(p.default)}` : ""}</span> : undefined)}>
                <Input className="mono h-9 text-[13px]" value={argText(args[p.name])}
                  onChange={(e) => {
                    const next = { ...args };
                    if (e.target.value === "") delete next[p.name];
                    else next[p.name] = parseArg(e.target.value, p.type);
                    onChange({ ...step, args: next });
                  }} />
              </Field>
            ))}
            {extra.map((k) => (
              <Field key={k} label={<span className="mono">{k}</span>}>
                <div className="flex gap-2">
                  <Input className="mono h-9 text-[13px]" value={argText(args[k])} onChange={(e) => onChange({ ...step, args: { ...args, [k]: e.target.value } })} />
                  <Button size="icon" variant="ghost" onClick={() => { const next = { ...args }; delete next[k]; onChange({ ...step, args: next }); }} aria-label={t("common.delete")}><Trash2 /></Button>
                </div>
              </Field>
            ))}
            <Field label={t("rb.approval")}>
              <Select value={step.approval ?? "policy"} onChange={(e) => onChange({ ...step, approval: e.target.value as "policy" | "always" })}>
                <option value="policy">{t("rb.approval_policy")}</option>
                <option value="always">{t("rb.approval_always")}</option>
              </Select>
            </Field>
            <Field label={t("rb.on_failure")}>
              <Select value={step.on_failure ?? "stop"} onChange={(e) => onChange({ ...step, on_failure: e.target.value as "stop" | "continue" })}>
                <option value="stop">{t("rb.on_failure_stop")}</option>
                <option value="continue">{t("rb.on_failure_continue")}</option>
              </Select>
            </Field>
            <Checkbox checked={!!step.optional} onCheckedChange={(v) => onChange({ ...step, optional: v })} label={t("rb.optional")} description={t("rb.optional_hint")} />
            <Checkbox checked={!!step.wait_for_job} onCheckedChange={(v) => onChange({ ...step, wait_for_job: v })} label={t("rb.wait_for_job")} description={t("rb.wait_for_job_hint")} />
          </>
        )}
        {step.kind === "manual" && (
          <>
            <Field label={t("rb.instructions")} className="sm:col-span-2">
              <Textarea className="font-sans! text-sm" value={step.instructions ?? ""} onChange={(e) => onChange({ ...step, instructions: e.target.value })} />
            </Field>
            <Field label={t("rb.assignee")}>
              <Select value={step.assignee ?? "human"} onChange={(e) => onChange({ ...step, assignee: e.target.value as "human" | "ai" })}>
                <option value="human">{t("rb.assignee_human")}</option>
                <option value="ai">{t("rb.assignee_ai")}</option>
              </Select>
            </Field>
            <Field label={t("rb.on_failure")}>
              <Select value={step.on_failure ?? "stop"} onChange={(e) => onChange({ ...step, on_failure: e.target.value as "stop" | "continue" })}>
                <option value="stop">{t("rb.on_failure_stop")}</option>
                <option value="continue">{t("rb.on_failure_continue")}</option>
              </Select>
            </Field>
          </>
        )}
        {step.kind === "wait" && (
          <Field label={t("rb.minutes")}>
            <Input type="number" min={1} max={1440} value={step.minutes ?? 5} onChange={(e) => onChange({ ...step, minutes: Number(e.target.value) })} />
          </Field>
        )}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ run detail */
export function RunbookRunPage() {
  const { t, i18n } = useTranslation();
  const { id } = useParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const invalidate = useInvalidate();
  const [approval, setApproval] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const q = useQuery({
    queryKey: ["runbook-run", id],
    queryFn: () => get<Run>(`api/runbooks/runs/${id}`),
    refetchInterval: (query) => (query.state.data && FINAL.includes(query.state.data.status) ? false : 3000),
  });
  const act = useMutation({
    mutationFn: (a: { path: string; body?: unknown }) => post<Run>(`api/runbooks/runs/${id}/${a.path}`, a.body),
    onSuccess: () => { invalidate(); setNote(""); setConfirmCancel(false); },
  });
  const r = q.data;
  if (q.isLoading) return <Spinner />;
  if (q.isError || !r) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const final = FINAL.includes(r.status);
  return (
    <div>
      <PageHeader
        title={<span className="flex flex-wrap items-center gap-3">{r.runbook_name}<RunStatusBadge status={r.status} /></span>}
        description={<>
          {r.customer_name ?? t("rb.all_customers")} · {t("rb.started_by", { who: r.started_by ?? "–", when: formatDate(r.created_at, i18n.language) })}
          {r.trigger !== "ui" && <> · {t(`rb.trigger.${r.trigger}`)}</>}
          {r.reason && <> · “{r.reason}”</>}
        </>}
        actions={<>
          <Link to="/runbooks?tab=runs"><Button>{t("rb.all_runs")}</Button></Link>
          {admin && r.runbook_id && <Link to={`/runbooks/${r.runbook_id}`}><Button variant="ghost"><Pencil />{t("rb.edit_runbook")}</Button></Link>}
          {admin && !final && <Button variant="danger" onClick={() => setConfirmCancel(true)}><Square />{t("rb.cancel_run")}</Button>}
          {admin && r.status === "failed" && r.current_step < r.steps_total && (
            <>
              <Button loading={act.isPending} onClick={() => act.mutate({ path: "retry", body: { skip: true } })}><SkipForward />{t("rb.skip")}</Button>
              <Button variant="primary" loading={act.isPending} onClick={() => act.mutate({ path: "retry", body: { skip: false } })}><RotateCcw />{t("rb.retry")}</Button>
            </>
          )}
        </>}
      />
      {r.error && <Alert tone={r.status === "cancelled" ? "neutral" : "offline"} className="mb-4">{r.error}</Alert>}
      {act.isError && <Alert tone="offline" className="mb-4">{errorMessage(act.error, t)}</Alert>}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card>
          <CardHeader icon={<ListChecks />} title={t("rb.steps")} description={t("rb.progress", { done: r.steps_done, total: r.steps_total })} />
          <ol className="divide-y divide-line">
            {r.steps?.map((s, i) => {
              const st = s.state;
              const current = i === r.current_step && !final;
              return (
                <li key={s.id} className={cn("flex gap-3 px-5 py-4", current && "bg-sunken/50")}>
                  <span className="mt-0.5 [&_svg]:size-5">{STEP_ICON[st.status]}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{i + 1}. {s.title}</span>
                      <Badge>{t(`rb.step_status.${st.status}`)}</Badge>
                      {s.kind === "tool" && <span className="mono text-xs text-muted"><Wrench className="mr-1 inline size-3" />{s.tool}</span>}
                      {s.kind === "manual" && <span className="text-xs text-muted">{t(`rb.assignee_${s.assignee ?? "human"}`)}</span>}
                      {s.kind === "wait" && <span className="text-xs text-muted"><Clock className="mr-1 inline size-3" />{t("rb.wait_minutes", { count: s.minutes ?? 0 })}</span>}
                    </div>
                    {s.kind === "manual" && (st.args?.instructions || s.instructions) && (
                      <p className="mt-1 whitespace-pre-wrap text-sm">{String(st.args?.instructions ?? fill(s.instructions ?? "", r))}</p>
                    )}
                    {st.status === "waiting_time" && st.wait_until && <p className="mt-1 text-sm text-muted">{t("rb.waiting_until", { when: formatDate(st.wait_until, i18n.language) })}</p>}
                    {st.decided_by && <p className="mt-1 text-xs text-muted">{t("rb.decided_by", { who: st.decided_by })}</p>}
                    {st.note && <p className="mt-1 text-sm">“{st.note}”</p>}
                    {st.error && <Alert tone={st.status === "skipped" ? "neutral" : "offline"} className="mt-2">{st.error}</Alert>}
                    {st.status === "waiting_approval" && st.approval_id && admin && (
                      <Button size="sm" variant="primary" className="mt-2" onClick={() => setApproval(st.approval_id)}><Hand />{t("rb.review_approval")}</Button>
                    )}
                    {st.status === "waiting_input" && current && admin && (
                      <div className="mt-3 space-y-2">
                        <Textarea className="min-h-16 font-sans! text-sm" placeholder={t("rb.note_placeholder")} value={note} onChange={(e) => setNote(e.target.value)} />
                        <div className="flex flex-wrap gap-2">
                          <Button size="sm" variant="primary" loading={act.isPending} onClick={() => act.mutate({ path: "complete", body: { ok: true, note: note || null } })}><CheckCircle2 />{t("rb.mark_done")}</Button>
                          <Button size="sm" loading={act.isPending} onClick={() => act.mutate({ path: "complete", body: { ok: false, note: note || null } })}><XCircle />{t("rb.mark_failed")}</Button>
                        </div>
                        {s.assignee === "ai" && <p className="text-xs text-muted">{t("rb.ai_step_hint")}</p>}
                      </div>
                    )}
                    {(st.args && s.kind === "tool") || st.result !== null && st.result !== undefined ? (
                      <Details label={t("rb.details")}>
                        {st.args && s.kind === "tool" && <><div className="text-xs font-semibold text-muted">{t("rb.arguments")}</div><pre className="mono mt-1 max-h-48 overflow-auto rounded bg-sunken p-2 text-[12px]">{JSON.stringify(st.args, null, 2)}</pre></>}
                        {st.result !== null && st.result !== undefined && <><div className="mt-2 text-xs font-semibold text-muted">{t("rb.result")}</div><pre className="mono mt-1 max-h-72 overflow-auto rounded bg-sunken p-2 text-[12px]">{JSON.stringify(st.result, null, 2)}</pre></>}
                      </Details>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </Card>
        <Card className="self-start">
          <CardHeader title={t("rb.run_info")} />
          <CardBody className="space-y-3 text-sm">
            <Info label={t("rb.col_status")}><RunStatusBadge status={r.status} /></Info>
            <Info label={t("rb.version")}>v{r.runbook_version}</Info>
            {r.finished_at && <Info label={t("rb.finished")}>{formatDate(r.finished_at, i18n.language)}</Info>}
            {r.params && Object.keys(r.params).length > 0 && (
              <Info label={t("rb.params")}>
                <dl className="space-y-1">
                  {Object.entries(r.params).map(([k, v]) => <div key={k}><dt className="mono inline text-muted">{k}:</dt> <dd className="inline">{argText(v)}</dd></div>)}
                </dl>
              </Info>
            )}
            <p className="text-xs text-muted">{t("rb.audit_hint")}</p>
          </CardBody>
        </Card>
      </div>
      <ApprovalDialog id={approval} onClose={() => { setApproval(null); invalidate(); }} />
      <Dialog open={confirmCancel} onOpenChange={setConfirmCancel} title={t("rb.cancel_title")} description={t("rb.cancel_text")}
        footer={<><Button onClick={() => setConfirmCancel(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={act.isPending} onClick={() => act.mutate({ path: "cancel" })}><Square />{t("rb.cancel_run")}</Button></>}>
        <p className="text-sm">{r.runbook_name}</p>
      </Dialog>
    </div>
  );
}

/** Preview of a step's text before it runs: parameters and the customer filled in. */
function fill(text: string, r: Run): string {
  return text.replace(/\{\{\s*([a-z_]+)\.([a-z0-9_]+)\s*\}\}/gi, (m, scope: string, key: string) => {
    if (scope === "params" && r.params && key in r.params) return argText(r.params[key]);
    if (scope === "customer" && key === "name" && r.customer_name) return r.customer_name;
    return m;
  });
}

function Details({ label, children }: { label: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2">
      <button type="button" className="inline-flex cursor-pointer items-center gap-1 text-xs font-semibold text-accent-text" onClick={() => setOpen(!open)}>
        <ChevronDown className={cn("size-3.5 transition-transform", !open && "-rotate-90")} />{label}
      </button>
      {open && <div className="mt-1">{children}</div>}
    </div>
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
