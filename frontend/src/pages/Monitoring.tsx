import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, AlertTriangle, BellRing, ChevronDown, ChevronRight, Cpu, HardDrive, MemoryStick, RefreshCw, Settings2, ShieldCheck } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { pct } from "@/components/guests";
import { Sparkline } from "@/components/net";
import { StatusDot, UsageBar, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { HealthDisk, MetricPoint, MonAlert, MonitoringNode, MonitoringOverview, MonitoringSettings } from "@/lib/types";
import { cn, formatBytes, formatDate, formatDuration, timeAgo } from "@/lib/utils";

type Range = "1h" | "24h" | "7d" | "30d";
const COLORS = { cpu: "#3b6fd8", mem: "#b05bd6", disk: "#e0812b" };

export function MonitoringPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [range, setRange] = useState<Range>("24h");
  const [env, setEnv] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [settings, setSettings] = useState(false);
  const envs = useEnvironments();
  const q = useQuery({
    queryKey: ["monitoring", range, env],
    queryFn: () => get<MonitoringOverview>(`api/monitoring?range=${range}${env ? `&environment_id=${env}` : ""}`),
    refetchInterval: 60_000,
  });
  const alerts = useQuery({ queryKey: ["mon-alerts"], queryFn: () => get<MonAlert[]>("api/monitoring/alerts?limit=50"), refetchInterval: 60_000 });
  const d = q.data;
  const openAlerts = (alerts.data ?? []).filter((a) => !a.resolved_at);
  return (
    <div>
      <PageHeader title={t("mon.title")} description={t("mon.desc")}
        actions={<>
          <Segmented value={range} onChange={setRange} options={(["1h", "24h", "7d", "30d"] as Range[]).map((r) => ({ value: r, label: t(`mon.range_${r}`) }))} />
          {admin && <Button onClick={() => setSettings(true)}><Settings2 /> {t("mon.settings")}</Button>}
        </>} />

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Select value={env} onChange={(e) => setEnv(e.target.value)} className="w-auto min-w-48">
          <option value="">{t("mon.all_envs")}</option>
          {(envs.data ?? []).map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
        </Select>
        <div className="flex-1" />
        <Button size="sm" onClick={() => { q.refetch(); alerts.refetch(); }} loading={q.isFetching}><RefreshCw /> {t("common.refresh")}</Button>
      </div>

      {q.isLoading && <Spinner />}
      {q.isError && <Alert tone="offline">{errorMessage(q.error, t)}</Alert>}
      {d && (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label={t("mon.nodes_online")} value={`${d.totals.online} / ${d.totals.nodes}`} tone={d.totals.online < d.totals.nodes ? "warning" : "online"} icon={<Activity />} />
            <Stat label={t("mon.cpu_avg")} value={d.totals.cpu_avg == null ? "-" : `${Math.round(d.totals.cpu_avg)}%`} icon={<Cpu />} />
            <Stat label={t("mon.memory")} value={`${pct(d.totals.mem_used, d.totals.mem_total)}%`} hint={`${formatBytes(d.totals.mem_used)} / ${formatBytes(d.totals.mem_total)}`} icon={<MemoryStick />} />
            <Stat label={t("mon.alerts_open")} value={d.totals.alerts_open} tone={d.totals.alerts_open ? "offline" : "online"} icon={<BellRing />}
              hint={d.totals.storage_total ? t("mon.storage_hint", { used: formatBytes(d.totals.storage_used), total: formatBytes(d.totals.storage_total) }) : undefined} />
          </div>

          {openAlerts.length > 0 && (
            <Card>
              <CardHeader title={t("mon.open_alerts")} icon={<AlertTriangle />} />
              <CardBody className="space-y-2">
                {openAlerts.map((a) => (
                  <div key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
                    <Badge tone={a.severity === "critical" ? "offline" : "warning"}>{t(`mon.sev_${a.severity}`)}</Badge>
                    <span className="font-semibold">{a.node ?? "-"}</span>
                    <span>{a.message}</span>
                    <span className="text-xs text-muted">{t("mon.since", { ago: timeAgo(a.started_at, i18n.language) })}</span>
                  </div>
                ))}
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader title={t("mon.nodes")} description={t("mon.nodes_hint")} />
            {!d.nodes.length ? <EmptyState icon={<Activity />} title={t("mon.no_nodes")}>{t("mon.no_nodes_hint")}</EmptyState> : (
              <Table>
                <thead>
                  <tr><Th className="w-8" /><Th>{t("mon.node")}</Th><Th>CPU</Th><Th>{t("mon.memory")}</Th><Th>{t("mon.disk")}</Th><Th>{t("mon.disks")}</Th><Th>{t("mon.alerts")}</Th></tr>
                </thead>
                <tbody>
                  {d.nodes.map((n) => (
                    <Fragment key={n.node_id}>
                      <Tr className="cursor-pointer" onClick={() => setOpen(open === n.node_id ? null : n.node_id)}>
                        <Td>{open === n.node_id ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}</Td>
                        <Td>
                          <div className="flex items-center gap-2 font-semibold"><StatusDot online={n.online} /> <Link to={`/proxmox/nodes/${n.node_id}`} onClick={(e) => e.stopPropagation()} className="hover:underline">{n.name}</Link></div>
                          <div className="text-xs text-muted">{n.environment ?? "-"}{n.cluster ? ` · ${n.cluster}` : ""}{n.online ? "" : ` · ${t("mon.last_seen", { ago: n.last_seen_at ? timeAgo(n.last_seen_at, i18n.language) : "-" })}`}</div>
                        </Td>
                        <MetricCell now={n.cpu} values={n.trend.map((p) => p.cpu)} color={COLORS.cpu} label="CPU" />
                        <MetricCell now={pct(n.mem_used, n.mem_total)} values={n.trend.map((p) => p.mem_pct)} color={COLORS.mem} label={t("mon.memory")} />
                        <MetricCell now={n.disk_total ? pct(n.disk_used, n.disk_total) : null} values={n.trend.map((p) => p.disk_pct)} color={COLORS.disk} label={t("mon.disk")} />
                        <Td><DiskHealth n={n} /></Td>
                        <Td>{n.alerts ? <Badge tone="offline">{n.alerts}</Badge> : <span className="text-xs text-muted">-</span>}</Td>
                      </Tr>
                      {open === n.node_id && (
                        <tr><td colSpan={7} className="border-b border-line bg-sunken/40 p-0"><NodeDetail n={n} range={range} /></td></tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          <AlertHistory alerts={alerts.data ?? []} />
        </div>
      )}
      {settings && <SettingsDialog onClose={() => setSettings(false)} />}
    </div>
  );
}

function MetricCell({ now, values, color, label }: { now: number | null | undefined; values: (number | null)[]; color: string; label: string }) {
  return (
    <Td>
      <div className="flex items-center gap-3">
        <span className={cn("w-11 text-right text-sm font-bold tabular-nums", now != null && now >= 90 && "text-offline")}>{now == null ? "-" : `${Math.round(now)}%`}</span>
        <Sparkline values={values} color={color} width={110} height={30} label={label} />
      </div>
    </Td>
  );
}

function DiskHealth({ n }: { n: MonitoringNode }) {
  const { t } = useTranslation();
  if (!n.disks.length) return <span className="text-xs text-muted">{n.health_error ? t("mon.health_error") : "-"}</span>;
  const worn = n.disks.filter((d) => d.wearout != null && d.wearout <= 10).length;
  return n.disk_health === "failed" ? <Badge tone="offline">{t("mon.smart_failed")}</Badge>
    : worn ? <Badge tone="warning">{t("mon.wearout_low", { count: worn })}</Badge>
      : n.disk_health === "ok" ? <Badge tone="online"><ShieldCheck className="size-3.5" /> {t("mon.disks_ok", { count: n.disks.length })}</Badge>
        : <Badge>{t("mon.unknown")}</Badge>;
}

/* ------------------------------------------------------------------ node detail */
function NodeDetail({ n, range }: { n: MonitoringNode; range: Range }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const hist = useQuery({ queryKey: ["mon-node", n.node_id, range], queryFn: () => get<{ points: MetricPoint[] }>(`api/monitoring/nodes/${n.node_id}?range=${range}`) });
  const refresh = useMutation({
    mutationFn: () => post(`api/monitoring/nodes/${n.node_id}/health`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["monitoring"] }); toast.success(t("mon.health_refreshed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const [smart, setSmart] = useState<HealthDisk | null>(null);
  const pts = hist.data?.points ?? n.trend;
  return (
    <div className="space-y-5 p-5">
      <div className="grid gap-4 lg:grid-cols-3">
        <TrendChart title="CPU" points={pts} pick={(p) => p.cpu} peak={(p) => p.cpu_max} color={COLORS.cpu} lang={i18n.language} />
        <TrendChart title={t("mon.memory")} points={pts} pick={(p) => p.mem_pct} color={COLORS.mem} lang={i18n.language} />
        <TrendChart title={t("mon.disk_root")} points={pts} pick={(p) => p.disk_pct} color={COLORS.disk} lang={i18n.language} />
      </div>
      <div className="flex flex-wrap gap-6 text-sm">
        {n.cpus && <span><span className="text-muted">{t("mon.cores")}:</span> {n.cpus}</span>}
        {n.load.length > 0 && <span><span className="text-muted">{t("mon.load")}:</span> {n.load.map((l) => l.toFixed(2)).join(" · ")}</span>}
        {n.uptime && <span><span className="text-muted">{t("mon.uptime")}:</span> {formatDuration(n.uptime)}</span>}
        {n.health_at && <span className="text-muted">{t("mon.health_at", { ago: timeAgo(n.health_at, i18n.language) })}</span>}
        <Button size="sm" onClick={() => refresh.mutate()} loading={refresh.isPending}><RefreshCw /> {t("mon.refresh_health")}</Button>
      </div>
      {n.health_error && <Alert tone="warning">{n.health_error}</Alert>}
      <div className="grid gap-5 xl:grid-cols-2">
        <div>
          <h3 className="mb-2 text-[13px] font-semibold">{t("mon.storages")}</h3>
          {!n.storages.length ? <p className="text-xs text-muted">{t("mon.no_health")}</p> : (
            <div className="space-y-2.5">
              {n.storages.map((s) => (
                <div key={s.storage}>
                  <UsageBar label={`${s.storage} · ${s.type}${s.shared ? ` · ${t("cl.shared")}` : ""}${s.active ? "" : ` · ${t("cl.inactive")}`}`} used={s.used} total={s.total} detail={s.total ? `${formatBytes(s.used)} / ${formatBytes(s.total)}` : "-"} />
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h3 className="mb-2 text-[13px] font-semibold">{t("mon.physical_disks")}</h3>
          {!n.disks.length ? <p className="text-xs text-muted">{t("mon.no_health")}</p> : (
            <table className="w-full text-sm">
              <tbody>
                {n.disks.map((d) => (
                  <tr key={d.devpath} className="border-b border-line last:border-0">
                    <td className="py-1.5 pr-2"><HardDrive className="inline size-4 text-muted" /> <span className="mono">{d.devpath}</span></td>
                    <td className="py-1.5 pr-2">{d.model}<div className="text-xs text-muted">{d.type} · {formatBytes(d.size)}{d.used ? ` · ${d.used}` : ""}</div></td>
                    <td className="py-1.5 pr-2">{d.health === "PASSED" || d.health === "OK" ? <Badge tone="online">{d.health}</Badge> : d.health && d.health !== "UNKNOWN" ? <Badge tone="offline">{d.health}</Badge> : <Badge>{d.health || "?"}</Badge>}</td>
                    <td className="py-1.5 pr-2 text-xs">{d.wearout != null ? t("mon.wearout", { pct: d.wearout }) : ""}</td>
                    <td className="py-1.5 text-right"><Button size="sm" variant="ghost" onClick={() => setSmart(d)}>SMART</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      {smart && <SmartDialog nodeId={n.node_id} disk={smart} onClose={() => setSmart(null)} />}
    </div>
  );
}

function TrendChart({ title, points, pick, peak, color, lang }: { title: string; points: MetricPoint[]; pick: (p: MetricPoint) => number | null; peak?: (p: MetricPoint) => number | null; color: string; lang: string }) {
  const { t } = useTranslation();
  const W = 400, H = 120;
  const vals = points.map(pick);
  const nums = vals.filter((v): v is number => v != null);
  const avg = nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null;
  const max = nums.length ? Math.max(...(peak ? points.map(peak).filter((v): v is number => v != null) : nums), ...nums) : null;
  const xy = (v: number, i: number) => `${((i * W) / Math.max(1, points.length - 1)).toFixed(1)},${(H - (Math.max(0, Math.min(100, v)) * H) / 100).toFixed(1)}`;
  const line = vals.map((v, i) => (v == null ? null : xy(v, i))).filter(Boolean).join(" ");
  const area = nums.length > 1 ? `0,${H} ${line} ${W},${H}` : "";
  return (
    <div className="rounded-md border border-line bg-surface p-3">
      <div className="mb-2 flex items-baseline justify-between text-[13px]">
        <span className="font-semibold">{title}</span>
        <span className="text-xs text-muted">{avg == null ? t("mon.no_data") : t("mon.avg_max", { avg: Math.round(avg), max: Math.round(max ?? 0) })}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-28 w-full" role="img" aria-label={title}>
        {[25, 50, 75].map((g) => <line key={g} x1="0" x2={W} y1={H - (g * H) / 100} y2={H - (g * H) / 100} stroke="var(--line)" strokeWidth="1" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />)}
        {area && <polygon points={area} fill={color} opacity="0.12" />}
        {nums.length > 1 && <polyline points={line} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />}
      </svg>
      {points.length > 0 && (
        <div className="mt-1 flex justify-between text-[11px] text-muted">
          <span>{formatDate(points[0].at, lang)}</span><span>{formatDate(points[points.length - 1].at, lang)}</span>
        </div>
      )}
    </div>
  );
}

function SmartDialog({ nodeId, disk, onClose }: { nodeId: string; disk: HealthDisk; onClose: () => void }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["smart", nodeId, disk.devpath],
    queryFn: () => get<{ health?: string; type?: string; text?: string; attributes?: { id?: string | number; name: string; value?: string | number; worst?: string | number; threshold?: string | number; raw?: string; fail?: string }[] }>(`api/monitoring/nodes/${nodeId}/smart?disk=${encodeURIComponent(disk.devpath)}`),
  });
  const s = q.data;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} xl title={`SMART · ${disk.devpath}`} description={disk.model}>
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : (
        <div className="space-y-3">
          <div><Badge tone={s?.health === "PASSED" || s?.health === "OK" ? "online" : "offline"}>{s?.health ?? "?"}</Badge></div>
          {s?.attributes?.length ? (
            <Table>
              <thead><tr><Th>ID</Th><Th>{t("mon.attribute")}</Th><Th>{t("mon.value")}</Th><Th>{t("mon.worst")}</Th><Th>{t("mon.threshold")}</Th><Th>Raw</Th></tr></thead>
              <tbody>
                {s.attributes.map((a, i) => (
                  <Tr key={i} className={a.fail && a.fail !== "-" ? "text-offline" : ""}><Td>{a.id}</Td><Td>{a.name}</Td><Td>{a.value}</Td><Td>{a.worst}</Td><Td>{a.threshold}</Td><Td className="mono text-xs">{a.raw}</Td></Tr>
                ))}
              </tbody>
            </Table>
          ) : <pre className="max-h-[480px] overflow-auto rounded-md bg-sunken p-3 text-[12px]">{s?.text ?? t("mon.no_data")}</pre>}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ alerts */
function AlertHistory({ alerts }: { alerts: MonAlert[] }) {
  const { t, i18n } = useTranslation();
  const [show, setShow] = useState(false);
  const list = useMemo(() => (show ? alerts : alerts.slice(0, 10)), [alerts, show]);
  return (
    <Card>
      <CardHeader title={t("mon.alert_history")} description={t("mon.alert_history_hint")} icon={<BellRing />} />
      {!alerts.length ? <EmptyState title={t("mon.no_alerts")} /> : (
        <>
          <Table>
            <thead><tr><Th>{t("mon.node")}</Th><Th>{t("mon.alert")}</Th><Th>{t("mon.started")}</Th><Th>{t("mon.resolved")}</Th><Th>{t("mon.mailed")}</Th></tr></thead>
            <tbody>
              {list.map((a) => (
                <Tr key={a.id}>
                  <Td className="font-semibold">{a.node ?? "-"}</Td>
                  <Td><div className="flex items-center gap-2"><Badge tone={a.resolved_at ? "neutral" : a.severity === "critical" ? "offline" : "warning"}>{t(`mon.kind_${a.kind}`, { defaultValue: a.kind })}</Badge><span className="text-sm">{a.message}</span></div></Td>
                  <Td className="text-sm">{formatDate(a.started_at, i18n.language)}</Td>
                  <Td className="text-sm">{a.resolved_at ? formatDate(a.resolved_at, i18n.language) : <Badge tone="offline" dot>{t("mon.open")}</Badge>}</Td>
                  <Td className="text-sm">{a.notified_at ? t("common.yes") : "-"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          {alerts.length > 10 && <div className="p-3 text-center"><Button size="sm" variant="ghost" onClick={() => setShow(!show)}>{show ? t("mon.show_less") : t("mon.show_all", { count: alerts.length })}</Button></div>}
        </>
      )}
    </Card>
  );
}

function SettingsDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["mon-settings"], queryFn: () => get<MonitoringSettings>("api/monitoring/settings") });
  const [f, setF] = useState<MonitoringSettings | null>(null);
  const v = f ?? q.data ?? null;
  const set = (p: Partial<MonitoringSettings>) => v && setF({ ...v, ...p });
  const save = useMutation({
    mutationFn: () => put<MonitoringSettings>("api/monitoring/settings", v),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["mon-settings"] }); toast.success(t("mon.settings_saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const num = (k: keyof MonitoringSettings, label: string, hint?: string) => v && (
    <Field label={label} hint={hint}><Input type="number" value={Number(v[k])} onChange={(e) => set({ [k]: Number(e.target.value) } as Partial<MonitoringSettings>)} /></Field>
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={t("mon.settings")} description={t("mon.settings_hint")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!v} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      {!v ? <Spinner /> : (
        <div className="space-y-4">
          <Switch checked={v.enabled} onCheckedChange={(x) => set({ enabled: x })} label={t("mon.alerts_enabled")} description={t("mon.alerts_enabled_hint")} />
          <Field label={t("mon.mail_to")} hint={t("mon.mail_to_hint")}><Input value={v.mail_to.join(", ")} onChange={(e) => set({ mail_to: e.target.value.split(/[,;\s]+/).filter(Boolean) })} /></Field>
          <div className="grid gap-4 sm:grid-cols-3">
            {num("cpu_pct", t("mon.th_cpu"))}
            {num("cpu_minutes", t("mon.th_cpu_minutes"))}
            {num("mem_pct", t("mon.th_mem"))}
            {num("disk_pct", t("mon.th_disk"))}
            {num("storage_pct", t("mon.th_storage"))}
            {num("offline_minutes", t("mon.th_offline"))}
          </div>
          <Switch checked={v.smart} onCheckedChange={(x) => set({ smart: x })} label={t("mon.th_smart")} />
          {v.smart && <div className="max-w-xs">{num("wearout_pct", t("mon.th_wearout"), t("mon.th_wearout_hint"))}</div>}
        </div>
      )}
    </Dialog>
  );
}

