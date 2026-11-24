import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, ChevronDown, ChevronRight, Clock, Eye, History, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { useAllMachines, useAllNodes } from "@/components/guests";
import { JobBadge, JobView, Output } from "@/components/jobs";
import { useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Guest, ScheduledTask, TaskKind, TaskOptions, TaskRun, TaskTarget, TaskTargets } from "@/lib/types";
import { formatDate, timeAgo } from "@/lib/utils";

const KINDS: TaskKind[] = ["ct_apt_upgrade", "ct_community_update", "node_updates", "template_sync", "custom_command"];
const CRON_PRESETS = ["0 3 * * *", "30 2 * * *", "0 4 * * 0", "0 5 1 * *", "0 */6 * * *"];

const guestKind = (k: TaskKind, o: TaskOptions) => k === "ct_apt_upgrade" || k === "ct_community_update" || (k === "custom_command" && o.run_on === "ct");

interface Draft {
  id?: string;
  name: string;
  kind: TaskKind;
  enabled: boolean;
  cron: string;
  targets: TaskTargets;
  options: TaskOptions;
  mail_to: string;
  mail_on: "always" | "failure" | "never";
}

const newDraft = (): Draft => ({
  name: "", kind: "ct_apt_upgrade", enabled: true, cron: "0 3 * * *",
  targets: { scope: "all", environment_ids: [], node_ids: [], guests: [], tag: null, exclude_vmids: [] },
  options: { snapshot: true, rollback: true, keep_snapshots: 3, command: null, run_on: "ct", timeout: 3600, parallel_nodes: 4 },
  mail_to: "", mail_on: "always",
});

const toBody = (d: Draft) => ({
  name: d.name, kind: d.kind, enabled: d.enabled, cron: d.cron, targets: d.targets, options: d.options, mail_on: d.mail_on,
  mail_to: d.mail_to.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
});

function TaskDialog({ draft, onClose, timezone }: { draft: Draft; onClose: () => void; timezone: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const envs = useEnvironments();
  const [d, setD] = useState<Draft>(draft);
  const pveNodes = useAllNodes();
  // A custom command on machines also runs on Linux and Windows servers and workstations.
  const anyMachine = d.kind === "custom_command" && d.options.run_on === "node";
  const machines = useAllMachines(anyMachine);
  const nodes = anyMachine ? machines : pveNodes;
  const set = (p: Partial<Draft>) => setD((o) => ({ ...o, ...p }));
  const setT = (p: Partial<TaskTargets>) => setD((o) => ({ ...o, targets: { ...o.targets, ...p } }));
  const setO = (p: Partial<TaskOptions>) => setD((o) => ({ ...o, options: { ...o.options, ...p } }));
  const onGuests = guestKind(d.kind, d.options);
  const guests = useQuery({ queryKey: ["guests", "", "", "lxc"], queryFn: () => get<Guest[]>("api/guests?type=lxc"), enabled: onGuests && d.targets.scope === "guests" });
  const preview = useMutation({ mutationFn: () => post<TaskTarget[]>("api/tasks/preview", toBody(d)), onError: (e) => toast.error(errorMessage(e, t)) });
  const save = useMutation({
    mutationFn: () => {
      const body = toBody({ ...d, name: d.name.trim() || t(`tasks.kind_${d.kind}`) });
      return d.id ? put<ScheduledTask>(`api/tasks/${d.id}`, body) : post<ScheduledTask>("api/tasks", body);
    },
    onSuccess: () => { toast.success(t("common.saved")); qc.invalidateQueries({ queryKey: ["tasks"] }); onClose(); },
  });
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const hasGuest = (node_id: string, vmid: number) => d.targets.guests.some((g) => g.node_id === node_id && g.vmid === vmid);
  const scopes: TaskTargets["scope"][] = onGuests ? ["all", "environments", "nodes", "guests", "tag"] : ["all", "environments", "nodes"];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={d.id ? t("tasks.edit") : t("tasks.new")}
      footer={<>
        <Button onClick={() => preview.mutate()} loading={preview.isPending}><Eye /> {t("tasks.preview")}</Button>
        <div className="flex-1" />
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!d.cron || (d.kind === "custom_command" && !d.options.command && (d.options.run_on === "ct" || !d.options.command_windows))} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("px.name")}><Input value={d.name} onChange={(e) => set({ name: e.target.value })} placeholder={t(`tasks.kind_${d.kind}`)} autoFocus /></Field>
          <Field label={t("tasks.kind")}>
            <Select value={d.kind} onChange={(e) => { const k = e.target.value as TaskKind; set({ kind: k, targets: { ...d.targets, scope: ["guests", "tag"].includes(d.targets.scope) && !guestKind(k, d.options) ? "all" : d.targets.scope } }); }}>
              {KINDS.map((k) => <option key={k} value={k}>{t(`tasks.kind_${k}`)}</option>)}
            </Select>
          </Field>
        </div>
        <p className="-mt-2 text-xs text-muted">{t(`tasks.kind_${d.kind}_hint`)}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("tasks.schedule")} hint={t("tasks.cron_hint", { tz: timezone })}>
            <Input className="mono" value={d.cron} onChange={(e) => set({ cron: e.target.value })} />
          </Field>
          <Field label={t("tasks.presets")}>
            <Select value={CRON_PRESETS.includes(d.cron) ? d.cron : ""} onChange={(e) => e.target.value && set({ cron: e.target.value })}>
              <option value="">{t("tasks.custom")}</option>
              {CRON_PRESETS.map((c) => <option key={c} value={c}>{t(`tasks.preset_${c.replace(/[^0-9]+/g, "_").replace(/^_|_$/g, "")}`)}</option>)}
            </Select>
          </Field>
        </div>
        <Switch checked={d.enabled} onCheckedChange={(v) => set({ enabled: v })} label={t("tasks.enabled")} />

        {d.kind === "custom_command" && (
          <div className="space-y-3 rounded-md border border-line p-4">
            <Field label={t("tasks.run_on")}>
              <Select value={d.options.run_on} onChange={(e) => setO({ run_on: e.target.value as "ct" | "node" })}>
                <option value="ct">{t("tasks.run_on_ct")}</option><option value="node">{t("tasks.run_on_node")}</option>
              </Select>
            </Field>
            <Field label={d.options.run_on === "node" ? t("tasks.command_linux") : t("tasks.command")} hint={t("tasks.command_hint")}><Textarea rows={5} className="mono text-[12px]" value={d.options.command ?? ""} onChange={(e) => setO({ command: e.target.value })} placeholder={"apt-get -y autoremove\nsystemctl restart myapp"} /></Field>
            {d.options.run_on === "node" && (
              <Field label={t("tasks.command_windows")} hint={t("tasks.command_windows_hint")}>
                <Textarea rows={5} className="mono text-[12px]" value={d.options.command_windows ?? ""} onChange={(e) => setO({ command_windows: e.target.value })}
                  placeholder={"Get-NetAdapter -Physical | Where-Object Status -eq 'Disconnected' |\n  Restart-NetAdapter -Confirm:$false"} />
              </Field>
            )}
          </div>
        )}

        <div className="space-y-3 rounded-md border border-line p-4">
          <div className="text-sm font-bold">{t("tasks.targets")}</div>
          <Select value={d.targets.scope} onChange={(e) => setT({ scope: e.target.value as TaskTargets["scope"] })}>
            {scopes.map((s) => <option key={s} value={s}>{t(`tasks.scope_${s}${onGuests && s === "all" ? "_ct" : ""}`)}</option>)}
          </Select>
          {d.targets.scope === "environments" && (
            <div className="grid gap-2 sm:grid-cols-2">
              {envs.data?.map((e) => <Checkbox key={e.id} checked={d.targets.environment_ids.includes(e.id)} onCheckedChange={() => setT({ environment_ids: toggle(d.targets.environment_ids, e.id) })} label={e.name} />)}
            </div>
          )}
          {d.targets.scope === "nodes" && (
            <div className="grid gap-2 sm:grid-cols-3">
              {nodes.data?.filter((n) => n.capabilities.includes("guests") || !onGuests).map((n) => <Checkbox key={n.id} checked={d.targets.node_ids.includes(n.id)} onCheckedChange={() => setT({ node_ids: toggle(d.targets.node_ids, n.id) })} label={n.name} description={anyMachine ? `${n.environment_name ?? ""} · ${n.kind}` : n.environment_name} />)}
            </div>
          )}
          {d.targets.scope === "guests" && (guests.isLoading ? <Spinner /> : (
            <div className="grid max-h-64 gap-2 overflow-y-auto sm:grid-cols-2">
              {guests.data?.map((g) => (
                <Checkbox key={`${g.node_id}-${g.vmid}`} checked={hasGuest(g.node_id, g.vmid)}
                  onCheckedChange={() => setT({ guests: hasGuest(g.node_id, g.vmid) ? d.targets.guests.filter((x) => !(x.node_id === g.node_id && x.vmid === g.vmid)) : [...d.targets.guests, { node_id: g.node_id, vmid: g.vmid }] })}
                  label={`${g.vmid} ${g.name}`} description={g.node} />
              ))}
            </div>
          ))}
          {d.targets.scope === "tag" && <Field label={t("tasks.tag")}><Input value={d.targets.tag ?? ""} onChange={(e) => setT({ tag: e.target.value.trim() || null })} placeholder="community-script" /></Field>}
          {onGuests && d.targets.scope !== "guests" && (
            <Field label={t("tasks.exclude")} hint={t("tasks.exclude_hint")}>
              <Input className="mono" value={d.targets.exclude_vmids.join(", ")} onChange={(e) => setT({ exclude_vmids: e.target.value.split(/[,\s]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0) })} placeholder="105, 120" />
            </Field>
          )}
          {preview.data && (
            <div className="rounded-md bg-sunken p-3 text-sm">
              <div className="mb-1 font-semibold">{t("tasks.preview_count", { count: preview.data.length })}</div>
              <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">{preview.data.map((x) => <Badge key={x.label}>{x.label}</Badge>)}</div>
            </div>
          )}
        </div>

        {onGuests && (
          <div className="space-y-3 rounded-md border border-line p-4">
            <div className="text-sm font-bold">{t("tasks.safety")}</div>
            <Checkbox checked={d.options.snapshot} onCheckedChange={(v) => setO({ snapshot: v, rollback: v && d.options.rollback })} label={t("tasks.snapshot")} description={t("tasks.snapshot_hint")} />
            <Checkbox checked={d.options.rollback} disabled={!d.options.snapshot} onCheckedChange={(v) => setO({ rollback: v })} label={t("tasks.rollback")} description={t("tasks.rollback_hint")} />
            <Field label={t("tasks.keep")} className="max-w-48"><Input type="number" min={1} max={50} value={d.options.keep_snapshots} onChange={(e) => setO({ keep_snapshots: Number(e.target.value) || 1 })} /></Field>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("tasks.timeout")}><Input type="number" min={1} max={360} value={Math.round(d.options.timeout / 60)} onChange={(e) => setO({ timeout: Math.max(60, (Number(e.target.value) || 1) * 60) })} /></Field>
          <Field label={t("tasks.parallel")} hint={t("tasks.parallel_hint")}><Input type="number" min={1} max={50} value={d.options.parallel_nodes} onChange={(e) => setO({ parallel_nodes: Number(e.target.value) || 1 })} /></Field>
        </div>

        <div className="space-y-3 rounded-md border border-line p-4">
          <div className="text-sm font-bold">{t("tasks.report")}</div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label={t("tasks.mail_to")} hint={t("tasks.mail_to_hint")} className="sm:col-span-2"><Input value={d.mail_to} onChange={(e) => set({ mail_to: e.target.value })} placeholder="ops@example.nl" /></Field>
            <Field label={t("tasks.mail_on")}>
              <Select value={d.mail_on} onChange={(e) => set({ mail_on: e.target.value as Draft["mail_on"] })}>
                {(["always", "failure", "never"] as const).map((m) => <option key={m} value={m}>{t(`tasks.mail_${m}`)}</option>)}
              </Select>
            </Field>
          </div>
        </div>
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
      </div>
    </Dialog>
  );
}

function RunsTable({ runs, compact }: { runs: TaskRun[]; compact?: boolean }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  if (!runs.length) return <EmptyState icon={<History />} title={t("tasks.runs_empty")} />;
  return (
    <Table>
      <thead><tr><Th>{t("tasks.task")}</Th><Th>{t("px.status")}</Th><Th>{t("tasks.result")}</Th><Th>{t("px.started")}</Th>{!compact && <Th>{t("tasks.trigger")}</Th>}</tr></thead>
      <tbody>
        {runs.map((r) => (
          <Tr key={r.id} className="cursor-pointer" onClick={() => navigate(`/tasks/runs/${r.id}`)}>
            <Td className="font-semibold">{r.name}</Td>
            <Td><JobBadge status={r.status} /></Td>
            <Td className="text-sm"><span className="text-online">{r.counts.success} ✓</span>{r.counts.failed > 0 && <span className="ml-2 text-offline">{r.counts.failed} ✗</span>}{r.counts.skipped > 0 && <span className="ml-2 text-muted">{r.counts.skipped} {t("tasks.skipped")}</span>}</Td>
            <Td className="text-sm text-muted" title={formatDate(r.started_at, i18n.language)}>{timeAgo(r.started_at, i18n.language)}</Td>
            {!compact && <Td className="text-sm text-muted">{r.trigger === "schedule" ? t("tasks.trigger_schedule") : r.requested_by ?? r.trigger}</Td>}
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

export function TasksPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const qc = useQueryClient();
  const navigate = useNavigate();
  const list = useQuery({ queryKey: ["tasks"], queryFn: () => get<{ tasks: ScheduledTask[]; settings: { timezone?: string } }>("api/tasks"), refetchInterval: 30_000 });
  const runs = useQuery({ queryKey: ["task-runs"], queryFn: () => get<TaskRun[]>("api/task-runs?limit=30"), refetchInterval: 15_000 });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [remove, setRemove] = useState<ScheduledTask | null>(null);
  const [tzOpen, setTzOpen] = useState(false);
  const [tz, setTz] = useState("");
  const tzName = list.data?.settings?.timezone ?? "Europe/Amsterdam";
  const run = useMutation({
    mutationFn: (id: string) => post<{ run_id: string }>(`api/tasks/${id}/run`),
    onSuccess: (r) => { toast.success(t("tasks.started")); navigate(`/tasks/runs/${r.run_id}`); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rm = useMutation({ mutationFn: (id: string) => del(`api/tasks/${id}`), onSuccess: () => { setRemove(null); qc.invalidateQueries({ queryKey: ["tasks"] }); } });
  const toggle = useMutation({
    mutationFn: (x: ScheduledTask) => put(`api/tasks/${x.id}`, { ...x, enabled: !x.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const saveTz = useMutation({
    mutationFn: () => put("api/schedules-settings", { timezone: tz }),
    onSuccess: () => { setTzOpen(false); qc.invalidateQueries({ queryKey: ["tasks"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const zones = useMemo(() => { try { return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? []; } catch { return []; } }, []);
  const edit = (x: ScheduledTask) => setDraft({ id: x.id, name: x.name, kind: x.kind, enabled: x.enabled, cron: x.cron, targets: { ...newDraft().targets, ...x.targets }, options: { ...newDraft().options, ...x.options }, mail_to: x.mail_to.join(", "), mail_on: x.mail_on });
  return (
    <>
      <PageHeader title={t("tasks.title")} description={t("tasks.desc")}
        actions={admin && <>
          <Button onClick={() => { setTz(tzName); setTzOpen(true); }}><Clock /> {tzName}</Button>
          <Button variant="primary" onClick={() => setDraft(newDraft())}><Plus /> {t("tasks.new")}</Button>
        </>} />
      <Card className="mb-6">
        <CardHeader icon={<CalendarClock />} title={t("tasks.list")} />
        {list.isLoading ? <CardBody><Spinner /></CardBody> : !list.data?.tasks.length ? (
          <EmptyState icon={<CalendarClock />} title={t("tasks.empty")} action={admin && <Button variant="primary" onClick={() => setDraft(newDraft())}><Plus /> {t("tasks.new")}</Button>} />
        ) : (
          <Table>
            <thead><tr><Th>{t("tasks.task")}</Th><Th>{t("tasks.schedule")}</Th><Th>{t("tasks.last_run")}</Th><Th>{t("tasks.next_run")}</Th>{admin && <Th />}</tr></thead>
            <tbody>
              {list.data.tasks.map((x) => (
                <Tr key={x.id}>
                  <Td>
                    <div className="flex items-center gap-2 font-semibold">{x.name}{!x.enabled && <Badge>{t("tasks.paused")}</Badge>}</div>
                    <div className="text-xs text-muted">{t(`tasks.kind_${x.kind}`)} · {t(`tasks.scope_${x.targets.scope}`)}{x.options.snapshot && guestKind(x.kind, x.options) ? ` · ${t("tasks.with_snapshot")}` : ""}</div>
                  </Td>
                  <Td className="mono text-[12.5px]">{x.cron}</Td>
                  <Td>{x.last_status ? <span className="flex items-center gap-2"><JobBadge status={x.last_status} /><span className="text-xs text-muted">{timeAgo(x.last_run_at, i18n.language)}</span></span> : <span className="text-muted">–</span>}</Td>
                  <Td className="text-sm text-muted">{x.enabled && x.next_run_at ? formatDate(x.next_run_at, i18n.language) : "–"}</Td>
                  {admin && (
                    <Td className="whitespace-nowrap text-right">
                      <Button size="sm" variant="ghost" onClick={() => run.mutate(x.id)} loading={run.isPending && run.variables === x.id} title={t("tasks.run_now")}>{!(run.isPending && run.variables === x.id) && <Play />}</Button>
                      <Switch checked={x.enabled} onCheckedChange={() => toggle.mutate(x)} />
                      <Button size="sm" variant="ghost" onClick={() => edit(x)} title={t("common.edit")}><Pencil /></Button>
                      <Button size="sm" variant="ghost" onClick={() => setRemove(x)} title={t("common.delete")}><Trash2 /></Button>
                    </Td>
                  )}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Card>
        <CardHeader icon={<History />} title={t("tasks.history")} />
        {runs.isLoading ? <CardBody><Spinner /></CardBody> : <RunsTable runs={runs.data ?? []} />}
      </Card>
      {draft && <TaskDialog draft={draft} onClose={() => setDraft(null)} timezone={tzName} />}
      <Dialog open={!!remove} onOpenChange={(v) => !v && setRemove(null)} title={t("tasks.remove_title", { name: remove?.name })}
        footer={<><Button onClick={() => setRemove(null)}>{t("common.cancel")}</Button><Button variant="danger" loading={rm.isPending} onClick={() => remove && rm.mutate(remove.id)}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("tasks.remove_text")}</p>
      </Dialog>
      <Dialog open={tzOpen} onOpenChange={setTzOpen} title={t("tasks.timezone")}
        footer={<><Button onClick={() => setTzOpen(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={saveTz.isPending} onClick={() => saveTz.mutate()}>{t("common.save")}</Button></>}>
        <Field label={t("tasks.timezone")} hint={t("tasks.timezone_hint")}>
          {zones.length ? <Select value={tz} onChange={(e) => setTz(e.target.value)}>{zones.map((z) => <option key={z} value={z}>{z}</option>)}</Select> : <Input value={tz} onChange={(e) => setTz(e.target.value)} />}
        </Field>
      </Dialog>
    </>
  );
}

export function TaskRunPage() {
  const { t, i18n } = useTranslation();
  const { id = "" } = useParams();
  const run = useQuery({
    queryKey: ["task-run", id],
    queryFn: () => get<TaskRun>(`api/task-runs/${id}`),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 3000 : false),
  });
  const [open, setOpen] = useState<number | null>(null);
  if (run.isLoading) return <Spinner />;
  if (!run.data) return <Alert tone="offline">{t("tasks.run_not_found")}</Alert>;
  const r = run.data;
  return (
    <>
      <PageHeader title={<span className="flex items-center gap-3">{r.name} <JobBadge status={r.status} /></span>}
        description={<>{t(`tasks.kind_${r.kind}`, { defaultValue: r.kind })} · {formatDate(r.started_at, i18n.language)}{r.finished_at ? ` → ${formatDate(r.finished_at, i18n.language)}` : ""} · {r.trigger === "schedule" ? t("tasks.trigger_schedule") : r.requested_by}</>}
        actions={<Link to="/tasks"><Button>{t("common.back")}</Button></Link>} />
      <div className="mb-5 grid gap-3 sm:grid-cols-3">
        {(["success", "failed", "skipped"] as const).map((k) => (
          <div key={k} className="rounded-lg border border-line bg-raised p-4">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t(`tasks.count_${k}`)}</div>
            <div className={`display mt-1 text-2xl ${k === "failed" && r.counts.failed ? "text-offline" : ""}`}>{r.counts[k]}</div>
          </div>
        ))}
      </div>
      <Card>
        <CardHeader icon={<History />} title={t("tasks.results")} description={r.status === "running" ? t("tasks.running_hint") : undefined} />
        {!r.results.length ? <CardBody>{r.status === "running" ? <Spinner /> : <span className="text-sm text-muted">{t("tasks.no_targets")}</span>}</CardBody> : (
          <Table>
            <thead><tr><Th className="w-6" /><Th>{t("tasks.target")}</Th><Th>{t("px.status")}</Th><Th>{t("tasks.details")}</Th></tr></thead>
            <tbody>
              {r.results.map((x, i) => (
                <Fragment key={i}>
                  <Tr className="cursor-pointer" onClick={() => setOpen(open === i ? null : i)}>
                    <Td>{open === i ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}</Td>
                    <Td className="font-semibold">{x.target}</Td>
                    <Td>{x.status === "skipped" ? <Badge>{t("tasks.skipped")}</Badge> : <JobBadge status={x.status} />}</Td>
                    <Td className="text-sm">
                      {x.error && <span className="text-offline">{x.error}</span>}
                      {x.note && <span className="text-muted">{x.note}</span>}
                      {x.snapshot && <span className="ml-2 text-xs text-muted">{t("tasks.snapshot_name", { name: x.snapshot })}</span>}
                      {x.rolled_back && <Badge tone="warning" className="ml-2">{t("tasks.rolled_back")}</Badge>}
                    </Td>
                  </Tr>
                  {open === i && (
                    <tr><td colSpan={4} className="border-b border-line bg-sunken/50 p-4">
                      {x.job_id ? <JobView jobId={x.job_id} /> : <Output text={x.output || t("tasks.no_output")} />}
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
