import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpCircle, Bot, Cpu, ExternalLink, FileText, HardDrive, HeartPulse, History, KeyRound, Mail, Package, Play, Power, RefreshCw, ScrollText, Server, Terminal, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { JobBadge, JobView, Output, useJobLabel } from "@/components/jobs";
import { GovernorSelect, NodeMetricsBars, PairDialog, StatusDot, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, patch, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Diagnosis, ExecResult, InstalledPackage, NodeInfo, NodeJob } from "@/lib/types";
import { ClaudeCard, OsBadge } from "@/pages/Servers";
import { formatBytes, formatDate, formatDuration, timeAgo } from "@/lib/utils";

const tabCls = "rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer";

function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-2 text-sm">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 text-right font-medium">{children}</span>
    </div>
  );
}

function useOp(nodeId: string) {
  return useMutation({ mutationFn: ({ action, params }: { action: string; params: Record<string, unknown> }) => post<unknown>(`api/nodes/${nodeId}/ops`, { action, params }) });
}

/* ------------------------------------------------------------------ jobs */
function JobsTab({ n }: { n: NodeInfo }) {
  const { t, i18n } = useTranslation();
  const label = useJobLabel();
  const jobs = useQuery({ queryKey: ["node-jobs", n.id], queryFn: () => get<NodeJob[]>(`api/nodes/${n.id}/jobs?limit=50`), refetchInterval: 10_000 });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Card>
      <CardHeader icon={<History />} title={t("px.jobs_title")} description={t("px.jobs_desc")} />
      {!jobs.data?.length ? <EmptyState icon={<History />} title={t("px.jobs_empty")} /> : (
        <Table>
          <thead><tr><Th>{t("px.job")}</Th><Th>{t("px.status")}</Th><Th>{t("px.by")}</Th><Th>{t("px.started")}</Th><Th /></tr></thead>
          <tbody>
            {jobs.data.map((j) => (
              <Tr key={j.id}>
                <Td className="font-semibold">{label(j.kind)}</Td>
                <Td><JobBadge status={j.status} /></Td>
                <Td>{j.requested_by}{j.source === "mcp" && <Badge tone="info" className="ml-2"><Bot className="size-3" /> MCP</Badge>}</Td>
                <Td className="text-muted">{timeAgo(j.started_at, i18n.language)}</Td>
                <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => setOpen(j.id)}>{t("px.show_output")}</Button></Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      <Dialog open={!!open} onOpenChange={(v) => !v && setOpen(null)} wide title={t("px.job_output")}>{open && <JobView jobId={open} />}</Dialog>
    </Card>
  );
}

/* ------------------------------------------------------------------ updates */
function UpdatesTab({ n, admin }: { n: NodeInfo; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<"install" | "reboot" | null>(null);
  const [typed, setTyped] = useState("");
  const [job, setJob] = useState<string | null>(null);
  const refresh = useMutation({
    mutationFn: () => post(`api/nodes/${n.id}/updates/refresh`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["node", n.id] }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const install = useMutation({
    mutationFn: () => post<NodeJob>(`api/nodes/${n.id}/updates/apply`),
    onSuccess: (j) => { setConfirm(null); setJob(j.id); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const reboot = useMutation({
    mutationFn: () => post(`api/nodes/${n.id}/reboot`, { confirm: typed }),
    onSuccess: () => { setConfirm(null); toast.success(t("px.rebooting")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const pkgs = n.updates.packages ?? [];
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          icon={<ArrowUpCircle />}
          title={n.updates.count ? t("px.updates_count", { count: n.updates.count }) : t("px.up_to_date")}
          description={`${t("px.checked")} ${timeAgo(n.updates.checked_at, i18n.language)}`}
          action={admin && (
            <>
              <Button size="sm" onClick={() => refresh.mutate()} loading={refresh.isPending} disabled={!n.online}>{!refresh.isPending && <RefreshCw />} {t("px.check")}</Button>
              {n.updates.reboot_required && <Button size="sm" onClick={() => { setTyped(""); setConfirm("reboot"); }} disabled={!n.online}><Power /> {t("px.reboot")}</Button>}
              <Button size="sm" variant="primary" onClick={() => { setTyped(""); setConfirm("install"); }} disabled={!n.online || !n.updates.count}>{t("px.install_all")}</Button>
            </>
          )}
        />
        {n.updates.reboot_required && <div className="px-5 pt-4"><Alert tone="warning">{t("px.reboot_hint")}</Alert></div>}
        {n.updates.error && <div className="px-5 pt-4"><Alert tone="offline">{n.updates.error}</Alert></div>}
        {pkgs.length ? (
          <Table>
            <thead><tr><Th>{t("px.package")}</Th><Th>{t("px.installed")}</Th><Th>{t("px.new_version")}</Th><Th>{t("px.source")}</Th></tr></thead>
            <tbody>{pkgs.map((p) => <Tr key={p.name}><Td className="mono text-[12.5px] font-semibold">{p.name}</Td><Td className="mono text-[12.5px] text-muted">{p.current}</Td><Td className="mono text-[12.5px]">{p.new}</Td><Td className="text-xs text-muted">{p.suite}</Td></Tr>)}</tbody>
          </Table>
        ) : <CardBody className="text-sm text-muted">{t("px.no_updates")}</CardBody>}
      </Card>
      {job && <Card><CardHeader icon={<Terminal />} title={t("px.job_output")} /><CardBody><JobView jobId={job} /></CardBody></Card>}
      <Dialog open={confirm !== null} onOpenChange={(v) => !v && setConfirm(null)} title={confirm === "reboot" ? t("px.reboot_title", { node: n.name }) : t("px.install_title", { node: n.name })}
        footer={<><Button onClick={() => setConfirm(null)}>{t("common.cancel")}</Button>
          <Button variant={confirm === "reboot" ? "danger" : "primary"} disabled={typed !== n.name} loading={install.isPending || reboot.isPending}
            onClick={() => (confirm === "reboot" ? reboot.mutate() : install.mutate())}>{confirm === "reboot" ? t("px.reboot") : t("px.install")}</Button></>}>
        <div className="space-y-3 text-sm">
          <p>{confirm === "reboot" ? t("px.reboot_text") : t("px.install_text", { count: n.updates.count })}</p>
          <Field label={t("px.type_name", { name: n.name })}><Input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus /></Field>
        </div>
      </Dialog>
    </div>
  );
}

/* ------------------------------------------------------------------ logs */
function LogsTab({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const op = useOp(n.id);
  const win = n.kind === "windows";
  const [f, setF] = useState({ unit: "", since: "1 hour ago", priority: "", grep: "", lines: 300, log: "" });
  const res = op.data as ExecResult | undefined;
  const run = () => op.mutate({ action: "logs.journal", params: f });
  return (
    <Card>
      <CardHeader icon={<ScrollText />} title={win ? t("srv.eventlog_title") : t("px.logs_title")} description={win ? t("srv.eventlog_desc") : t("px.logs_desc")} />
      <CardBody className="space-y-4">
        <form className={win ? "grid gap-3 sm:grid-cols-2 xl:grid-cols-6" : "grid gap-3 sm:grid-cols-2 xl:grid-cols-5"} onSubmit={(e) => { e.preventDefault(); run(); }}>
          {win && (
            <Field label={t("srv.event_log")}>
              <Select value={f.log} onChange={(e) => setF({ ...f, log: e.target.value })}>
                <option value="">System + Application</option>
                {["System", "Application", "Security", "Setup"].map((l) => <option key={l} value={l}>{l}</option>)}
              </Select>
            </Field>
          )}
          <Field label={win ? t("srv.provider") : t("px.unit")}><Input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} placeholder={win ? "Service Control Manager, MSExchange…" : n.kind === "proxmox" ? "pveproxy, nginx, …" : "nginx, postfix, …"} /></Field>
          <Field label={t("px.since")}><Input value={f.since} onChange={(e) => setF({ ...f, since: e.target.value })} placeholder="1 hour ago" /></Field>
          <Field label={t("px.priority")}>
            <Select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })}>
              <option value="">{t("px.all")}</option>
              {(win ? ["crit", "err", "warning", "info"] : ["emerg", "alert", "crit", "err", "warning", "notice", "info"]).map((p) => <option key={p} value={p}>{win ? t(`srv.level_${p}`) : p}</option>)}
            </Select>
          </Field>
          <Field label={t("px.grep")}><Input value={f.grep} onChange={(e) => setF({ ...f, grep: e.target.value })} placeholder="error|timeout" /></Field>
          <div className="flex items-end"><Button type="submit" variant="dark" loading={op.isPending} disabled={!n.online} className="w-full"><Play /> {t("px.show")}</Button></div>
        </form>
        {op.isError && <Alert tone="offline">{errorMessage(op.error, t)}</Alert>}
        {res && <Output text={res.stdout || res.stderr || t("px.no_lines")} />}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ services */
interface Svc { name: string; load: string; active: string; sub: string; description: string }

function ServicesTab({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const [state, setState] = useState("running");
  const [q, setQ] = useState("");
  const list = useQuery({
    queryKey: ["services", n.id, state],
    queryFn: () => post<Svc[]>(`api/nodes/${n.id}/ops`, { action: "services.list", params: { state: state === "all" ? "" : state } }),
    enabled: n.online,
  });
  const status = useOp(n.id);
  const action = useOp(n.id);
  const [open, setOpen] = useState<string | null>(null);
  const doAction = (unit: string, a: string) =>
    action.mutate({ action: "services.action", params: { unit, action: a } }, {
      onSuccess: (r) => {
        const res = r as ExecResult;
        if (res.exit_code === 0) toast.success(t("px.svc_done", { unit, action: a }));
        else toast.error(res.stderr || `exit ${res.exit_code}`);
        list.refetch();
        status.mutate({ action: "services.status", params: { unit } });
      },
      onError: (e) => toast.error(errorMessage(e, t)),
    });
  const rows = (list.data ?? []).filter((s) => !q || s.name.includes(q) || s.description.toLowerCase().includes(q.toLowerCase()));
  const st = status.data as { status: string; properties: Record<string, string> } | undefined;
  return (
    <Card>
      <CardHeader icon={<Server />} title={t("px.services_title")} description={t("px.services_desc")}
        action={<><Select value={state} onChange={(e) => setState(e.target.value)} className="h-8 w-auto py-0 text-[13px]"><option value="running">{t("px.svc_running")}</option><option value="failed">{t("px.svc_failed")}</option><option value="all">{t("px.all")}</option></Select>
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("px.filter")} className="h-8 w-40" /></>} />
      {list.isError && <CardBody><Alert tone="offline">{errorMessage(list.error, t)}</Alert></CardBody>}
      {list.isLoading ? <CardBody><Spinner /></CardBody> : (
        <Table>
          <thead><tr><Th>{t("px.service")}</Th><Th>{t("px.status")}</Th><Th /></tr></thead>
          <tbody>
            {rows.map((s) => (
              <Tr key={s.name}>
                <Td><div className="mono text-[12.5px] font-semibold">{s.name}</div><div className="text-xs text-muted">{s.description}</div></Td>
                <Td><Badge tone={s.active === "active" ? "online" : s.active === "failed" ? "offline" : "neutral"} dot>{s.active} · {s.sub}</Badge></Td>
                <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => { setOpen(s.name); status.mutate({ action: "services.status", params: { unit: s.name } }); }}>{t("px.details")}</Button></Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      <Dialog open={!!open} onOpenChange={(v) => !v && setOpen(null)} wide title={open ?? ""}
        footer={open && ["restart", "start", "stop"].map((a) => <Button key={a} variant={a === "stop" ? "danger" : a === "restart" ? "dark" : "secondary"} loading={action.isPending && action.variables?.params.action === a} onClick={() => doAction(open, a)}>{t(`px.svc_${a}`)}</Button>)}>
        {status.isPending ? <Spinner /> : st ? <Output text={st.status} /> : status.isError ? <Alert tone="offline">{errorMessage(status.error, t)}</Alert> : null}
      </Dialog>
    </Card>
  );
}

/* ------------------------------------------------------------------ command + files */
function CommandTab({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const exec = useOp(n.id);
  const file = useOp(n.id);
  const [cmd, setCmd] = useState("");
  const win = n.kind === "windows";
  const [path, setPath] = useState(win ? "C:\\Windows\\System32\\drivers\\etc\\hosts" : n.kind === "proxmox" ? "/etc/pve/storage.cfg" : "/etc/os-release");
  const [history, setHistory] = useState<{ cmd: string; res: ExecResult }[]>([]);
  const run = () => {
    if (!cmd.trim()) return;
    const c = cmd;
    exec.mutate({ action: "exec", params: { command: c, timeout: 120 } }, { onSuccess: (r) => { setHistory((h) => [...h.slice(-19), { cmd: c, res: r as ExecResult }]); setCmd(""); } });
  };
  const fr = file.data as { content?: string; entries?: string[]; truncated?: boolean } | undefined;
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader icon={<Terminal />} title={win ? t("srv.ps_title") : t("px.cmd_title")} description={win ? t("srv.ps_desc") : t("px.cmd_desc")} />
        <CardBody className="space-y-3">
          <Alert tone="warning">{t("px.cmd_warning")}</Alert>
          <Output className="min-h-40" text={history.map((h) => `# ${h.cmd}\n${h.res.stdout}${h.res.stderr ? h.res.stderr : ""}${h.res.exit_code ? `[exit ${h.res.exit_code}]\n` : ""}${h.res.timed_out ? "[timed out]\n" : ""}`).join("\n")} />
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); run(); }}>
            <Input className="mono" value={cmd} onChange={(e) => setCmd(e.target.value)} placeholder={win ? "Get-Service | Where-Object Status -eq 'Stopped'" : n.kind === "proxmox" ? "pvesm status" : "df -h"} disabled={!n.online} />
            <Button type="submit" variant="dark" loading={exec.isPending} disabled={!n.online || !cmd.trim()}><Play /> {t("px.run")}</Button>
          </form>
          {exec.isError && <Alert tone="offline">{errorMessage(exec.error, t)}</Alert>}
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<FileText />} title={t("px.file_title")} description={t("px.file_desc")} />
        <CardBody className="space-y-3">
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); file.mutate({ action: "file.read", params: { path, tail: path.startsWith("/var/log") || /\.log$/i.test(path) } }); }}>
            <Input className="mono" value={path} onChange={(e) => setPath(e.target.value)} />
            <Button type="submit" loading={file.isPending} disabled={!n.online}>{t("px.show")}</Button>
          </form>
          {file.isError && <Alert tone="offline">{errorMessage(file.error, t)}</Alert>}
          {fr && <Output text={fr.entries ? fr.entries.join("\n") : fr.content ?? ""} />}
        </CardBody>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ health (diagnose) */
function HealthTab({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["diagnose", n.id], queryFn: () => get<Diagnosis>(`api/servers/${n.id}/diagnose`), enabled: n.online, staleTime: 30_000, retry: false });
  const d = q.data;
  const procs = (rows: Diagnosis["top_cpu"], title: string) => (
    <Card>
      <CardHeader title={title} />
      <Table>
        <thead><tr><Th>{t("srv.process")}</Th><Th>PID</Th><Th className="text-right">CPU</Th><Th className="text-right">{t("srv.memory")}</Th></tr></thead>
        <tbody>{(rows ?? []).map((p) => <Tr key={`${title}-${p.pid}`}><Td className="mono text-[12.5px] font-semibold">{p.name}{p.user && <span className="ml-2 text-xs font-normal text-muted">{p.user}</span>}</Td><Td className="text-muted">{p.pid}</Td><Td className="text-right">{p.cpu.toFixed(1)}%</Td><Td className="text-right">{formatBytes(p.mem_bytes)}</Td></Tr>)}</tbody>
      </Table>
    </Card>
  );
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader icon={<HeartPulse />} title={t("srv.health_title")} description={t("srv.health_desc")}
          action={<Button size="sm" onClick={() => q.refetch()} loading={q.isFetching} disabled={!n.online}>{!q.isFetching && <RefreshCw />} {t("srv.health_run")}</Button>} />
        <CardBody className="space-y-3">
          {q.isError && <Alert tone="offline">{errorMessage(q.error, t)}</Alert>}
          {q.isLoading && <Spinner />}
          {d && (d.findings.length ? (
            <ul className="space-y-2">{d.findings.map((f) => <li key={f}><Alert tone="warning">{f}</Alert></li>)}</ul>
          ) : <Alert tone="online">{t("srv.health_ok")}</Alert>)}
          {d?.time_sync && <p className="text-sm text-muted">{t("srv.time_sync")}: <span className="text-ink">{d.time_sync}</span></p>}
        </CardBody>
      </Card>
      {d && !!d.failed_services?.length && (
        <Card>
          <CardHeader icon={<Server />} title={t("srv.failed_services")} />
          <Table>
            <tbody>{d.failed_services.map((s) => <Tr key={s.name}><Td><div className="mono text-[12.5px] font-semibold">{s.name}</div><div className="text-xs text-muted">{s.description}</div></Td><Td><Badge tone="offline" dot>{s.active} · {s.sub}</Badge></Td></Tr>)}</tbody>
          </Table>
        </Card>
      )}
      {d && <div className="grid gap-6 xl:grid-cols-2">{procs(d.top_cpu, t("srv.top_cpu"))}{procs(d.top_memory, t("srv.top_memory"))}</div>}
      {d?.recent_errors && <Card><CardHeader icon={<ScrollText />} title={t("srv.recent_errors")} /><CardBody><Output text={d.recent_errors} /></CardBody></Card>}
      {d && !!d.listening?.length && (
        <Card>
          <CardHeader title={t("srv.listening")} />
          <Table>
            <thead><tr><Th>{t("srv.proto")}</Th><Th>{t("srv.address")}</Th><Th>{t("srv.process")}</Th></tr></thead>
            <tbody>{d.listening.map((l, i) => <Tr key={i}><Td className="text-xs uppercase text-muted">{l.proto}</Td><Td className="mono text-[12.5px]">{l.address}</Td><Td className="mono text-[12.5px]">{l.process ?? "–"}</Td></Tr>)}</tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ installed software */
function SoftwareTab({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const op = useOp(n.id);
  const [pattern, setPattern] = useState("");
  const rows = (op.data as InstalledPackage[] | undefined) ?? null;
  return (
    <Card>
      <CardHeader icon={<Package />} title={t("srv.sw_installed")} description={t("srv.sw_installed_desc")} />
      <CardBody>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); op.mutate({ action: "packages.query", params: { pattern: pattern.trim().toLowerCase() || "*" } }); }}>
          <Input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder={n.kind === "windows" ? "*sql*, KB5*" : "nginx*, openssl"} />
          <Button type="submit" variant="dark" loading={op.isPending} disabled={!n.online}>{t("px.show")}</Button>
        </form>
        {op.isError && <Alert tone="offline" className="mt-3">{errorMessage(op.error, t)}</Alert>}
      </CardBody>
      {rows && (rows.length ? (
        <Table>
          <thead><tr><Th>{t("px.package")}</Th><Th>{t("px.version")}</Th><Th>{t("srv.vendor")}</Th></tr></thead>
          <tbody>{rows.slice(0, 500).map((p) => <Tr key={`${p.name}-${p.version}-${p.arch ?? ""}`}><Td className="mono text-[12.5px] font-semibold">{p.name}</Td><Td className="mono text-[12.5px]">{p.version}</Td><Td className="text-xs text-muted">{p.vendor ?? p.source ?? p.arch ?? ""}</Td></Tr>)}</tbody>
        </Table>
      ) : <CardBody className="text-sm text-muted">{t("srv.sw_none")}</CardBody>)}
    </Card>
  );
}

/* ------------------------------------------------------------------ overview tab */
function OverviewTab({ n, admin, section }: { n: NodeInfo; admin: boolean; section: Section }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const envs = useEnvironments();
  const [pair, setPair] = useState(false);
  const [rename, setRename] = useState(n.name);
  const [remove, setRemove] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  useEffect(() => setRename(n.name), [n.name]);
  const save = useMutation({
    mutationFn: (body: Record<string, string>) => patch(`api/nodes/${n.id}`, body),
    onSuccess: () => { toast.success(t("common.saved")); qc.invalidateQueries({ queryKey: ["node", n.id] }); qc.invalidateQueries({ queryKey: ["overview"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const agentUpdate = useMutation({
    mutationFn: () => post<NodeJob>(`api/nodes/${n.id}/agent/update`),
    onSuccess: (j) => setJob(j.id),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const testMail = useMutation({
    mutationFn: () => post<{ ok?: boolean; error?: string }>(`api/nodes/${n.id}/notifications/test`),
    onSuccess: (r) => (r.ok === false ? toast.error(t("px.test_failed"), { description: r.error }) : toast.success(t("px.test_sent"))),
  });
  const pushMail = useMutation({
    mutationFn: () => post<{ ok: boolean; error?: string }>(`api/nodes/${n.id}/notifications/push`),
    onSuccess: (r) => { r.ok ? toast.success(t("px.notify_applied")) : toast.error(r.error); qc.invalidateQueries({ queryKey: ["node", n.id] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const del_ = useMutation({ mutationFn: () => del(`api/nodes/${n.id}`), onSuccess: () => { qc.invalidateQueries({ queryKey: ["overview"] }); navigate(section === "servers" ? "/servers" : "/proxmox/nodes"); } });
  const m = n.metrics || {};
  const cl = n.info?.cluster;
  return (
    <div className="grid gap-6 xl:grid-cols-3">
      <div className="space-y-6 xl:col-span-2">
        <Card>
          <CardHeader icon={<Cpu />} title={t("px.resources")} description={m.at ? `${t("px.updated")} ${timeAgo(m.at, i18n.language)}` : undefined} />
          <CardBody className="grid gap-6 md:grid-cols-2">
            <NodeMetricsBars n={n} />
            <div className="divide-y divide-line">
              <Row label={t("px.load")}>{m.load?.map((l) => l.toFixed(2)).join(" · ") ?? "–"}</Row>
              <Row label={t("px.uptime")}>{m.uptime_seconds ? formatDuration(m.uptime_seconds) : "–"}</Row>
              {n.kind === "proxmox" ? <Row label={t("px.guests")}>{m.guests ? `${m.guests.vms_running}/${m.guests.vms_total} VM · ${m.guests.cts_running}/${m.guests.cts_total} CT` : "–"}</Row>
                : <Row label={t("srv.processes")}>{m.processes ?? "–"}</Row>}
              {m.failed_services != null && <Row label={t("srv.failed_services")}>{m.failed_services ? <Badge tone="offline">{m.failed_services}</Badge> : "0"}</Row>}
              {(n.kind === "proxmox" || !!m.cpu_mhz) && <Row label={t("px.cpu_freq")}>{m.cpu_mhz ? `${m.cpu_mhz.toFixed(0)} MHz` : "–"}</Row>}
            </div>
          </CardBody>
        </Card>
        {n.capabilities.includes("governor") && <Card>
          <CardHeader icon={<Cpu />} title={t("px.governor_title")} description={t("px.governor_desc")} action={<GovernorSelect n={n} disabled={!admin || !n.online} />} />
          <CardBody className="text-sm text-muted">
            {t("px.governor_current")}: <span className="mono text-ink">{n.governor?.current ?? "–"}</span>
            {n.governor?.driver && <> · {t("px.driver")}: <span className="mono text-ink">{n.governor.driver}</span></>}
            {n.governor?.desired && n.governor.desired !== n.governor.current && <> · <Badge tone="warning">{t("px.governor_pending", { g: n.governor.desired })}</Badge></>}
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {["performance", "schedutil", "ondemand", "powersave"].map((g) => <div key={g} className="rounded-md bg-sunken px-3 py-2 text-xs"><span className="font-semibold text-ink">{t(`px.gov_${g}`)}</span><br />{t(`px.gov_${g}_hint`)}</div>)}
            </div>
          </CardBody>
        </Card>}
        {!!m.disks?.length && (
          <Card>
            <CardHeader icon={<HardDrive />} title={t("srv.disks")} />
            <Table>
              <thead><tr><Th>{t("srv.mount")}</Th><Th>{t("srv.fs")}</Th><Th>{t("srv.used")}</Th><Th className="text-right">{t("srv.free")}</Th></tr></thead>
              <tbody>{m.disks.map((d) => {
                const pct = d.total ? (d.used / d.total) * 100 : 0;
                return <Tr key={d.path}><Td className="mono text-[12.5px] font-semibold">{d.path}</Td><Td className="text-xs text-muted">{d.fs}</Td>
                  <Td><Badge tone={pct >= 90 ? "offline" : pct >= 80 ? "warning" : "neutral"}>{pct.toFixed(0)}%</Badge> <span className="text-xs text-muted">{formatBytes(d.used)} / {formatBytes(d.total)}</span></Td>
                  <Td className="text-right text-sm">{formatBytes(d.total - d.used)}</Td></Tr>;
              })}</tbody>
            </Table>
          </Card>
        )}
        {cl && (cl.nodes?.length ?? 0) > 0 && (
          <Card>
            <CardHeader icon={<Server />} title={`${t("px.cluster")} ${cl.name ?? ""}`} description={cl.quorate ? t("px.quorate") : t("px.not_quorate")} />
            <CardBody className="flex flex-wrap gap-2">
              {cl.nodes?.map((c) => <Badge key={c.name} tone={c.online ? "online" : "offline"} dot>{c.name}{c.ip ? ` · ${c.ip}` : ""}</Badge>)}
            </CardBody>
          </Card>
        )}
        {job && <Card><CardHeader icon={<Terminal />} title={t("px.job_agent_update")} /><CardBody><JobView jobId={job} /></CardBody></Card>}
      </div>
      <div className="space-y-6">
        <Card>
          <CardHeader icon={<Server />} title={t("px.system")} />
          <CardBody className="divide-y divide-line">
            <Row label={t("px.hostname")}>{n.hostname ?? "–"}</Row>
            {n.kind === "proxmox" && <Row label="Proxmox VE">{n.info?.pve_version ?? "–"}</Row>}
            <Row label={t("px.os")}>{n.info?.os_pretty_name ?? n.os ?? "–"}</Row>
            {typeof n.info?.domain === "string" && n.info.domain && <Row label={t("srv.domain")}>{n.info.domain}</Row>}
            {n.kind !== "windows" ? <Row label={t("px.kernel")}><span className="mono text-[12.5px]">{n.info?.kernel ?? "–"}</span></Row>
              : typeof n.info?.os_build === "string" && <Row label="Build"><span className="mono text-[12.5px]">{n.info.os_build}</span></Row>}
            <Row label="CPU"><span className="text-xs">{n.info?.cpu_model ?? "–"}</span></Row>
            <Row label={t("px.ip")}>{n.remote_ip ?? "–"}</Row>
          </CardBody>
        </Card>
        <Card>
          <CardHeader icon={<KeyRound />} title={t("px.agent")} />
          <CardBody className="space-y-3">
            <div className="divide-y divide-line">
              <Row label={t("px.version")}><span className="mono">{n.agent_version ?? "–"}</span> {n.agent_update_available && <Badge tone="info">{t("px.agent_outdated")}</Badge>}</Row>
              <Row label={t("px.install_kind")}>{n.install_kind ?? "–"} · {n.arch}</Row>
              <Row label={t("px.connected")}>{n.online ? timeAgo(n.connected_at, i18n.language) : `${t("px.offline")} · ${timeAgo(n.last_seen_at, i18n.language)}`}</Row>
              <Row label={t("px.enrolled")}>{formatDate(n.enrolled_at, i18n.language)}</Row>
            </div>
            {admin && <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => agentUpdate.mutate()} loading={agentUpdate.isPending} disabled={!n.online}>{t("px.agent_update")}</Button>
              <Button size="sm" onClick={() => setPair(true)}>{t("px.repair")}</Button>
            </div>}
          </CardBody>
        </Card>
        {section === "servers" && <ClaudeCard server={n} />}
        {admin && n.capabilities.includes("pve-notifications") && (
          <Card>
            <CardHeader icon={<Mail />} title={t("px.notify_title")} />
            <CardBody className="space-y-3 text-sm">
              {n.notify.error ? <Alert tone="offline">{n.notify.error}</Alert> : n.notify.applied_at ? <p className="text-muted">{t("px.notify_applied_at", { when: timeAgo(n.notify.applied_at, i18n.language) })}</p> : <p className="text-muted">{t("px.notify_never")}</p>}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={() => pushMail.mutate()} loading={pushMail.isPending} disabled={!n.online}>{t("px.notify_push")}</Button>
                <Button size="sm" onClick={() => testMail.mutate()} loading={testMail.isPending} disabled={!n.online}>{t("px.test_send")}</Button>
              </div>
              <Link to={`/customers/${n.environment_id}`} className="block text-xs font-semibold text-accent-text hover:underline">{t("px.notify_env_link")} →</Link>
            </CardBody>
          </Card>
        )}
        {admin && (
          <Card>
            <CardHeader title={t("px.manage")} />
            <CardBody className="space-y-4">
              <Field label={t("px.name")}>
                <div className="flex gap-2"><Input value={rename} onChange={(e) => setRename(e.target.value)} /><Button onClick={() => save.mutate({ name: rename })} disabled={rename === n.name || !rename.trim()}>{t("common.save")}</Button></div>
              </Field>
              <Field label={t("px.environment")}>
                <Select value={n.environment_id} onChange={(e) => save.mutate({ environment_id: e.target.value })}>
                  {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
                </Select>
              </Field>
              {n.kind !== "proxmox" && (
                <Field label={t("cust.machine_role")} hint={t("cust.machine_role_hint")}>
                  <Segmented value={n.role ?? "server"} onChange={(v) => save.mutate({ role: v })} options={[{ value: "server", label: t("cust.role_server") }, { value: "workstation", label: t("cust.role_workstation") }]} />
                </Field>
              )}
              <Button variant="danger" size="sm" onClick={() => setRemove(true)}><Trash2 /> {section === "servers" ? t("srv.remove") : t("px.remove_node")}</Button>
            </CardBody>
          </Card>
        )}
      </div>
      <PairDialog open={pair} onOpenChange={setPair} environmentId={n.environment_id} />
      <Dialog open={remove} onOpenChange={setRemove} title={t("px.remove_title", { node: n.name })}
        footer={<><Button onClick={() => setRemove(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={del_.isPending} onClick={() => del_.mutate()}>{section === "servers" ? t("srv.remove") : t("px.remove_node")}</Button></>}>
        <p className="text-sm">{t("px.remove_text")}</p>
      </Dialog>
    </div>
  );
}

type Section = "proxmox" | "servers";

export function NodeDetailPage({ section = "proxmox" }: { section?: Section }) {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const admin = user?.role === "admin";
  const node = useQuery({ queryKey: ["node", id], queryFn: () => get<NodeInfo>(`api/nodes/${id}`), refetchInterval: 15_000 });
  if (node.isLoading) return <Spinner />;
  if (!node.data) return <Alert tone="offline">{t("px.not_found")}</Alert>;
  const n = node.data;
  const tab = params.get("tab") ?? "overview";
  const has = (c: string) => n.capabilities.includes(c);
  const win = n.kind === "windows";
  const servers = section === "servers";
  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3"><StatusDot online={n.online} /> {n.name}</span>}
        description={servers
          ? <span className="flex flex-wrap items-center gap-2"><Link to={n.role === "workstation" ? "/workstations" : "/servers"} className="hover:underline">{n.role === "workstation" ? t("nav.workstations") : t("nav.servers")}</Link> · <Link to={`/customers/${n.environment_id}`} className="hover:underline">{n.environment_name}</Link> · <OsBadge kind={n.kind} label={n.info?.os_pretty_name ?? n.os} /></span>
          : <><Link to={`/customers/${n.environment_id}`} className="hover:underline">{n.environment_name}</Link>{n.cluster_name ? ` · ${t("px.cluster")} ${n.cluster_name}` : ""}{n.info?.pve_version ? ` · Proxmox VE ${n.info.pve_version}` : ""}</>}
        actions={<>
          {has("guests") && <Button size="sm" onClick={() => navigate(`/proxmox/guests?node=${n.id}`)}><HardDrive /> {t("gx.title")}</Button>}
          {admin && has("terminal") && <>
            <Button size="sm" variant="dark" disabled={!n.online} onClick={() => navigate(`/console/node/${n.id}`)}><Terminal /> {win ? "PowerShell" : t("gx.shell")}</Button>
            <Button size="sm" variant="ghost" disabled={!n.online} title={t("gx.new_tab")} onClick={() => window.open(`console/node/${n.id}`, "_blank", "noopener")}><ExternalLink /></Button>
          </>}
        </>}
      />
      {!n.online && <Alert tone="warning" className="mb-4">{t("px.offline_hint")}</Alert>}
      <Tabs.Root value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="overview" className={tabCls}>{t("px.tab_overview")}</Tabs.Trigger>
          <Tabs.Trigger value="updates" className={tabCls}>{t("px.updates")}{n.updates.count > 0 && <span className="ml-1.5 rounded-full bg-koraal px-1.5 text-[10px] text-nacht">{n.updates.count}</span>}</Tabs.Trigger>
          {admin && has("logs") && <Tabs.Trigger value="logs" className={tabCls}>{t("px.tab_logs")}</Tabs.Trigger>}
          {admin && has("services") && <Tabs.Trigger value="services" className={tabCls}>{t("px.tab_services")}</Tabs.Trigger>}
          {admin && has("diagnose") && <Tabs.Trigger value="health" className={tabCls}>{t("srv.tab_health")}</Tabs.Trigger>}
          {admin && has("packages") && servers && <Tabs.Trigger value="software" className={tabCls}>{t("srv.tab_software")}</Tabs.Trigger>}
          {admin && has("exec") && <Tabs.Trigger value="command" className={tabCls}>{win ? "PowerShell" : t("px.tab_command")}</Tabs.Trigger>}
          <Tabs.Trigger value="jobs" className={tabCls}>{t("px.tab_jobs")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="overview"><OverviewTab n={n} admin={admin} section={section} /></Tabs.Content>
        <Tabs.Content value="updates"><UpdatesTab n={n} admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="logs"><LogsTab n={n} /></Tabs.Content>}
        {admin && <Tabs.Content value="services"><ServicesTab n={n} /></Tabs.Content>}
        {admin && <Tabs.Content value="health"><HealthTab n={n} /></Tabs.Content>}
        {admin && <Tabs.Content value="software"><SoftwareTab n={n} /></Tabs.Content>}
        {admin && <Tabs.Content value="command"><CommandTab n={n} /></Tabs.Content>}
        <Tabs.Content value="jobs"><JobsTab n={n} /></Tabs.Content>
      </Tabs.Root>
    </>
  );
}
