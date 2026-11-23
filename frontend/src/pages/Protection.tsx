import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive, ArrowLeft, CheckCircle2, ChevronRight, Database, Eye, EyeOff, FileArchive, Folder, FolderOpen, HardDrive,
  History, Play, Plus, RefreshCw, RotateCcw, Save, ServerCog, ShieldAlert, ShieldCheck, Trash2, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { useCustomer } from "@/components/customers";
import { JobView } from "@/components/jobs";
import { tabCls } from "@/components/net";
import { UsageBar } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  BackupFileEntry, BackupPolicy, BackupVolume, Coverage, CoverageGuest, CoverageMachine, CoverageRow, CoverageStatus,
  NodeJob, PbsJob, PbsOverview, StorageRow,
} from "@/lib/types";
import { formatBytes, formatDate, timeAgo } from "@/lib/utils";

type Tone = "online" | "warning" | "offline" | "info" | "neutral" | "accent";
const STATUS_TONE: Record<CoverageStatus, Tone> = {
  failed: "offline", never: "offline", no_job: "offline", stale: "warning", none: "warning", unknown: "neutral",
  external: "info", backup_server: "info", ok: "online", excluded: "neutral", not_monitored: "neutral",
};
const PROBLEMS: CoverageStatus[] = ["failed", "never", "no_job", "stale", "none"];

export function StatusBadge({ status }: { status: CoverageStatus }) {
  const { t } = useTranslation();
  const Icon = status === "ok" ? CheckCircle2 : PROBLEMS.includes(status) ? (status === "stale" || status === "none" ? ShieldAlert : XCircle) : ShieldCheck;
  return <Badge tone={STATUS_TONE[status]} className="whitespace-nowrap"><Icon className="size-3" />{t(`prot.st_${status}`)}</Badge>;
}

function useAct<T = unknown>(fn: (v: T) => Promise<unknown>, keys: unknown[][], ok?: string) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => { keys.forEach((k) => qc.invalidateQueries({ queryKey: k })); if (ok) toast.success(ok); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
}

function guestLabel(g: CoverageGuest) {
  return `${g.type === "lxc" ? "CT" : "VM"} ${g.vmid}${g.name ? ` ${g.name}` : ""}`;
}

function Age({ iso }: { iso: string | null; hours?: number | null }) {
  const { t, i18n } = useTranslation();
  if (!iso) return <span className="text-muted">{t("prot.never")}</span>;
  return <span title={formatDate(iso, i18n.language)}>{timeAgo(iso, i18n.language)}</span>;
}

/* ------------------------------------------------------------------ page */
export function ProtectionPage() {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const [params, setParams] = useSearchParams();
  const picked = params.get("customer") || customerId;
  const pick = (id: string) => { const p = new URLSearchParams(params); if (id) p.set("customer", id); else p.delete("customer"); setParams(p, { replace: true }); };
  return (
    <div>
      {picked ? <CustomerCoverage id={picked} onBack={customerId ? undefined : () => pick("")} /> : (
        <>
          <PageHeader title={t("prot.title")} description={t("prot.desc")} />
          <AllCustomers onPick={pick} />
        </>
      )}
    </div>
  );
}

function AllCustomers({ onPick }: { onPick: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["protection"], queryFn: () => get<CoverageRow[]>("api/protection"), refetchInterval: 60_000 });
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const rows = q.data ?? [];
  const failed = rows.reduce((a, r) => a + r.summary.failed, 0);
  const missing = rows.reduce((a, r) => a + r.summary.missing, 0);
  const good = rows.reduce((a, r) => a + r.summary.protected, 0);
  const total = rows.reduce((a, r) => a + r.summary.considered, 0);
  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label={t("prot.k_protected")} value={total ? `${Math.round((good * 100) / total)}%` : "–"} hint={t("prot.k_protected_hint", { good, total })} tone="online" icon={<ShieldCheck />} />
        <Stat label={t("prot.k_failed")} value={failed} tone={failed ? "offline" : "neutral"} icon={<XCircle />} />
        <Stat label={t("prot.k_missing")} value={missing} tone={missing ? "warning" : "neutral"} icon={<ShieldAlert />} />
      </div>
      <Card>
        <CardHeader icon={<Archive />} title={t("prot.customers")} description={t("prot.customers_hint")} />
        {!rows.length ? <EmptyState icon={<Archive />} title={t("prot.no_customers")} /> : (
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("prot.col_customer")}</Th><Th>{t("prot.k_protected")}</Th><Th>{t("prot.k_failed")}</Th><Th>{t("prot.k_missing")}</Th><Th>{t("prot.col_last")}</Th><Th>{t("prot.col_jobs")}</Th><Th>{t("prot.col_checked")}</Th><Th /></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <Tr key={r.customer.id} className="cursor-pointer" onClick={() => onPick(r.customer.id)}>
                    <Td className="font-bold">{r.customer.name}</Td>
                    <Td className="min-w-40">{r.summary.considered ? <UsageBar label="" percent={r.summary.protected_pct ?? 0} detail={`${r.summary.protected} / ${r.summary.considered}`} /> : <span className="text-muted">–</span>}</Td>
                    <Td>{r.summary.failed ? <Badge tone="offline">{r.summary.failed}</Badge> : <span className="text-muted">0</span>}</Td>
                    <Td>{r.summary.missing ? <Badge tone="warning">{r.summary.missing}</Badge> : <span className="text-muted">0</span>}</Td>
                    <Td className="text-sm"><Age iso={r.summary.last_backup} /></Td>
                    <Td className="text-sm">{r.jobs}{r.pbs ? ` · PBS ${r.pbs}` : ""}</Td>
                    <Td className="text-sm text-muted">{r.collected_at ? timeAgo(r.collected_at, i18n.language) : t("prot.not_checked")}</Td>
                    <Td className="text-right"><ChevronRight className="inline size-4 text-muted" /></Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ one customer */
function CustomerCoverage({ id, onBack }: { id: string; onBack?: () => void }) {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "coverage";
  const setTab = (v: string) => { const p = new URLSearchParams(params); p.set("tab", v); setParams(p, { replace: true }); };
  const key = ["protection", id];
  const q = useQuery({ queryKey: key, queryFn: () => get<Coverage>(`api/protection/customers/${id}`), refetchInterval: 60_000 });
  const qc = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => post<Coverage>(`api/protection/customers/${id}/refresh`),
    onSuccess: (d) => { qc.setQueryData(key, d); toast.success(t("prot.refreshed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const v = q.data;
  return (
    <div>
      <PageHeader
        title={<span className="flex items-center gap-2">{onBack && <Button variant="ghost" size="icon" aria-label={t("common.back")} onClick={onBack}><ArrowLeft /></Button>}{t("prot.title_customer", { name: v?.customer.name ?? "…" })}</span>}
        description={v?.collected_at ? t("prot.checked_at", { when: timeAgo(v.collected_at, i18n.language) }) : t("prot.desc")}
        actions={admin && <Button variant="primary" loading={refresh.isPending} onClick={() => refresh.mutate()}>{!refresh.isPending && <RefreshCw />} {t("prot.refresh")}</Button>} />
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : v && (
        <div className="space-y-5">
          {!v.collected_at && <Alert tone="info" title={t("prot.not_collected")}>{t("prot.not_collected_hint")}</Alert>}
          {v.error && <Alert tone="warning">{v.error}</Alert>}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label={t("prot.k_protected")} value={v.summary.protected_pct != null ? `${v.summary.protected_pct}%` : "–"} hint={t("prot.k_protected_hint", { good: v.summary.protected, total: v.summary.considered })} tone="online" icon={<ShieldCheck />} />
            <Stat label={t("prot.k_failed")} value={v.summary.failed} tone={v.summary.failed ? "offline" : "neutral"} icon={<XCircle />} />
            <Stat label={t("prot.k_missing")} value={v.summary.missing} hint={t("prot.sla_hint", { hours: v.policy.max_age_hours })} tone={v.summary.missing ? "warning" : "neutral"} icon={<ShieldAlert />} />
            <Stat label={t("prot.k_last")} value={v.summary.last_backup ? timeAgo(v.summary.last_backup, i18n.language) : "–"} hint={t("prot.k_size", { size: formatBytes(v.summary.size) })} icon={<History />} />
          </div>
          <Tabs.Root value={tab} onValueChange={setTab}>
            <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
              <Tabs.Trigger value="coverage" className={tabCls}>{t("prot.tab_coverage")}</Tabs.Trigger>
              <Tabs.Trigger value="jobs" className={tabCls}>{t("prot.tab_jobs")}</Tabs.Trigger>
              <Tabs.Trigger value="pbs" className={tabCls}>{t("prot.tab_pbs")}</Tabs.Trigger>
              <Tabs.Trigger value="policy" className={tabCls}>{t("prot.tab_policy")}</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="coverage"><CoverageTab v={v} admin={admin} /></Tabs.Content>
            <Tabs.Content value="jobs"><JobsTab v={v} /></Tabs.Content>
            <Tabs.Content value="pbs"><PbsTab v={v} admin={admin} /></Tabs.Content>
            <Tabs.Content value="policy"><PolicyTab v={v} admin={admin} /></Tabs.Content>
          </Tabs.Root>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ coverage */
function CoverageTab({ v, admin }: { v: Coverage; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const [filter, setFilter] = useState<"problems" | "all">(v.summary.problems ? "problems" : "all");
  const [restore, setRestore] = useState<CoverageGuest | null>(null);
  const [backupNow, setBackupNow] = useState<CoverageGuest | null>(null);
  const exclude = useAct((x: { key: string; excluded: boolean }) => post(`api/protection/customers/${v.customer.id}/exclude`, x), [["protection", v.customer.id]], t("prot.saved"));
  const guests = filter === "all" ? v.guests : v.guests.filter((g) => PROBLEMS.includes(g.status));
  const machines = filter === "all" ? v.machines : v.machines.filter((m) => PROBLEMS.includes(m.status));
  return (
    <div className="space-y-5">
      <Segmented value={filter} onChange={setFilter} options={[{ value: "problems", label: t("prot.f_problems", { count: v.summary.problems }) }, { value: "all", label: t("prot.f_all") }]} />
      <Card>
        <CardHeader icon={<HardDrive />} title={t("prot.guests")} description={t("prot.guests_hint")} />
        {!guests.length ? <EmptyState icon={<ShieldCheck />} title={v.guests.length ? t("prot.no_problems") : t("prot.no_guests")} /> : (
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("prot.col_guest")}</Th><Th>{t("prot.col_status")}</Th><Th>{t("prot.col_last")}</Th><Th>{t("prot.col_size")}</Th><Th>{t("prot.col_jobs")}</Th><Th>{t("prot.col_storage")}</Th><Th /></tr></thead>
              <tbody>
                {guests.map((g) => (
                  <Tr key={g.key}>
                    <Td><div className="font-bold">{guestLabel(g)}</div><div className="text-xs text-muted">{g.cluster} · {g.node}{g.power !== "running" ? ` · ${g.power}` : ""}</div></Td>
                    <Td><StatusBadge status={g.status} />{g.failure && g.status === "failed" && <div className="mt-1 max-w-72 text-xs text-offline">{g.failure.error}</div>}</Td>
                    <Td className="text-sm"><Age iso={g.last_backup} hours={g.age_hours} />{g.verified && <div className="text-xs text-muted">{t("prot.verified", { state: g.verified })}</div>}</Td>
                    <Td className="text-sm">{g.size ? formatBytes(g.size) : "–"}{g.count > 1 && <div className="text-xs text-muted">{t("prot.versions", { count: g.count, size: formatBytes(g.total_size) })}</div>}</Td>
                    <Td className="text-sm">{g.jobs.length ? <span className="mono text-xs">{g.jobs.join(", ")}</span> : <span className="text-muted">–</span>}{g.next_run && <div className="text-xs text-muted">{t("prot.next", { when: formatDate(g.next_run, i18n.language) })}</div>}</Td>
                    <Td className="text-sm">{g.storages.join(", ") || "–"}{g.offsite && <Badge tone="info" className="ml-1.5">{t("prot.separate")}</Badge>}</Td>
                    <Td className="whitespace-nowrap text-right">
                      {admin && <>
                        <Button variant="ghost" size="icon" title={t("prot.backup_now")} aria-label={t("prot.backup_now")} onClick={() => setBackupNow(g)}><Play /></Button>
                        <Button variant="ghost" size="icon" title={t("prot.restore")} aria-label={t("prot.restore")} onClick={() => setRestore(g)} disabled={!g.count}><RotateCcw /></Button>
                        <Button variant="ghost" size="icon" title={g.status === "excluded" ? t("prot.include") : t("prot.exclude")} aria-label={g.status === "excluded" ? t("prot.include") : t("prot.exclude")} onClick={() => exclude.mutate({ key: g.key, excluded: g.status !== "excluded" })}>{g.status === "excluded" ? <Eye /> : <EyeOff />}</Button>
                      </>}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      <Card>
        <CardHeader icon={<ServerCog />} title={t("prot.machines")} description={t("prot.machines_hint")} />
        {!machines.length ? <EmptyState icon={<ShieldCheck />} title={v.machines.length ? t("prot.no_problems") : t("prot.no_machines")} /> : (
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("prot.col_machine")}</Th><Th>{t("prot.col_status")}</Th><Th>{t("prot.col_method")}</Th><Th>{t("prot.col_last")}</Th><Th /></tr></thead>
              <tbody>
                {machines.map((m) => <MachineRow key={m.key} m={m} admin={admin} onExclude={(x) => exclude.mutate({ key: m.key, excluded: x })} />)}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      {restore && <RestoreDialog g={restore} onClose={() => setRestore(null)} />}
      {backupNow && <BackupNowDialog g={backupNow} v={v} onClose={() => setBackupNow(null)} />}
    </div>
  );
}

function MachineRow({ m, admin, onExclude }: { m: CoverageMachine; admin: boolean; onExclude: (excluded: boolean) => void }) {
  const { t } = useTranslation();
  const wsb = m.local?.wsb;
  return (
    <Tr>
      <Td><Link to={m.role === "workstation" ? `/workstations/${m.machine_id}` : `/servers/${m.machine_id}`} className="font-bold hover:underline">{m.name}</Link><div className="text-xs text-muted">{t(`prot.role_${m.role}`)} · {m.os === "windows" ? "Windows" : "Linux"}{!m.online ? ` · ${t("prot.offline")}` : ""}</div></Td>
      <Td><StatusBadge status={m.status} />{m.error && <div className="mt-1 max-w-72 text-xs text-muted">{m.error}</div>}</Td>
      <Td className="text-sm">
        {m.method ? t(`prot.method_${m.method}`) : "–"}
        {m.method === "guest" && m.guest && <div className="text-xs text-muted">{t("prot.as_guest", { vmid: m.guest.vmid, cluster: m.guest.cluster })}</div>}
        {m.method === "wsb" && <div className="text-xs text-muted">{[wsb?.schedule?.length ? t("prot.wsb_at", { times: wsb.schedule.join(", ") }) : null, m.system_state ? t("prot.wsb_system_state") : t("prot.wsb_no_system_state")].filter(Boolean).join(" · ")}</div>}
        {m.detail && m.method !== "wsb" && <div className="max-w-72 text-xs text-muted">{m.detail}</div>}
        {m.detail && m.method === "wsb" && m.status === "failed" && <div className="text-xs text-offline">{t("prot.wsb_error", { code: m.detail })}</div>}
      </Td>
      <Td className="text-sm"><Age iso={m.last_backup} hours={m.age_hours} /></Td>
      <Td className="text-right">{admin && <Button variant="ghost" size="icon" title={m.status === "excluded" ? t("prot.include") : t("prot.exclude")} aria-label={m.status === "excluded" ? t("prot.include") : t("prot.exclude")} onClick={() => onExclude(m.status !== "excluded")}>{m.status === "excluded" ? <Eye /> : <EyeOff />}</Button>}</Td>
    </Tr>
  );
}

/* ------------------------------------------------------------------ restore */
function RestoreDialog({ g, onClose }: { g: CoverageGuest; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const vols = useQuery({ queryKey: ["guest-backups", g.node_id, g.vmid], queryFn: () => get<BackupVolume[]>(`api/protection/nodes/${g.node_id}/guests/${g.vmid}/backups`) });
  const stores = useQuery({ queryKey: ["storage", g.node_id], queryFn: () => get<{ storages: StorageRow[] }>(`api/cluster/${g.node_id}/storage`).then((r) => r.storages ?? []) });
  const [vol, setVol] = useState<BackupVolume | null>(null);
  const [mode, setMode] = useState<"new" | "overwrite">("new");
  const [vmid, setVmid] = useState("");
  const [storage, setStorage] = useState("");
  const [start, setStart] = useState(false);
  const [unique, setUnique] = useState(true);
  const [confirm, setConfirm] = useState("");
  const [job, setJob] = useState<{ id: string; vmid: number; node: string } | null>(null);
  const [browse, setBrowse] = useState<BackupVolume | null>(null);
  useEffect(() => { if (vols.data?.length && !vol) setVol(vols.data[0]); }, [vols.data, vol]);
  const diskStores = (stores.data ?? []).filter((s) => s.content.split(",").some((c) => c === (g.type === "lxc" ? "rootdir" : "images")));
  const go = useMutation({
    mutationFn: () => post<{ job: NodeJob; vmid: number; node: string }>(`api/protection/nodes/${g.node_id}/restore`, {
      volume: vol?.volid, vmid: mode === "overwrite" ? g.vmid : vmid ? Number(vmid) : null, storage: storage || null,
      overwrite: mode === "overwrite", start, unique: mode === "new" && unique, confirm }),
    onSuccess: (r) => setJob({ id: r.job.id, vmid: r.vmid, node: r.node }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const needStorage = g.type === "lxc" && mode === "new" && !storage;
  const blocked = !vol || needStorage || (mode === "overwrite" && confirm !== String(g.vmid));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={t("prot.restore_title", { name: guestLabel(g) })} description={t("prot.restore_hint")}
      footer={job ? <Button onClick={onClose}>{t("common.close")}</Button> : <><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant={mode === "overwrite" ? "danger" : "primary"} disabled={blocked} loading={go.isPending} onClick={() => go.mutate()}><RotateCcw /> {mode === "overwrite" ? t("prot.restore_overwrite_btn") : t("prot.restore_new_btn")}</Button></>}>
      {job ? (
        <div className="space-y-3"><Alert tone="info">{t("prot.restore_started", { vmid: job.vmid, node: job.node })}</Alert><JobView jobId={job.id} /></div>
      ) : (
        <div className="space-y-5">
          {vols.isLoading ? <Spinner /> : vols.isError ? <Alert tone="offline">{errorMessage(vols.error, t)}</Alert> : !vols.data?.length ? <EmptyState icon={<FileArchive />} title={t("prot.no_backups")} /> : (
            <div className="max-h-64 overflow-auto rounded-md border border-line">
              <Table>
                <thead><tr><Th /><Th>{t("prot.col_time")}</Th><Th>{t("prot.col_size")}</Th><Th>{t("prot.col_storage")}</Th><Th>{t("prot.col_notes")}</Th><Th /></tr></thead>
                <tbody>
                  {vols.data.map((b) => (
                    <Tr key={b.volid} className="cursor-pointer" onClick={() => setVol(b)}>
                      <Td><input type="radio" checked={vol?.volid === b.volid} onChange={() => setVol(b)} aria-label={b.volid} /></Td>
                      <Td className="text-sm">{b.ctime ? formatDate(b.ctime, i18n.language) : "–"}{b.protected && <Badge className="ml-1.5">{t("prot.protected")}</Badge>}</Td>
                      <Td className="text-sm">{formatBytes(b.size)}</Td>
                      <Td className="text-sm">{b.storage}{b.verified && <Badge tone={b.verified === "ok" ? "online" : "offline"} className="ml-1.5">{t("prot.verified", { state: b.verified })}</Badge>}</Td>
                      <Td className="max-w-56 truncate text-xs text-muted">{b.notes}</Td>
                      <Td className="text-right">{b.storage_type === "pbs" && <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setBrowse(b); }}><FolderOpen /> {t("prot.files")}</Button>}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </div>
          )}
          <Segmented value={mode} onChange={setMode} options={[{ value: "new", label: t("prot.mode_new") }, { value: "overwrite", label: t("prot.mode_overwrite") }]} />
          {mode === "new" ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("prot.target_vmid")} hint={t("prot.target_vmid_hint")}><Input type="number" min={100} value={vmid} onChange={(e) => setVmid(e.target.value)} placeholder={t("prot.next_free")} /></Field>
              <Field label={t("prot.target_storage")} hint={g.type === "lxc" ? t("prot.target_storage_ct") : t("prot.target_storage_vm")}>
                <Select value={storage} onChange={(e) => setStorage(e.target.value)}><option value="">{g.type === "lxc" ? "…" : t("prot.original_storage")}</option>{diskStores.map((s) => <option key={s.storage} value={s.storage}>{s.storage} ({s.type})</option>)}</Select>
              </Field>
              <Switch checked={unique} onCheckedChange={setUnique} label={t("prot.unique")} description={t("prot.unique_hint")} />
              <Switch checked={start} onCheckedChange={setStart} label={t("prot.start_after")} />
            </div>
          ) : (
            <div className="space-y-3">
              <Alert tone="offline" title={t("prot.overwrite_warn_title")}>{t("prot.overwrite_warn", { name: guestLabel(g) })}</Alert>
              {g.power !== "stopped" && <Alert tone="warning">{t("prot.overwrite_stop_first")}</Alert>}
              <Switch checked={start} onCheckedChange={setStart} label={t("prot.start_after")} />
              <Field label={t("prot.type_vmid", { vmid: g.vmid })}><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} className="mono" /></Field>
            </div>
          )}
        </div>
      )}
      {browse && <FileBrowser nodeId={g.node_id} vol={browse} onClose={() => setBrowse(null)} />}
    </Dialog>
  );
}

function FileBrowser({ nodeId, vol, onClose }: { nodeId: string; vol: BackupVolume; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [path, setPath] = useState("/");
  const [sel, setSel] = useState<BackupFileEntry | null>(null);
  const [target, setTarget] = useState("");
  const [job, setJob] = useState<{ id: string; target: string } | null>(null);
  const q = useQuery({ queryKey: ["backup-files", nodeId, vol.volid, path], queryFn: () => get<{ entries: BackupFileEntry[] }>(`api/protection/nodes/${nodeId}/files?volume=${encodeURIComponent(vol.volid)}&path=${encodeURIComponent(path)}`) });
  const go = useMutation({
    mutationFn: () => post<{ job: NodeJob; target: string }>(`api/protection/nodes/${nodeId}/files/restore`, { volume: vol.volid, path: sel?.path, target_dir: target || null }),
    onSuccess: (r) => setJob({ id: r.job.id, target: r.target }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const up = path === "/" ? null : path.replace(/\/[^/]+\/?$/, "") || "/";
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={t("prot.files_title")} description={t("prot.files_hint")}
      footer={job ? <Button onClick={onClose}>{t("common.close")}</Button> : <><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!sel} loading={go.isPending} onClick={() => go.mutate()}><RotateCcw /> {t("prot.files_restore")}</Button></>}>
      {job ? (
        <div className="space-y-3"><Alert tone="info">{t("prot.files_started", { target: job.target })}</Alert><JobView jobId={job.id} /><p className="text-sm text-muted">{t("prot.files_next")} <Link to="/servers/copy" className="text-accent-text hover:underline">{t("nav.srv_copy")}</Link></p></div>
      ) : (
        <div className="space-y-4">
          <div className="mono flex items-center gap-2 rounded-md bg-sunken px-3 py-2 text-xs">{up && <Button variant="ghost" size="sm" onClick={() => { setPath(up); setSel(null); }}><ArrowLeft /></Button>}<span className="truncate">{path}</span></div>
          {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : !q.data?.entries.length ? <EmptyState title={t("prot.files_empty")} /> : (
            <ul className="max-h-80 divide-y divide-line overflow-auto rounded-md border border-line">
              {q.data.entries.map((e) => (
                <li key={e.path} className={`flex cursor-pointer items-center gap-2 px-3 py-2 text-sm hover:bg-sunken ${sel?.path === e.path ? "bg-sunken" : ""}`}
                  onClick={() => setSel(e)} onDoubleClick={() => { if (!e.leaf) { setPath(e.path); setSel(null); } }}>
                  {e.leaf ? <FileArchive className="size-4 text-muted" /> : <Folder className="size-4 text-koraal" />}
                  <span className="flex-1 truncate">{e.name}</span>
                  {e.size != null && e.leaf && <span className="text-xs text-muted">{formatBytes(e.size)}</span>}
                  {e.mtime && <span className="hidden text-xs text-muted sm:inline">{formatDate(e.mtime, i18n.language)}</span>}
                  {!e.leaf && <Button size="sm" variant="ghost" onClick={(ev) => { ev.stopPropagation(); setPath(e.path); setSel(null); }}><ChevronRight /></Button>}
                </li>
              ))}
            </ul>
          )}
          <Field label={t("prot.files_target")} hint={t("prot.files_target_hint")}><Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="/var/lib/stackpilot-restore/…" className="mono" /></Field>
          {sel && <p className="text-sm">{t("prot.files_selected")} <span className="mono">{sel.path}</span></p>}
        </div>
      )}
    </Dialog>
  );
}

function BackupNowDialog({ g, v, onClose }: { g: CoverageGuest; v: Coverage; onClose: () => void }) {
  const { t } = useTranslation();
  const cl = v.clusters.find((c) => c.name === g.cluster);
  const stores = cl?.storages ?? [];
  const [storage, setStorage] = useState(stores.find((s) => s.type === "pbs")?.storage ?? stores[0]?.storage ?? "");
  const [mode, setMode] = useState<"snapshot" | "suspend" | "stop">("snapshot");
  const [prot, setProt] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const go = useMutation({
    mutationFn: () => post<NodeJob>(`api/protection/nodes/${g.node_id}/backup-now`, { vmid: g.vmid, storage, mode, protected: prot }),
    onSuccess: (r) => setJob(r.id),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={t("prot.backup_now_title", { name: guestLabel(g) })}
      footer={job ? <Button onClick={onClose}>{t("common.close")}</Button> : <><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!storage} loading={go.isPending} onClick={() => go.mutate()}><Play /> {t("prot.backup_now")}</Button></>}>
      {job ? <JobView jobId={job} /> : (
        <div className="space-y-4">
          <Field label={t("prot.col_storage")}><Select value={storage} onChange={(e) => setStorage(e.target.value)}>{stores.map((s) => <option key={s.storage} value={s.storage}>{s.storage} ({s.type})</option>)}</Select></Field>
          <Field label={t("cl.mode")}><Select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>{(["snapshot", "suspend", "stop"] as const).map((m) => <option key={m} value={m}>{t(`cl.mode_${m}`)}</option>)}</Select></Field>
          <Switch checked={prot} onCheckedChange={setProt} label={t("prot.protected")} description={t("prot.protected_hint")} />
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ jobs and storages */
function JobsTab({ v }: { v: Coverage }) {
  const { t, i18n } = useTranslation();
  if (!v.clusters.length) return <Card><EmptyState icon={<Database />} title={t("prot.no_clusters")} /></Card>;
  return (
    <div className="space-y-5">
      {v.clusters.map((c) => (
        <Card key={c.key}>
          <CardHeader icon={<Database />} title={c.name} description={c.nodes.join(", ")}
            action={c.anchor_id && <Link to={`/proxmox/cluster?tab=backup&node=${c.anchor_id}`}><Button size="sm"><Plus /> {t("prot.manage_jobs")}</Button></Link>} />
          <CardBody className="space-y-4">
            {c.error && <Alert tone="warning">{c.error === "offline" ? t("prot.cluster_offline") : c.error}</Alert>}
            {c.runs && <p className="text-sm text-muted">{t("prot.runs", { ok: c.runs.ok, failed: c.runs.failed, days: 14 })}{c.runs.last_ok ? ` · ${t("prot.last_ok", { when: timeAgo(c.runs.last_ok, i18n.language) })}` : ""}</p>}
            {c.runs?.job_errors?.map((e, i) => <Alert key={i} tone="offline">{formatDate(e.at, i18n.language)} · {e.node}: {e.error}</Alert>)}
            {!c.jobs?.length ? <Alert tone="offline" title={t("prot.no_jobs")}>{t("prot.no_jobs_hint")}</Alert> : (
              <div className="overflow-x-auto"><Table>
                <thead><tr><Th>{t("prot.col_job")}</Th><Th>{t("cl.schedule")}</Th><Th>{t("prot.col_storage")}</Th><Th>{t("cl.selection")}</Th><Th>{t("prot.col_retention")}</Th><Th>{t("cl.next_run")}</Th><Th>{t("cl.state")}</Th></tr></thead>
                <tbody>{c.jobs.map((j) => (
                  <Tr key={j.id}>
                    <Td className="mono text-xs">{j.id}{j.comment && <div className="font-sans text-muted">{j.comment}</div>}</Td>
                    <Td className="mono text-sm">{j.schedule}</Td>
                    <Td>{j.storage}</Td>
                    <Td className="text-sm">{j.all ? t("cl.all_guests") : j.pool ? `pool ${j.pool}` : <span className="mono text-xs">{j.vmid}</span>}{j.exclude ? <div className="text-xs text-muted">{t("prot.excluding", { ids: j.exclude })}</div> : null}{j.node ? ` · ${j.node}` : ""}</Td>
                    <Td className="mono text-xs">{typeof j.prune === "string" ? j.prune : j.prune ? Object.entries(j.prune as Record<string, string>).map(([k, x]) => `${k}=${x}`).join(", ") : "–"}</Td>
                    <Td className="text-sm">{j.next_run ? formatDate(j.next_run, i18n.language) : "–"}</Td>
                    <Td>{j.enabled ? <Badge tone="online">{t("cl.enabled")}</Badge> : <Badge>{t("cl.disabled")}</Badge>}</Td>
                  </Tr>))}
                </tbody>
              </Table></div>
            )}
            {!!c.storages?.length && (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {c.storages.map((s) => (
                  <div key={s.storage} className="rounded-md border border-line p-3">
                    <div className="flex items-center justify-between text-sm font-bold">{s.storage}<span className="flex gap-1"><Badge>{s.type}</Badge>{s.shared ? <Badge tone="info">{t("prot.separate")}</Badge> : <Badge tone="warning">{t("prot.local")}</Badge>}</span></div>
                    <div className="mt-1 text-xs text-muted">{t("prot.storage_backups", { count: s.backups, size: formatBytes(s.size) })}{s.server ? ` · ${s.server}${s.datastore ? `:${s.datastore}` : ""}` : ""}</div>
                    {s.total ? <div className="mt-2"><UsageBar label="" used={s.used} total={s.total} detail={`${formatBytes(s.used)} / ${formatBytes(s.total)}`} /></div> : null}
                  </div>
                ))}
              </div>
            )}
            {c.errors?.length ? <p className="text-xs text-muted">{c.errors.join(" · ")}</p> : null}
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ Proxmox Backup Server */
function PbsTab({ v, admin }: { v: Coverage; admin: boolean }) {
  const { t } = useTranslation();
  if (!v.pbs.length) return <Card><EmptyState icon={<Archive />} title={t("prot.no_pbs")}>{t("prot.no_pbs_hint")}</EmptyState></Card>;
  return <div className="space-y-5">{v.pbs.map((p) => <PbsCard key={p.node_id} nodeId={p.node_id} name={p.name} admin={admin} />)}</div>;
}

function PbsCard({ nodeId, name, admin }: { nodeId: string; name: string; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const key = ["pbs", nodeId];
  const q = useQuery({ queryKey: key, queryFn: () => get<PbsOverview>(`api/protection/pbs/${nodeId}`) });
  const [jobDlg, setJobDlg] = useState<{ kind: "verify" | "sync" | "prune"; job?: PbsJob } | null>(null);
  const [addDs, setAddDs] = useState(false);
  const [addRemote, setAddRemote] = useState(false);
  const runJob = useAct((x: { kind: string; id: string }) => post(`api/protection/pbs/${nodeId}/jobs/${x.kind}/${x.id}/run`), [key], t("prot.started"));
  const delJob = useAct((x: { kind: string; id: string }) => del(`api/protection/pbs/${nodeId}/jobs/${x.kind}/${x.id}`), [key], t("prot.removed"));
  const p = q.data;
  const ts = (n?: number | null) => (n ? formatDate(new Date(n * 1000).toISOString(), i18n.language) : "–");
  const jobTable = (kind: "verify" | "sync" | "prune", rows: PbsJob[] | undefined) => (
    <div>
      <div className="mb-2 flex items-center justify-between"><h3 className="text-[13px] font-bold">{t(`prot.pbs_${kind}`)}</h3>{admin && <Button size="sm" onClick={() => setJobDlg({ kind })}><Plus /> {t("prot.add")}</Button>}</div>
      {!rows?.length ? <p className="text-sm text-muted">{t(`prot.pbs_no_${kind}`)}</p> : (
        <div className="overflow-x-auto"><Table>
          <thead><tr><Th>{t("prot.col_job")}</Th><Th>{t("prot.col_store")}</Th>{kind === "sync" && <Th>{t("prot.col_remote")}</Th>}<Th>{t("cl.schedule")}</Th><Th>{t("prot.col_last_run")}</Th><Th>{t("cl.next_run")}</Th><Th /></tr></thead>
          <tbody>{rows.map((j) => (
            <Tr key={j.id}>
              <Td className="mono text-xs">{j.id}</Td><Td>{j.store}</Td>
              {kind === "sync" && <Td className="text-sm">{j.remote}:{String(j["remote-store"] ?? "")}</Td>}
              <Td className="mono text-sm">{j.schedule ?? "–"}</Td>
              <Td className="text-sm">{j.last_state ? <Badge tone={j.last_state === "ok" ? "online" : "offline"}>{j.last_state}</Badge> : "–"} <span className="text-xs text-muted">{ts(j.last_run)}</span></Td>
              <Td className="text-sm">{ts(j.next_run)}</Td>
              <Td className="whitespace-nowrap text-right">{admin && <>
                <Button variant="ghost" size="icon" aria-label={t("cl.run_now")} onClick={() => runJob.mutate({ kind, id: j.id })}><Play /></Button>
                <Button variant="ghost" size="icon" aria-label={t("common.edit")} onClick={() => setJobDlg({ kind, job: j })}><Save /></Button>
                <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => delJob.mutate({ kind, id: j.id })}><Trash2 /></Button>
              </>}</Td>
            </Tr>))}
          </tbody>
        </Table></div>
      )}
    </div>
  );
  return (
    <Card>
      <CardHeader icon={<Archive />} title={name} description={p?.version ?? t("prot.pbs")}
        action={admin && p && <><Button size="sm" onClick={() => setAddDs(true)}><Plus /> {t("prot.add_datastore")}</Button><Button size="sm" onClick={() => setAddRemote(true)}><Plus /> {t("prot.add_remote")}</Button></>} />
      <CardBody className="space-y-6">
        {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : p && (
          <>
            {p.fingerprint && <div className="flex flex-wrap items-center gap-2 text-sm"><span className="text-muted">{t("prot.fingerprint")}</span><span className="mono break-all text-xs">{p.fingerprint}</span><CopyButton value={p.fingerprint} /></div>}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(p.datastores ?? []).map((d) => (
                <div key={d.name} className="rounded-md border border-line p-3">
                  <div className="text-sm font-bold">{d.name}</div><div className="mono text-xs text-muted">{d.path}</div>
                  {d.total ? <div className="mt-2"><UsageBar label="" used={d.used ?? 0} total={d.total} detail={`${formatBytes(d.used)} / ${formatBytes(d.total)}`} /></div> : null}
                  <div className="mt-1 text-xs text-muted">{t("prot.gc", { state: d.gc_state ?? "–", when: ts(d.gc_last) })}</div>
                </div>
              ))}
              {!p.datastores?.length && <p className="text-sm text-muted">{t("prot.no_datastores")}</p>}
            </div>
            {jobTable("verify", p.verify_jobs)}
            {jobTable("sync", p.sync_jobs)}
            {jobTable("prune", p.prune_jobs)}
            <div>
              <h3 className="mb-2 text-[13px] font-bold">{t("prot.remotes")}</h3>
              {!p.remotes?.length ? <p className="text-sm text-muted">{t("prot.no_remotes")}</p> : <ul className="text-sm">{p.remotes.map((r) => <li key={r.name}><span className="font-semibold">{r.name}</span> · <span className="mono text-xs">{r["auth-id"]}@{r.host}</span></li>)}</ul>}
            </div>
          </>
        )}
      </CardBody>
      {jobDlg && p && <PbsJobDialog nodeId={nodeId} kind={jobDlg.kind} job={jobDlg.job} p={p} onClose={() => setJobDlg(null)} />}
      {addDs && <DatastoreDialog nodeId={nodeId} onClose={() => setAddDs(false)} />}
      {addRemote && <RemoteDialog nodeId={nodeId} onClose={() => setAddRemote(false)} />}
    </Card>
  );
}

function PbsJobDialog({ nodeId, kind, job, p, onClose }: { nodeId: string; kind: "verify" | "sync" | "prune"; job?: PbsJob; p: PbsOverview; onClose: () => void }) {
  const { t } = useTranslation();
  const num = (x: unknown) => (x == null || x === "" ? null : Number(x));
  const [f, setF] = useState({
    id: job?.id ?? "", store: job?.store ?? p.datastores?.[0]?.name ?? "", schedule: job?.schedule ?? (kind === "verify" ? "sat 03:00" : kind === "sync" ? "daily" : "daily"),
    remote: (job?.remote as string) ?? p.remotes?.[0]?.name ?? "", remote_store: String(job?.["remote-store"] ?? ""), direction: "pull" as "pull" | "push",
    remove_vanished: Boolean(job?.["remove-vanished"]), outdated_after: num(job?.["outdated-after"]) ?? 30,
    keep_last: num(job?.["keep-last"]), keep_daily: num(job?.["keep-daily"]) ?? (job ? null : 7), keep_weekly: num(job?.["keep-weekly"]) ?? (job ? null : 4),
    keep_monthly: num(job?.["keep-monthly"]) ?? (job ? null : 6), keep_yearly: num(job?.["keep-yearly"]), comment: (job?.comment as string) ?? "",
  });
  const set = (x: Partial<typeof f>) => setF({ ...f, ...x });
  const save = useAct(() => post(`api/protection/pbs/${nodeId}/jobs`, { kind, ...f, comment: f.comment || null }), [["pbs", nodeId]], t("prot.saved"));
  const val = (x: number | null) => x ?? "";
  const n = (s: string) => (s === "" ? null : Number(s));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={t(`prot.pbs_${kind}`)} description={t(`prot.pbs_${kind}_hint`)}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!f.id || !f.store} loading={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: onClose })}>{t("common.save")}</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("prot.col_job")}><Input value={f.id} disabled={!!job} onChange={(e) => set({ id: e.target.value })} className="mono" placeholder={kind === "sync" ? "offsite" : kind} /></Field>
        <Field label={t("prot.col_store")}><Select value={f.store} onChange={(e) => set({ store: e.target.value })}>{(p.datastores ?? []).map((d) => <option key={d.name}>{d.name}</option>)}</Select></Field>
        <Field label={t("cl.schedule")} hint={t("prot.schedule_hint")}><Input value={f.schedule} onChange={(e) => set({ schedule: e.target.value })} className="mono" /></Field>
        {kind === "verify" && <Field label={t("prot.outdated_after")}><Input type="number" min={1} value={val(f.outdated_after)} onChange={(e) => set({ outdated_after: n(e.target.value) ?? 30 })} /></Field>}
        {kind === "sync" && <>
          <Field label={t("prot.col_remote")}><Select value={f.remote} onChange={(e) => set({ remote: e.target.value })}><option value="">…</option>{(p.remotes ?? []).map((r) => <option key={r.name}>{r.name}</option>)}</Select></Field>
          <Field label={t("prot.remote_store")}><Input value={f.remote_store} onChange={(e) => set({ remote_store: e.target.value })} /></Field>
          <Field label={t("prot.direction")}><Select value={f.direction} onChange={(e) => set({ direction: e.target.value as "pull" | "push" })}><option value="pull">{t("prot.dir_pull")}</option><option value="push">{t("prot.dir_push")}</option></Select></Field>
          <Switch checked={f.remove_vanished} onCheckedChange={(x) => set({ remove_vanished: x })} label={t("prot.remove_vanished")} />
        </>}
        {kind === "prune" && (["keep_last", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"] as const).map((k) => (
          <Field key={k} label={t(`prot.${k}`)}><Input type="number" min={0} value={val(f[k])} onChange={(e) => set({ [k]: n(e.target.value) })} /></Field>
        ))}
        <Field label={t("cl.comment")}><Input value={f.comment} onChange={(e) => set({ comment: e.target.value })} /></Field>
      </div>
    </Dialog>
  );
}

function DatastoreDialog({ nodeId, onClose }: { nodeId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [f, setF] = useState({ name: "", path: "/mnt/datastore/", gc_schedule: "daily" });
  const save = useAct(() => post(`api/protection/pbs/${nodeId}/datastores`, f), [["pbs", nodeId]], t("prot.saved"));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={t("prot.add_datastore")} description={t("prot.datastore_hint")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!f.name || f.path.length < 3} loading={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: onClose })}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("prot.ds_name")}><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label={t("prot.ds_path")}><Input value={f.path} onChange={(e) => setF({ ...f, path: e.target.value })} className="mono" /></Field>
        <Field label={t("prot.gc_schedule")}><Input value={f.gc_schedule} onChange={(e) => setF({ ...f, gc_schedule: e.target.value })} className="mono" /></Field>
      </div>
    </Dialog>
  );
}

function RemoteDialog({ nodeId, onClose }: { nodeId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [f, setF] = useState({ name: "offsite", host: "", auth_id: "sync@pbs", password: "", fingerprint: "" });
  const save = useAct(() => post(`api/protection/pbs/${nodeId}/remotes`, { ...f, fingerprint: f.fingerprint || null }), [["pbs", nodeId]], t("prot.saved"));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={t("prot.add_remote")} description={t("prot.remote_hint")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!f.name || !f.host || !f.password} loading={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: onClose })}>{t("common.save")}</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("prot.remote_name")}><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label={t("prot.remote_host")}><Input value={f.host} onChange={(e) => setF({ ...f, host: e.target.value })} placeholder="pbs-offsite.example.nl" /></Field>
        <Field label={t("prot.auth_id")}><Input value={f.auth_id} onChange={(e) => setF({ ...f, auth_id: e.target.value })} className="mono" /></Field>
        <Field label={t("prot.remote_password")}><Input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" /></Field>
        <div className="sm:col-span-2"><Field label={t("prot.fingerprint")}><Input value={f.fingerprint} onChange={(e) => setF({ ...f, fingerprint: e.target.value })} className="mono" /></Field></div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ policy */
function PolicyTab({ v, admin }: { v: Coverage; admin: boolean }) {
  const { t } = useTranslation();
  const [f, setF] = useState<BackupPolicy>(v.policy);
  const [rcpt, setRcpt] = useState(v.policy.recipients.join("\n"));
  useEffect(() => { setF(v.policy); setRcpt(v.policy.recipients.join("\n")); }, [v.policy]);
  const save = useAct(() => put(`api/protection/customers/${v.customer.id}/policy`, { ...f, recipients: rcpt.split(/[\s,;]+/).filter(Boolean) }), [["protection", v.customer.id], ["protection"]], t("prot.saved"));
  const names = useMemo(() => {
    const m = new Map<string, string>();
    v.guests.forEach((g) => m.set(g.key, guestLabel(g)));
    v.machines.forEach((x) => m.set(x.key, x.name));
    return m;
  }, [v]);
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card>
        <CardHeader icon={<ShieldCheck />} title={t("prot.policy")} description={t("prot.policy_hint")} />
        <CardBody className="space-y-4">
          {v.customer.sla && <p className="text-sm text-muted">{t("prot.customer_sla", { sla: v.customer.sla })}</p>}
          <Field label={t("prot.max_age")} hint={t("prot.max_age_hint")}><Input type="number" min={1} max={2160} value={f.max_age_hours} disabled={!admin} onChange={(e) => setF({ ...f, max_age_hours: Number(e.target.value) || 26 })} /></Field>
          <Switch checked={f.alert_failed} disabled={!admin} onCheckedChange={(x) => setF({ ...f, alert_failed: x })} label={t("prot.alert_failed")} description={t("prot.alert_failed_hint")} />
          <Switch checked={f.alert_missing} disabled={!admin} onCheckedChange={(x) => setF({ ...f, alert_missing: x })} label={t("prot.alert_missing")} description={t("prot.alert_missing_hint")} />
          <Switch checked={f.workstations} disabled={!admin} onCheckedChange={(x) => setF({ ...f, workstations: x })} label={t("prot.workstations")} description={t("prot.workstations_hint")} />
          <Field label={t("prot.recipients")} hint={t("prot.recipients_hint")}><Textarea rows={3} value={rcpt} disabled={!admin} onChange={(e) => setRcpt(e.target.value)} /></Field>
          {admin && <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(undefined)}><Save /> {t("common.save")}</Button>}
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<EyeOff />} title={t("prot.excluded")} description={t("prot.excluded_hint")} />
        <CardBody>
          {!f.exclude.length ? <p className="text-sm text-muted">{t("prot.none_excluded")}</p> : (
            <ul className="divide-y divide-line">
              {f.exclude.map((k) => (
                <li key={k} className="flex items-center justify-between py-2 text-sm">
                  <span>{names.get(k) ?? <span className="mono text-xs">{k}</span>}</span>
                  {admin && <Button size="sm" variant="ghost" onClick={() => setF({ ...f, exclude: f.exclude.filter((x) => x !== k) })}><Eye /> {t("prot.include")}</Button>}
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
