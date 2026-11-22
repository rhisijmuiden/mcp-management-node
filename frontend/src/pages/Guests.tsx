import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft, Camera, Cpu, ExternalLink, HardDrive, History, Network, Plus, RefreshCw, RotateCcw, Search, Settings2, Terminal, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { ConsoleView } from "@/components/console-lazy";
import { CreateGuestDialog } from "@/components/CreateGuestDialog";
import { GuestIcon, GuestStatus, MiniBar, PowerButtons, pct, useAllNodes } from "@/components/guests";
import { JobView } from "@/components/jobs";
import { EnvDot, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ConsoleSession, Guest, GuestDetail, NodeJob } from "@/lib/types";
import { formatBytes, formatDuration } from "@/lib/utils";

const tabCls = "rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer";

/* ================================================================== list */
export function GuestsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const envs = useEnvironments();
  const nodes = useAllNodes();
  const env = params.get("env") ?? "";
  const node = params.get("node") ?? "";
  const type = (params.get("type") ?? "") as "" | "qemu" | "lxc";
  const [q, setQ] = useState(params.get("q") ?? "");
  const [create, setCreate] = useState(params.get("create") === "1");
  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  };
  const qs = new URLSearchParams();
  if (env) qs.set("environment_id", env);
  if (node) qs.set("node_id", node);
  if (type) qs.set("type", type);
  const list = useQuery({
    queryKey: ["guests", env, node, type],
    queryFn: () => get<Guest[]>(`api/guests?${qs}`),
    refetchInterval: 20_000,
  });
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (list.data ?? []).filter((g) => !needle || String(g.vmid).includes(needle) || g.name.toLowerCase().includes(needle) || g.tags.some((x) => x.toLowerCase().includes(needle)));
  }, [list.data, q]);
  const refresh = useMutation({
    mutationFn: async () => {
      const ids = (nodes.data ?? []).filter((n) => n.online && (!node || n.id === node) && (!env || n.environment_id === env) && n.capabilities.includes("guests")).map((n) => n.id);
      await Promise.all(ids.map((id) => post(`api/nodes/${id}/guests/refresh`).catch(() => undefined)));
      await new Promise((r) => setTimeout(r, 1200));
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["guests"] }),
  });
  const running = rows.filter((g) => g.status === "running").length;
  const pveNodes = (nodes.data ?? []).filter((n) => !env || n.environment_id === env);

  return (
    <>
      <PageHeader title={t("gx.title")} description={t("gx.desc")}
        actions={<>
          <Button onClick={() => refresh.mutate()} loading={refresh.isPending}>{!refresh.isPending && <RefreshCw />} {t("px.refresh")}</Button>
          {admin && <Button variant="primary" onClick={() => setCreate(true)}><Plus /> {t("gx.create")}</Button>}
        </>} />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={(e) => { setQ(e.target.value); }} placeholder={t("gx.search")} className="h-9 w-64 pl-9" />
        </div>
        <Select value={env} onChange={(e) => { set("env", e.target.value); }} className="h-9 w-auto py-0 text-[13px]">
          <option value="">{t("px.all_envs")}</option>
          {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
        </Select>
        <Select value={node} onChange={(e) => set("node", e.target.value)} className="h-9 w-auto py-0 text-[13px]">
          <option value="">{t("gx.all_nodes")}</option>
          {pveNodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
        </Select>
        <Segmented value={type || "all"} onChange={(v) => set("type", v === "all" ? "" : v)}
          options={[{ value: "all", label: t("px.all") }, { value: "qemu", label: "VM" }, { value: "lxc", label: "CT" }]} />
        <div className="flex-1" />
        <span className="text-sm text-muted">{t("gx.count", { count: rows.length, running })}</span>
      </div>
      <Card>
        {list.isLoading ? <CardBody><Spinner /></CardBody> : !rows.length ? (
          <EmptyState icon={<HardDrive />} title={t("gx.empty")} action={admin && <Button variant="primary" onClick={() => setCreate(true)}><Plus /> {t("gx.create")}</Button>}>
            {t("gx.empty_hint")}
          </EmptyState>
        ) : (
          <Table>
            <thead><tr><Th>{t("gx.guest")}</Th><Th>{t("px.status")}</Th><Th>{t("gx.node")}</Th><Th>CPU</Th><Th>{t("gx.memory")}</Th><Th>{t("px.uptime")}</Th>{admin && <Th />}</tr></thead>
            <tbody>
              {rows.map((g) => (
                <Tr key={`${g.node_id}-${g.vmid}`} className="cursor-pointer" onClick={() => navigate(`/proxmox/guests/${g.node_id}/${g.vmid}`)}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <GuestIcon type={g.type} />
                      <div className="min-w-0">
                        <div className="font-semibold"><span className="mono text-muted">{g.vmid}</span> {g.name}</div>
                        <div className="flex flex-wrap gap-1 pt-0.5">
                          {g.template && <Badge tone="info">{t("gx.template")}</Badge>}
                          {g.community_script && <Badge tone="accent">community-script</Badge>}
                          {g.tags.filter((x) => x !== "community-script").slice(0, 4).map((x) => <Badge key={x}>{x}</Badge>)}
                        </div>
                      </div>
                    </div>
                  </Td>
                  <Td><GuestStatus g={g} /></Td>
                  <Td>
                    <div className="flex items-center gap-1.5 text-sm"><EnvDot color={envs.data?.find((e) => e.id === g.environment_id)?.color} /> {g.node}</div>
                    {g.pve_node && g.pve_node !== g.node && <div className="text-xs text-muted">{g.pve_node}</div>}
                  </Td>
                  <Td>{g.status === "running" ? <div className="flex items-center gap-2"><MiniBar value={Math.round(g.cpu * 100)} /><span className="text-xs text-muted">{Math.round(g.cpu * 100)}% · {g.maxcpu}c</span></div> : <span className="text-xs text-muted">{g.maxcpu} {t("gx.cores")}</span>}</Td>
                  <Td>{g.status === "running" ? <div className="flex items-center gap-2"><MiniBar value={pct(g.mem, g.maxmem)} /><span className="text-xs text-muted">{formatBytes(g.mem)} / {formatBytes(g.maxmem)}</span></div> : <span className="text-xs text-muted">{formatBytes(g.maxmem)}</span>}</Td>
                  <Td className="text-sm text-muted">{g.uptime ? formatDuration(g.uptime) : "–"}</Td>
                  {admin && <Td className="text-right"><PowerButtons g={g} compact /></Td>}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <p className="mt-3 text-xs text-muted">{t("gx.list_hint")}</p>
      {admin && <CreateGuestDialog open={create} onOpenChange={(v) => { setCreate(v); if (!v && params.get("create")) set("create", ""); }} environmentId={env || undefined} />}
    </>
  );
}

/* ================================================================== detail */
function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-2 text-sm">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 break-all text-right font-medium">{children}</span>
    </div>
  );
}

function JobCard({ job, onClose }: { job: string; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const done = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["guest"] });
    qc.invalidateQueries({ queryKey: ["guests"] });
  }, [qc]);
  return (
    <Card>
      <CardHeader icon={<History />} title={t("px.job_output")} action={<Button size="sm" variant="ghost" onClick={onClose}>{t("common.done")}</Button>} />
      <CardBody><JobView jobId={job} onDone={done} /></CardBody>
    </Card>
  );
}

function ResourcesCard({ g, onSaved }: { g: GuestDetail; onSaved: () => void }) {
  const { t } = useTranslation();
  const c = g.config ?? {};
  const [f, setF] = useState({ cores: String(c.cores ?? g.maxcpu ?? 1), memory: String(c.memory ?? Math.round(g.maxmem / 1048576)), onboot: String(c.onboot ?? "0") === "1", tags: g.tags.join(", "), description: String(c.description ?? "") });
  const save = useMutation({
    mutationFn: () => put(`api/guests/${g.node_id}/${g.vmid}/resources`, {
      cores: Number(f.cores), memory: Number(f.memory), onboot: f.onboot,
      tags: f.tags.split(/[,; ]+/).map((x) => x.trim()).filter(Boolean), description: f.description,
    }),
    onSuccess: () => { toast.success(t("common.saved")); onSaved(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Card>
      <CardHeader icon={<Settings2 />} title={t("gx.resources_edit")} description={t("gx.resources_hint")} />
      <CardBody className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("gx.cores_label")}><Input type="number" min={1} value={f.cores} onChange={(e) => setF({ ...f, cores: e.target.value })} /></Field>
          <Field label={t("gx.memory_mb")}><Input type="number" min={64} step={256} value={f.memory} onChange={(e) => setF({ ...f, memory: e.target.value })} /></Field>
        </div>
        <Field label={t("gx.tags")} hint={t("gx.tags_hint")}><Input value={f.tags} onChange={(e) => setF({ ...f, tags: e.target.value })} /></Field>
        <Field label={t("gx.notes")}><Textarea rows={3} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <Switch checked={f.onboot} onCheckedChange={(v) => setF({ ...f, onboot: v })} label={t("gx.onboot")} description={t("gx.onboot_hint")} />
        <Button variant="dark" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </CardBody>
    </Card>
  );
}

function SnapshotsTab({ g, admin, onJob }: { g: GuestDetail; admin: boolean; onJob: (j: NodeJob) => void }) {
  const { t, i18n } = useTranslation();
  const [create, setCreate] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [vmstate, setVmstate] = useState(false);
  const [rollback, setRollback] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [startAfter, setStartAfter] = useState(false);
  const base = `api/guests/${g.node_id}/${g.vmid}/snapshots`;
  const mk = useMutation({
    mutationFn: () => post<NodeJob>(base, { name, description: desc, vmstate }),
    onSuccess: (j) => { setCreate(false); onJob(j); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rb = useMutation({
    mutationFn: (n: string) => post<NodeJob>(`${base}/${encodeURIComponent(n)}/rollback`, { confirm: typed, start: startAfter }),
    onSuccess: (j) => { setRollback(null); onJob(j); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rm = useMutation({
    mutationFn: (n: string) => del<NodeJob>(`${base}/${encodeURIComponent(n)}`),
    onSuccess: (j) => onJob(j),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const openCreate = () => {
    const d = new Date();
    const p = (n: number) => String(n).padStart(2, "0");
    setName(`snap-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`);
    setDesc("");
    setVmstate(false);
    setCreate(true);
  };
  const snaps = [...g.snapshots].sort((a, b) => (b.snaptime ?? 0) - (a.snaptime ?? 0));
  return (
    <Card>
      <CardHeader icon={<Camera />} title={t("gx.snapshots")} description={t("gx.snapshots_desc")}
        action={admin && <Button size="sm" variant="dark" onClick={openCreate} disabled={!g.online || !!g.lock}><Camera /> {t("gx.snapshot_new")}</Button>} />
      {!snaps.length ? <EmptyState icon={<Camera />} title={t("gx.snapshots_empty")} /> : (
        <Table>
          <thead><tr><Th>{t("px.name")}</Th><Th>{t("gx.taken")}</Th><Th>{t("gx.description")}</Th>{admin && <Th />}</tr></thead>
          <tbody>
            {snaps.map((s) => (
              <Tr key={s.name}>
                <Td><span className="mono text-[12.5px] font-semibold">{s.name}</span>{s.vmstate && <Badge tone="info" className="ml-2">RAM</Badge>}</Td>
                <Td className="text-sm text-muted">{s.snaptime ? new Date(s.snaptime * 1000).toLocaleString(i18n.language === "nl" ? "nl-NL" : "en-GB") : "–"}</Td>
                <Td className="text-sm">{s.description || <span className="text-muted">–</span>}</Td>
                {admin && (
                  <Td className="whitespace-nowrap text-right">
                    <Button size="sm" variant="ghost" disabled={!!g.lock} onClick={() => { setTyped(""); setStartAfter(g.status === "running"); setRollback(s.name); }}><RotateCcw /> {t("gx.rollback")}</Button>
                    <Button size="sm" variant="ghost" disabled={!!g.lock} loading={rm.isPending && rm.variables === s.name} onClick={() => rm.mutate(s.name)}><Trash2 /></Button>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      <Dialog open={create} onOpenChange={setCreate} title={t("gx.snapshot_new")}
        footer={<><Button onClick={() => setCreate(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={mk.isPending} disabled={name.length < 2} onClick={() => mk.mutate()}>{t("gx.snapshot_take")}</Button></>}>
        <div className="space-y-4">
          <Field label={t("px.name")} hint={t("gx.snapshot_name_hint")}><Input className="mono" value={name} onChange={(e) => setName(e.target.value.replace(/[^A-Za-z0-9_-]/g, ""))} /></Field>
          <Field label={t("gx.description")}><Input value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
          {g.type === "qemu" && g.status === "running" && <Checkbox checked={vmstate} onCheckedChange={setVmstate} label={t("gx.snapshot_ram")} description={t("gx.snapshot_ram_hint")} />}
        </div>
      </Dialog>
      <Dialog open={!!rollback} onOpenChange={(v) => !v && setRollback(null)} title={t("gx.rollback_title", { name: rollback })}
        footer={<><Button onClick={() => setRollback(null)}>{t("common.cancel")}</Button><Button variant="danger" disabled={typed !== rollback} loading={rb.isPending} onClick={() => rollback && rb.mutate(rollback)}>{t("gx.rollback")}</Button></>}>
        <div className="space-y-4 text-sm">
          <Alert tone="warning">{t("gx.rollback_text")}</Alert>
          <Checkbox checked={startAfter} onCheckedChange={setStartAfter} label={t("gx.rollback_start")} />
          <Field label={t("px.type_name", { name: rollback })}><Input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus /></Field>
        </div>
      </Dialog>
    </Card>
  );
}

function MigrateDialog({ g, open, onOpenChange, onJob }: { g: GuestDetail; open: boolean; onOpenChange: (v: boolean) => void; onJob: (j: NodeJob) => void }) {
  const { t } = useTranslation();
  const nodes = useAllNodes();
  const self = nodes.data?.find((n) => n.id === g.node_id);
  const members = (self?.info?.cluster?.nodes ?? []).filter((m) => m.name !== (g.pve_node ?? self?.name));
  const [target, setTarget] = useState("");
  const [online, setOnline] = useState(true);
  useEffect(() => { if (open) setTarget(members.find((m) => m.online)?.name ?? ""); }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = useMutation({
    mutationFn: () => post<NodeJob>(`api/guests/${g.node_id}/${g.vmid}/migrate`, { target, online }),
    onSuccess: (j) => { onOpenChange(false); onJob(j); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("gx.migrate_title", { name: `${g.vmid} ${g.name}` })}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={go.isPending} disabled={!target} onClick={() => go.mutate()}><ArrowRightLeft /> {t("gx.migrate")}</Button></>}>
      {!g.cluster || !members.length ? <Alert tone="info">{t("gx.migrate_no_cluster")}</Alert> : (
        <div className="space-y-4 text-sm">
          <Field label={t("gx.migrate_target")}>
            <Select value={target} onChange={(e) => setTarget(e.target.value)}>
              {members.map((m) => <option key={m.name} value={m.name} disabled={!m.online}>{m.name}{m.online ? "" : ` (${t("px.offline")})`}</option>)}
            </Select>
          </Field>
          {g.status === "running" && (g.type === "qemu"
            ? <Checkbox checked={online} onCheckedChange={setOnline} label={t("gx.migrate_live")} description={t("gx.migrate_live_hint")} />
            : <Alert tone="warning">{t("gx.migrate_ct_restart")}</Alert>)}
          <p className="text-muted">{t("gx.migrate_hint")}</p>
        </div>
      )}
    </Dialog>
  );
}

function DestroyDialog({ g, open, onOpenChange, onJob }: { g: GuestDetail; open: boolean; onOpenChange: (v: boolean) => void; onJob: (j: NodeJob) => void }) {
  const { t } = useTranslation();
  const [typed, setTyped] = useState("");
  useEffect(() => { if (open) setTyped(""); }, [open]);
  const go = useMutation({
    mutationFn: () => post<NodeJob>(`api/guests/${g.node_id}/${g.vmid}/destroy`, { confirm: typed }),
    onSuccess: (j) => { onOpenChange(false); onJob(j); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("gx.destroy_title", { name: `${g.vmid} ${g.name}` })}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="danger" disabled={typed !== String(g.vmid) && typed !== g.name} loading={go.isPending} onClick={() => go.mutate()}><Trash2 /> {t("gx.destroy")}</Button></>}>
      <div className="space-y-4 text-sm">
        {g.status === "running" ? <Alert tone="warning">{t("gx.destroy_running")}</Alert> : <Alert tone="offline">{t("gx.destroy_text")}</Alert>}
        <Field label={t("gx.destroy_type", { vmid: g.vmid })}><Input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} disabled={g.status === "running"} /></Field>
      </div>
    </Dialog>
  );
}

function configRows(g: GuestDetail): [string, string][] {
  const c = g.config ?? {};
  const keys = g.type === "qemu"
    ? ["ostype", "machine", "bios", "cpu", "sockets", "cores", "memory", "balloon", "scsihw", "boot", "agent"]
    : ["hostname", "ostype", "arch", "cores", "memory", "swap", "unprivileged", "features", "nameserver"];
  const disks = Object.keys(c).filter((k) => /^(scsi|virtio|sata|ide|efidisk|tpmstate|rootfs|mp)\d*$/.test(k)).sort();
  const nets = Object.keys(c).filter((k) => /^net\d+$/.test(k)).sort();
  return [...keys, ...disks, ...nets].filter((k) => c[k] !== undefined && c[k] !== "").map((k) => [k, String(c[k])]);
}

function addresses(g: GuestDetail): string[] {
  const out: string[] = [];
  for (const i of g.interfaces ?? []) {
    if (i.name === "lo") continue;
    if (i.inet) out.push(`${i.name}: ${i.inet}`);
    for (const a of i["ip-addresses"] ?? []) if (!a["ip-address"].startsWith("127.") && !a["ip-address"].startsWith("fe80") && a["ip-address"] !== "::1") out.push(`${i.name}: ${a["ip-address"]}/${a.prefix}`);
  }
  return out;
}

export function GuestDetailPage() {
  const { t } = useTranslation();
  const { node = "", vmid = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [job, setJob] = useState<string | null>(null);
  const [migrate, setMigrate] = useState(false);
  const [destroy, setDestroy] = useState(false);
  const [ctMode, setCtMode] = useState<"lxc-console" | "lxc-shell">("lxc-console");
  const q = useQuery({ queryKey: ["guest", node, vmid], queryFn: () => get<GuestDetail>(`api/guests/${node}/${vmid}`), refetchInterval: 15_000 });
  const tab = params.get("tab") ?? "overview";
  const onJob = useCallback((j: NodeJob) => {
    setJob(j.id);
    if (j.kind === "guest.destroy" || j.kind === "guest.migrate") toast.info(t("gx.job_follow"));
  }, [t]);
  const openConsole = useCallback(
    (size: { cols: number; rows: number }) => post<ConsoleSession>(`api/guests/${node}/${vmid}/console`, { mode: q.data?.type === "lxc" ? ctMode : "vnc", ...size }),
    [node, vmid, ctMode, q.data?.type],
  );
  if (q.isLoading) return <Spinner />;
  if (!q.data) return (
    <div className="space-y-4">
      <Alert tone="offline">{t("gx.not_found")}</Alert>
      {job && <JobCard job={job} onClose={() => navigate("/proxmox/guests")} />}
      <Link to="/proxmox/guests"><Button>{t("common.back")}</Button></Link>
    </div>
  );
  const g = q.data;
  const running = g.status === "running";
  const ips = addresses(g);
  const consoleUrl = `console/guest/${g.node_id}/${g.vmid}`;
  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3"><GuestIcon type={g.type} className="size-6 text-ink" /> <span className="mono text-muted">{g.vmid}</span> {g.name}</span>}
        description={<span className="flex flex-wrap items-center gap-2"><GuestStatus g={g} /> <span>{g.type === "qemu" ? t("gx.vm") : t("gx.ct")}</span> · <Link to={`/proxmox/nodes/${g.node_id}`} className="hover:underline">{g.node}</Link>{g.environment ? ` · ${g.environment}` : ""}</span>}
        actions={admin && <>
          <PowerButtons g={g} onJob={onJob} />
          <Button size="sm" onClick={() => setMigrate(true)} disabled={!g.online || !!g.lock || g.template}><ArrowRightLeft /> {t("gx.migrate")}</Button>
          <Button size="sm" variant="ghost" onClick={() => setDestroy(true)} disabled={!g.online || !!g.lock} title={t("gx.destroy")}><Trash2 /></Button>
        </>}
      />
      {g.error && <Alert tone="warning" className="mb-4">{g.error}</Alert>}
      {job && <div className="mb-5"><JobCard job={job} onClose={() => setJob(null)} /></div>}
      <Tabs.Root value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="overview" className={tabCls}>{t("px.tab_overview")}</Tabs.Trigger>
          {admin && <Tabs.Trigger value="console" className={tabCls}>{t("gx.console")}</Tabs.Trigger>}
          <Tabs.Trigger value="snapshots" className={tabCls}>{t("gx.snapshots")}{g.snapshots.length > 0 && <span className="ml-1.5 rounded-full bg-line px-1.5 text-[10px]">{g.snapshots.length}</span>}</Tabs.Trigger>
          {admin && <Tabs.Trigger value="settings" className={tabCls}>{t("gx.settings")}</Tabs.Trigger>}
        </Tabs.List>
        <Tabs.Content value="overview">
          <div className="grid gap-6 xl:grid-cols-3">
            <div className="space-y-6 xl:col-span-2">
              <Card>
                <CardHeader icon={<Cpu />} title={t("px.resources")} />
                <CardBody className="grid gap-4 sm:grid-cols-3">
                  {[
                    { label: "CPU", value: running ? Math.round(g.cpu * 100) : 0, detail: `${g.maxcpu} ${t("gx.cores")}` },
                    { label: t("gx.memory"), value: running ? pct(g.mem, g.maxmem) : 0, detail: `${running ? formatBytes(g.mem) + " / " : ""}${formatBytes(g.maxmem)}` },
                    { label: t("gx.disk"), value: pct(g.disk, g.maxdisk), detail: g.disk ? `${formatBytes(g.disk)} / ${formatBytes(g.maxdisk)}` : formatBytes(g.maxdisk) },
                  ].map((m) => (
                    <div key={m.label} className="rounded-md bg-sunken p-4">
                      <div className="flex items-center justify-between text-xs font-semibold text-muted"><span>{m.label}</span><span>{m.value}%</span></div>
                      <MiniBar value={m.value} className="mt-2 w-full" />
                      <div className="mt-2 text-xs text-muted">{m.detail}</div>
                    </div>
                  ))}
                </CardBody>
              </Card>
              <Card>
                <CardHeader icon={<HardDrive />} title={t("gx.config")} />
                <CardBody className="divide-y divide-line">
                  {configRows(g).map(([k, v]) => <Row key={k} label={<span className="mono text-[12.5px]">{k}</span>}><span className="mono text-[12px]">{v}</span></Row>)}
                  {!g.config && <p className="text-sm text-muted">{t("gx.config_unavailable")}</p>}
                </CardBody>
              </Card>
            </div>
            <div className="space-y-6">
              <Card>
                <CardHeader icon={<Network />} title={t("gx.network")} />
                <CardBody className="divide-y divide-line">
                  <Row label={t("px.uptime")}>{g.uptime ? formatDuration(g.uptime) : "–"}</Row>
                  <Row label={t("gx.addresses")}>{ips.length ? <span className="mono text-[12px]">{ips.map((a) => <span key={a} className="block">{a}</span>)}</span> : <span className="text-muted">{running ? (g.type === "qemu" ? t("gx.no_agent_ips") : "–") : "–"}</span>}</Row>
                  <Row label={t("gx.traffic")}>{formatBytes(g.netin)} ↓ · {formatBytes(g.netout)} ↑</Row>
                  {g.hastate && <Row label="HA">{g.hastate}</Row>}
                  {g.tags.length > 0 && <Row label={t("gx.tags")}><span className="flex flex-wrap justify-end gap-1">{g.tags.map((x) => <Badge key={x}>{x}</Badge>)}</span></Row>}
                </CardBody>
              </Card>
              {admin && running && (
                <Card>
                  <CardHeader icon={<Terminal />} title={t("gx.console")} description={g.type === "qemu" ? t("gx.console_vnc") : t("gx.console_term")} />
                  <CardBody className="flex flex-wrap gap-2">
                    <Button variant="dark" size="sm" onClick={() => setParams({ tab: "console" }, { replace: true })}><Terminal /> {t("gx.console_open")}</Button>
                    <Button size="sm" onClick={() => window.open(consoleUrl, "_blank", "noopener")}><ExternalLink /> {t("gx.new_tab")}</Button>
                  </CardBody>
                </Card>
              )}
            </div>
          </div>
        </Tabs.Content>
        {admin && (
          <Tabs.Content value="console">
            {!running ? <Alert tone="warning">{t("gx.console_not_running")}</Alert> : tab === "console" && (
              <ConsoleView key={`${g.type}-${ctMode}`} open={openConsole} newTabUrl={consoleUrl} className="h-[calc(100vh-260px)] min-h-[420px]"
                toolbar={g.type === "lxc" && <Segmented value={ctMode} onChange={setCtMode} options={[{ value: "lxc-console", label: t("gx.mode_console") }, { value: "lxc-shell", label: t("gx.mode_shell") }]} />} />
            )}
          </Tabs.Content>
        )}
        <Tabs.Content value="snapshots"><SnapshotsTab g={g} admin={admin} onJob={onJob} /></Tabs.Content>
        {admin && <Tabs.Content value="settings"><div className="max-w-2xl"><ResourcesCard key={JSON.stringify(g.config ?? {})} g={g} onSaved={() => qc.invalidateQueries({ queryKey: ["guest", node, vmid] })} /></div></Tabs.Content>}
      </Tabs.Root>
      {admin && <MigrateDialog g={g} open={migrate} onOpenChange={setMigrate} onJob={onJob} />}
      {admin && <DestroyDialog g={g} open={destroy} onOpenChange={setDestroy} onJob={onJob} />}
    </>
  );
}
