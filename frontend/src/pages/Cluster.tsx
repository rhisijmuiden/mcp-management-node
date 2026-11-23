import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HardDrive, Pencil, Play, Plus, Power, PowerOff, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import { toast } from "sonner";
import { JobView } from "@/components/jobs";
import { NodeSelect, tabCls, useNodeParam } from "@/components/net";
import { UsageBar } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { BackupJobPve, Guest, HaOverview, NodeJob, ReplicationJob, StorageRow } from "@/lib/types";
import { formatBytes, formatDate, formatDuration } from "@/lib/utils";

type Ctx = { nodeId: string; admin: boolean; clusterNodes: string[] };

export function ClusterPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "ha";
  const setTab = (v: string) => { const p = new URLSearchParams(params); p.set("tab", v); setParams(p, { replace: true }); };
  const { nodeId, setNodeId, node, nodes, isLoading } = useNodeParam();
  const clusterNodes = node?.info?.cluster?.nodes?.map((n) => n.name) ?? (node ? [node.name] : []);
  const ctx: Ctx = { nodeId, admin, clusterNodes };
  return (
    <div>
      <PageHeader title={t("cl.title")} description={t("cl.desc")}
        actions={<NodeSelect value={nodeId} onChange={setNodeId} nodes={nodes} />} />
      {isLoading && <Spinner />}
      {!isLoading && !nodes.length && <Card><EmptyState title={t("cl.no_nodes")}>{t("cl.no_nodes_hint")}</EmptyState></Card>}
      {node && (
        <>
          <div className="mb-4 text-sm text-muted">{node.cluster_name ? t("cl.cluster_of", { cluster: node.cluster_name, count: clusterNodes.length }) : t("cl.standalone")}{node.info?.cluster?.quorate === false && <Badge tone="offline" className="ml-2">{t("cl.no_quorum")}</Badge>}</div>
          <Tabs.Root value={tab} onValueChange={setTab}>
            <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
              <Tabs.Trigger value="ha" className={tabCls}>{t("cl.tab_ha")}</Tabs.Trigger>
              <Tabs.Trigger value="replication" className={tabCls}>{t("cl.tab_replication")}</Tabs.Trigger>
              <Tabs.Trigger value="backup" className={tabCls}>{t("cl.tab_backup")}</Tabs.Trigger>
              <Tabs.Trigger value="storage" className={tabCls}>{t("cl.tab_storage")}</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="ha"><HaTab {...ctx} /></Tabs.Content>
            <Tabs.Content value="replication"><ReplicationTab {...ctx} /></Tabs.Content>
            <Tabs.Content value="backup"><BackupJobsTab {...ctx} /></Tabs.Content>
            <Tabs.Content value="storage"><StorageTab {...ctx} /></Tabs.Content>
          </Tabs.Root>
        </>
      )}
    </div>
  );
}

function useGuestsOf(nodeId: string) {
  return useQuery({ queryKey: ["guests", "cluster-pick", nodeId], queryFn: () => get<Guest[]>("api/guests"), enabled: !!nodeId, staleTime: 30_000 });
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

/* ------------------------------------------------------------------ HA */
function HaTab({ nodeId, admin, clusterNodes }: Ctx) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["ha", nodeId], queryFn: () => get<HaOverview>(`api/cluster/${nodeId}/ha`), refetchInterval: 30_000 });
  const [addRes, setAddRes] = useState(false);
  const [addRule, setAddRule] = useState(false);
  const key = [["ha", nodeId]];
  const setState = useAct((v: { sid: string; state: string; group?: string }) => put(`api/cluster/${nodeId}/ha/resources/${encodeURIComponent(v.sid)}`, { sid: v.sid, state: v.state, group: v.group || null }), key, t("cl.saved"));
  const remove = useAct((sid: string) => del(`api/cluster/${nodeId}/ha/resources/${encodeURIComponent(sid)}`), key, t("cl.removed"));
  const delRule = useAct((v: { kind: string; id: string }) => del(`api/cluster/${nodeId}/ha/${v.kind}/${v.id}`), key, t("cl.removed"));
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const h = q.data!;
  const st = (sid: string) => h.status.find((s) => s.sid === sid);
  const manager = h.status.filter((s) => s.type === "master" || s.type === "quorum" || s.type === "lrm");
  return (
    <div className="space-y-5">
      {manager.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {manager.map((s) => <Badge key={s.id} tone={String(s.status ?? "").includes("active") || String(s.status ?? "") === "OK" ? "online" : "neutral"}>{s.type} {s.node ?? ""}: {String(s.status ?? "-")}</Badge>)}
        </div>
      )}
      <Card>
        <CardHeader title={t("cl.ha_resources")} description={t("cl.ha_resources_hint")} action={admin && <Button size="sm" onClick={() => setAddRes(true)}><Plus /> {t("cl.add")}</Button>} />
        {!h.resources.length ? <EmptyState title={t("cl.no_ha")} /> : (
          <Table>
            <thead><tr><Th>{t("cl.resource")}</Th><Th>{t("cl.state")}</Th><Th>{t("cl.current")}</Th><Th>{h.mode === "groups" ? t("cl.group") : t("cl.restarts")}</Th><Th /></tr></thead>
            <tbody>
              {h.resources.map((r) => (
                <Tr key={r.sid}>
                  <Td className="mono font-semibold">{r.sid}{r.comment && <div className="text-xs font-normal text-muted">{r.comment}</div>}</Td>
                  <Td>
                    {admin ? (
                      <Select value={r.state ?? "started"} onChange={(e) => setState.mutate({ sid: r.sid, state: e.target.value, group: r.group })} className="w-auto">
                        {["started", "stopped", "ignored", "disabled"].map((s) => <option key={s} value={s}>{t(`cl.ha_${s}`)}</option>)}
                      </Select>
                    ) : <Badge>{r.state}</Badge>}
                  </Td>
                  <Td>{st(r.sid) ? <span className="text-sm">{String(st(r.sid)?.state ?? st(r.sid)?.status ?? "-")} · {st(r.sid)?.node ?? ""}</span> : "-"}</Td>
                  <Td className="text-sm">{h.mode === "groups" ? r.group || "-" : `${r.max_restart ?? 1} / ${r.max_relocate ?? 1}`}</Td>
                  <Td className="text-right">{admin && <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => remove.mutate(r.sid)}><Trash2 /></Button>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Card>
        <CardHeader title={h.mode === "rules" ? t("cl.ha_rules") : t("cl.ha_groups")} description={h.mode === "rules" ? t("cl.ha_rules_hint") : t("cl.ha_groups_hint")}
          action={admin && <Button size="sm" onClick={() => setAddRule(true)}><Plus /> {t("cl.add")}</Button>} />
        {h.mode === "rules" ? (
          !h.rules.length ? <EmptyState title={t("cl.no_rules")} /> : (
            <Table>
              <thead><tr><Th>ID</Th><Th>{t("cl.type")}</Th><Th>{t("cl.resources")}</Th><Th>{t("cl.nodes")}</Th><Th /></tr></thead>
              <tbody>{h.rules.map((r) => (
                <Tr key={r.rule}><Td className="mono font-semibold">{r.rule}</Td><Td>{r.type}{r.affinity ? ` · ${r.affinity}` : ""}</Td><Td className="mono text-xs">{r.resources}</Td><Td className="mono text-xs">{r.nodes ?? "-"}</Td>
                  <Td className="text-right">{admin && <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => delRule.mutate({ kind: "rules", id: r.rule })}><Trash2 /></Button>}</Td></Tr>
              ))}</tbody>
            </Table>
          )
        ) : (
          !h.groups.length ? <EmptyState title={t("cl.no_groups")} /> : (
            <Table>
              <thead><tr><Th>{t("cl.group")}</Th><Th>{t("cl.nodes")}</Th><Th>{t("cl.restricted")}</Th><Th /></tr></thead>
              <tbody>{h.groups.map((g) => (
                <Tr key={g.group}><Td className="mono font-semibold">{g.group}</Td><Td className="mono text-xs">{g.nodes}</Td><Td>{g.restricted ? t("common.yes") : t("common.no")}</Td>
                  <Td className="text-right">{admin && <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => delRule.mutate({ kind: "groups", id: g.group })}><Trash2 /></Button>}</Td></Tr>
              ))}</tbody>
            </Table>
          )
        )}
      </Card>
      {addRes && <HaResourceDialog nodeId={nodeId} groups={h.groups.map((g) => g.group)} mode={h.mode} onClose={() => setAddRes(false)} />}
      {addRule && <HaRuleDialog nodeId={nodeId} mode={h.mode} clusterNodes={clusterNodes} resources={h.resources.map((r) => r.sid)} onClose={() => setAddRule(false)} />}
    </div>
  );
}

function GuestPicker({ nodeId, value, onChange, multiple }: { nodeId: string; value: number[]; onChange: (v: number[]) => void; multiple?: boolean }) {
  const { t } = useTranslation();
  const g = useGuestsOf(nodeId);
  const list = (g.data ?? []).slice().sort((a, b) => a.vmid - b.vmid);
  if (g.isLoading) return <Spinner />;
  if (!multiple) {
    return (
      <Select value={value[0] ?? ""} onChange={(e) => onChange(e.target.value ? [Number(e.target.value)] : [])}>
        <option value="">…</option>
        {list.map((x) => <option key={`${x.node_id}-${x.vmid}`} value={x.vmid}>{x.vmid} · {x.name} ({x.node})</option>)}
      </Select>
    );
  }
  return (
    <div className="max-h-52 space-y-1.5 overflow-auto rounded-md border border-line p-2">
      {!list.length && <div className="text-xs text-muted">{t("cl.no_guests")}</div>}
      {list.map((x) => (
        <Checkbox key={`${x.node_id}-${x.vmid}`} checked={value.includes(x.vmid)} label={`${x.vmid} · ${x.name} (${x.node})`}
          onCheckedChange={(v) => onChange(v ? [...value, x.vmid] : value.filter((y) => y !== x.vmid))} />
      ))}
    </div>
  );
}

function sidOf(vmid: number, guests: Guest[] | undefined) {
  const g = guests?.find((x) => x.vmid === vmid);
  return `${g?.type === "lxc" ? "ct" : "vm"}:${vmid}`;
}

function HaResourceDialog({ nodeId, groups, mode, onClose }: { nodeId: string; groups: string[]; mode: string; onClose: () => void }) {
  const { t } = useTranslation();
  const guests = useGuestsOf(nodeId);
  const [vmid, setVmid] = useState<number[]>([]);
  const [state, setState] = useState("started");
  const [group, setGroup] = useState("");
  const [maxRestart, setMaxRestart] = useState(1);
  const [maxRelocate, setMaxRelocate] = useState(1);
  const [comment, setComment] = useState("");
  const add = useAct(() => post(`api/cluster/${nodeId}/ha/resources`, { sid: sidOf(vmid[0], guests.data), state, group: group || null, max_restart: maxRestart, max_relocate: maxRelocate, comment }), [["ha", nodeId]], t("cl.saved"));
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("cl.add_ha")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!vmid.length} loading={add.isPending} onClick={() => add.mutate(undefined, { onSuccess: onClose })}>{t("cl.add")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("cl.guest")}><GuestPicker nodeId={nodeId} value={vmid} onChange={setVmid} /></Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("cl.state")}><Select value={state} onChange={(e) => setState(e.target.value)}>{["started", "stopped", "ignored", "disabled"].map((s) => <option key={s} value={s}>{t(`cl.ha_${s}`)}</option>)}</Select></Field>
          {mode === "groups" && <Field label={t("cl.group")}><Select value={group} onChange={(e) => setGroup(e.target.value)}><option value="">-</option>{groups.map((g) => <option key={g}>{g}</option>)}</Select></Field>}
          <Field label={t("cl.max_restart")}><Input type="number" min={0} max={10} value={maxRestart} onChange={(e) => setMaxRestart(Number(e.target.value))} /></Field>
          <Field label={t("cl.max_relocate")}><Input type="number" min={0} max={10} value={maxRelocate} onChange={(e) => setMaxRelocate(Number(e.target.value))} /></Field>
        </div>
        <Field label={t("cl.comment")}><Input value={comment} onChange={(e) => setComment(e.target.value)} /></Field>
      </div>
    </Dialog>
  );
}

function HaRuleDialog({ nodeId, mode, clusterNodes, resources, onClose }: { nodeId: string; mode: string; clusterNodes: string[]; resources: string[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [id, setId] = useState("");
  const [type, setType] = useState<"node-affinity" | "resource-affinity">("node-affinity");
  const [res, setRes] = useState<string[]>([]);
  const [prio, setPrio] = useState<Record<string, number>>({});
  const [affinity, setAffinity] = useState("positive");
  const [strict, setStrict] = useState(false);
  const [restricted, setRestricted] = useState(false);
  const add = useAct(() => mode === "rules"
    ? post(`api/cluster/${nodeId}/ha/rules`, { id, type, resources: res, nodes: type === "node-affinity" ? prio : {}, affinity, strict })
    : post(`api/cluster/${nodeId}/ha/groups`, { id, nodes: prio, restricted }), [["ha", nodeId]], t("cl.saved"));
  const togglePrio = (n: string, on: boolean) => setPrio((p) => { const c = { ...p }; if (on) c[n] = 1; else delete c[n]; return c; });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={mode === "rules" ? t("cl.add_rule") : t("cl.add_group")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(id) || (mode === "rules" && !res.length)} loading={add.isPending} onClick={() => add.mutate(undefined, { onSuccess: onClose })}>{t("cl.add")}</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="ID"><Input value={id} onChange={(e) => setId(e.target.value)} className="mono" /></Field>
          {mode === "rules" && <Field label={t("cl.type")}><Select value={type} onChange={(e) => setType(e.target.value as typeof type)}><option value="node-affinity">{t("cl.node_affinity")}</option><option value="resource-affinity">{t("cl.resource_affinity")}</option></Select></Field>}
        </div>
        {mode === "rules" && (
          <Field label={t("cl.resources")}>
            <div className="max-h-40 space-y-1.5 overflow-auto rounded-md border border-line p-2">
              {!resources.length && <div className="text-xs text-muted">{t("cl.no_ha")}</div>}
              {resources.map((r) => <Checkbox key={r} checked={res.includes(r)} label={r} onCheckedChange={(v) => setRes(v ? [...res, r] : res.filter((x) => x !== r))} />)}
            </div>
          </Field>
        )}
        {(mode === "groups" || type === "node-affinity") && (
          <Field label={t("cl.node_priority")} hint={t("cl.node_priority_hint")}>
            <div className="space-y-2">
              {clusterNodes.map((n) => (
                <div key={n} className="flex items-center gap-3">
                  <Checkbox checked={n in prio} label={n} onCheckedChange={(v) => togglePrio(n, v)} />
                  {n in prio && <Input type="number" min={0} max={1000} value={prio[n]} onChange={(e) => setPrio({ ...prio, [n]: Number(e.target.value) })} className="w-24" />}
                </div>
              ))}
            </div>
          </Field>
        )}
        {mode === "rules" && type === "resource-affinity" && <Field label={t("cl.affinity")}><Select value={affinity} onChange={(e) => setAffinity(e.target.value)}><option value="positive">{t("cl.together")}</option><option value="negative">{t("cl.apart")}</option></Select></Field>}
        {mode === "rules" ? <Checkbox checked={strict} onCheckedChange={setStrict} label={t("cl.strict")} /> : <Checkbox checked={restricted} onCheckedChange={setRestricted} label={t("cl.restricted")} />}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ replication */
function ReplicationTab({ nodeId, admin, clusterNodes }: Ctx) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["replication", nodeId], queryFn: () => get<ReplicationJob[]>(`api/cluster/${nodeId}/replication`), refetchInterval: 30_000 });
  const [add, setAdd] = useState(false);
  const key = [["replication", nodeId]];
  const runNow = useAct((id: string) => post(`api/cluster/${nodeId}/replication/${id}/run`), key, t("cl.started"));
  const remove = useAct((id: string) => del(`api/cluster/${nodeId}/replication/${id}`), key, t("cl.removed"));
  const ts = (s?: number) => (s ? formatDate(new Date(s * 1000).toISOString(), i18n.language) : "-");
  return (
    <Card>
      <CardHeader title={t("cl.tab_replication")} description={t("cl.replication_hint")} action={admin && <Button size="sm" onClick={() => setAdd(true)}><Plus /> {t("cl.add")}</Button>} />
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !q.data?.length ? <EmptyState title={t("cl.no_replication")} /> : (
        <Table>
          <thead><tr><Th>ID</Th><Th>{t("cl.route")}</Th><Th>{t("cl.schedule")}</Th><Th>{t("cl.last_sync")}</Th><Th>{t("cl.next_sync")}</Th><Th>{t("cl.state")}</Th><Th /></tr></thead>
          <tbody>
            {q.data.map((j) => (
              <Tr key={j.id}>
                <Td className="mono font-semibold">{j.id}{j.comment && <div className="text-xs font-normal text-muted">{j.comment}</div>}</Td>
                <Td className="text-sm">{j.source ?? "?"} → {j.target}{j.rate ? ` · ${j.rate} MB/s` : ""}</Td>
                <Td className="mono text-xs">{j.schedule ?? "*/15"}</Td>
                <Td className="text-sm">{ts(j.last_sync)}{j.duration ? <span className="text-xs text-muted"> · {formatDuration(j.duration)}</span> : null}</Td>
                <Td className="text-sm">{ts(j.next_sync)}</Td>
                <Td>{j.disable ? <Badge>{t("cl.disabled")}</Badge> : j.error || j.fail_count ? <Badge tone="offline">{j.error ?? t("cl.failed_n", { count: j.fail_count })}</Badge> : <Badge tone="online">{j.state ?? "OK"}</Badge>}</Td>
                <Td className="whitespace-nowrap text-right">
                  {admin && <>
                    <Button variant="ghost" size="icon" aria-label={t("cl.run_now")} onClick={() => runNow.mutate(j.id)}><Play /></Button>
                    <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => remove.mutate(j.id)}><Trash2 /></Button>
                  </>}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {add && <ReplicationDialog nodeId={nodeId} clusterNodes={clusterNodes} onClose={() => setAdd(false)} />}
    </Card>
  );
}

function ReplicationDialog({ nodeId, clusterNodes, onClose }: { nodeId: string; clusterNodes: string[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [vmid, setVmid] = useState<number[]>([]);
  const [target, setTarget] = useState("");
  const [schedule, setSchedule] = useState("*/15");
  const [rate, setRate] = useState("");
  const [comment, setComment] = useState("");
  const add = useAct(() => post(`api/cluster/${nodeId}/replication`, { vmid: vmid[0], target, schedule, rate: rate ? Number(rate) : null, comment }), [["replication", nodeId]], t("cl.saved"));
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("cl.add_replication")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!vmid.length || !target} loading={add.isPending} onClick={() => add.mutate(undefined, { onSuccess: onClose })}>{t("cl.add")}</Button></>}>
      <div className="space-y-4">
        <Alert tone="info">{t("cl.replication_zfs")}</Alert>
        <Field label={t("cl.guest")}><GuestPicker nodeId={nodeId} value={vmid} onChange={setVmid} /></Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("cl.target")}><Select value={target} onChange={(e) => setTarget(e.target.value)}><option value="">…</option>{clusterNodes.map((n) => <option key={n}>{n}</option>)}</Select></Field>
          <Field label={t("cl.schedule")} hint={t("cl.schedule_hint")}><Input value={schedule} onChange={(e) => setSchedule(e.target.value)} className="mono" /></Field>
          <Field label={t("cl.rate")}><Input type="number" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="MB/s" /></Field>
          <Field label={t("cl.comment")}><Input value={comment} onChange={(e) => setComment(e.target.value)} /></Field>
        </div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ backup jobs */
function BackupJobsTab({ nodeId, admin, clusterNodes }: Ctx) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["backup-jobs", nodeId], queryFn: () => get<{ jobs: BackupJobPve[] }>(`api/cluster/${nodeId}/backup-jobs`).then((r) => r.jobs ?? []) });
  const [edit, setEdit] = useState<BackupJobPve | "new" | null>(null);
  const [jobs, setJobs] = useState<NodeJob[]>([]);
  const key = [["backup-jobs", nodeId]];
  const runNow = useAct((id: string) => post<NodeJob[]>(`api/cluster/${nodeId}/backup-jobs/${id}/run`).then((r) => setJobs(r)), [], t("cl.started"));
  const toggle = useAct((j: BackupJobPve) => put(`api/cluster/${nodeId}/backup-jobs/${j.id}`, { ...jobToForm(j), enabled: !(j.enabled ?? 1) }), key, t("cl.saved"));
  const remove = useAct((id: string) => del(`api/cluster/${nodeId}/backup-jobs/${id}`), key, t("cl.removed"));
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader title={t("cl.tab_backup")} description={t("cl.backup_hint")} action={admin && <Button size="sm" onClick={() => setEdit("new")}><Plus /> {t("cl.add")}</Button>} />
        {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !q.data?.length ? <EmptyState title={t("cl.no_backup_jobs")} /> : (
          <Table>
            <thead><tr><Th>{t("cl.schedule")}</Th><Th>{t("cl.storage")}</Th><Th>{t("cl.selection")}</Th><Th>{t("cl.mode")}</Th><Th>{t("cl.next_run")}</Th><Th>{t("cl.state")}</Th><Th /></tr></thead>
            <tbody>
              {q.data.map((j) => (
                <Tr key={j.id}>
                  <Td className="mono text-sm font-semibold">{j.schedule}{j.comment && <div className="text-xs font-normal text-muted">{j.comment}</div>}</Td>
                  <Td>{j.storage}</Td>
                  <Td className="text-sm">{j.all ? t("cl.all_guests") : <span className="mono text-xs">{j.vmid}</span>}{j.node ? ` · ${j.node}` : ""}</Td>
                  <Td className="text-sm">{j.mode ?? "snapshot"} · {j.compress ?? "zstd"}</Td>
                  <Td className="text-sm">{j["next-run"] ? formatDate(new Date(j["next-run"] * 1000).toISOString(), i18n.language) : "-"}</Td>
                  <Td>{(j.enabled ?? 1) ? <Badge tone="online">{t("cl.enabled")}</Badge> : <Badge>{t("cl.disabled")}</Badge>}</Td>
                  <Td className="whitespace-nowrap text-right">
                    {admin && <>
                      <Button variant="ghost" size="icon" aria-label={t("cl.run_now")} onClick={() => runNow.mutate(j.id)} disabled={runNow.isPending}><Play /></Button>
                      <Button variant="ghost" size="icon" aria-label={(j.enabled ?? 1) ? t("cl.disable") : t("cl.enable")} onClick={() => toggle.mutate(j)}>{(j.enabled ?? 1) ? <PowerOff /> : <Power />}</Button>
                      <Button variant="ghost" size="icon" aria-label={t("common.edit")} onClick={() => setEdit(j)}><Pencil /></Button>
                      <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => remove.mutate(j.id)}><Trash2 /></Button>
                    </>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {jobs.map((j) => (
        <Card key={j.id}><CardHeader title={t("cl.backup_running")} action={<Button size="sm" onClick={() => setJobs(jobs.filter((x) => x.id !== j.id))}>{t("common.close")}</Button>} /><CardBody><JobView jobId={j.id} /></CardBody></Card>
      ))}
      {edit && <BackupJobDialog nodeId={nodeId} job={edit === "new" ? null : edit} clusterNodes={clusterNodes} onClose={() => setEdit(null)} />}
    </div>
  );
}

function parsePrune(p: BackupJobPve["prune-backups"]): Record<string, number> {
  const out: Record<string, number> = {};
  const src = typeof p === "string" ? Object.fromEntries(p.split(",").map((x) => x.split("="))) : p ?? {};
  for (const [k, v] of Object.entries(src)) out[k] = Number(v);
  return out;
}

function jobToForm(j: BackupJobPve | null) {
  const prune = parsePrune(j?.["prune-backups"]);
  return {
    schedule: j?.schedule ?? "sun 01:00",
    storage: j?.storage ?? "",
    all: !!j?.all || !j,
    vmids: j?.vmid ? j.vmid.split(",").map(Number).filter(Boolean) : [],
    node: j?.node ?? null,
    mode: (j?.mode ?? "snapshot") as "snapshot" | "suspend" | "stop",
    compress: (j?.compress ?? "zstd") as "zstd" | "lzo" | "gzip" | "0",
    enabled: (j?.enabled ?? 1) !== 0,
    mailto: j?.mailto ? j.mailto.split(/[,;\s]+/).filter(Boolean) : [],
    keep_last: (prune["keep-last"] ?? null) as number | null,
    keep_daily: (prune["keep-daily"] ?? (j ? null : 7)) as number | null,
    keep_weekly: (prune["keep-weekly"] ?? (j ? null : 4)) as number | null,
    keep_monthly: (prune["keep-monthly"] ?? null) as number | null,
    comment: j?.comment ?? "",
  };
}

function BackupJobDialog({ nodeId, job, clusterNodes, onClose }: { nodeId: string; job: BackupJobPve | null; clusterNodes: string[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [f, setF] = useState(() => jobToForm(job));
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });
  const st = useQuery({ queryKey: ["storage", nodeId], queryFn: () => get<{ storages: StorageRow[] }>(`api/cluster/${nodeId}/storage`).then((r) => r.storages ?? []) });
  const backupStores = (st.data ?? []).filter((s) => s.content.split(",").includes("backup"));
  const save = useAct(() => (job ? put(`api/cluster/${nodeId}/backup-jobs/${job.id}`, f) : post(`api/cluster/${nodeId}/backup-jobs`, f)), [["backup-jobs", nodeId]], t("cl.saved"));
  const num = (v: string) => (v === "" ? null : Number(v));
  const val = (v: number | null) => v ?? "";
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={job ? t("cl.edit_backup_job") : t("cl.add_backup_job")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!f.storage || (!f.all && !f.vmids.length)} loading={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: onClose })}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("cl.schedule")} hint={t("cl.backup_schedule_hint")}><Input value={f.schedule} onChange={(e) => set({ schedule: e.target.value })} className="mono" /></Field>
          <Field label={t("cl.storage")}>
            <Select value={f.storage} onChange={(e) => set({ storage: e.target.value })}>
              <option value="">…</option>
              {backupStores.map((s) => <option key={s.storage} value={s.storage}>{s.storage} ({s.type})</option>)}
              {f.storage && !backupStores.some((s) => s.storage === f.storage) && <option value={f.storage}>{f.storage}</option>}
            </Select>
          </Field>
          <Field label={t("cl.mode")}><Select value={f.mode} onChange={(e) => set({ mode: e.target.value as typeof f.mode })}>{["snapshot", "suspend", "stop"].map((m) => <option key={m} value={m}>{t(`cl.mode_${m}`)}</option>)}</Select></Field>
          <Field label={t("cl.compress")}><Select value={f.compress} onChange={(e) => set({ compress: e.target.value as typeof f.compress })}>{["zstd", "lzo", "gzip", "0"].map((m) => <option key={m} value={m}>{m === "0" ? t("cl.none") : m}</option>)}</Select></Field>
          <Field label={t("cl.only_node")}><Select value={f.node ?? ""} onChange={(e) => set({ node: e.target.value || null })}><option value="">{t("cl.all_nodes")}</option>{clusterNodes.map((n) => <option key={n}>{n}</option>)}</Select></Field>
          <Field label={t("cl.mailto")}><Input value={f.mailto.join(", ")} onChange={(e) => set({ mailto: e.target.value.split(/[,;\s]+/).filter(Boolean) })} /></Field>
        </div>
        <Switch checked={f.all} onCheckedChange={(v) => set({ all: v })} label={t("cl.all_guests")} />
        {!f.all && <Field label={t("cl.guests")}><GuestPicker nodeId={nodeId} value={f.vmids} onChange={(v) => set({ vmids: v })} multiple /></Field>}
        <div>
          <div className="mb-2 text-[13px] font-semibold">{t("cl.retention")}</div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label={t("cl.keep_last")}><Input type="number" min={0} value={val(f.keep_last)} onChange={(e) => set({ keep_last: num(e.target.value) })} /></Field>
            <Field label={t("cl.keep_daily")}><Input type="number" min={0} value={val(f.keep_daily)} onChange={(e) => set({ keep_daily: num(e.target.value) })} /></Field>
            <Field label={t("cl.keep_weekly")}><Input type="number" min={0} value={val(f.keep_weekly)} onChange={(e) => set({ keep_weekly: num(e.target.value) })} /></Field>
            <Field label={t("cl.keep_monthly")}><Input type="number" min={0} value={val(f.keep_monthly)} onChange={(e) => set({ keep_monthly: num(e.target.value) })} /></Field>
          </div>
        </div>
        <Field label={t("cl.comment")}><Input value={f.comment} onChange={(e) => set({ comment: e.target.value })} /></Field>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ storage */
function StorageTab({ nodeId, admin, clusterNodes }: Ctx) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["storage", nodeId], queryFn: () => get<{ storages: StorageRow[] }>(`api/cluster/${nodeId}/storage`).then((r) => r.storages ?? []), refetchInterval: 60_000 });
  const [add, setAdd] = useState(false);
  const key = [["storage", nodeId]];
  const enable = useAct((v: { s: string; on: boolean }) => put(`api/cluster/${nodeId}/storage/${v.s}`, { enabled: v.on }), key, t("cl.saved"));
  const remove = useAct((s: string) => del(`api/cluster/${nodeId}/storage/${s}`), key, t("cl.removed"));
  return (
    <Card>
      <CardHeader title={t("cl.tab_storage")} description={t("cl.storage_hint")} action={admin && <Button size="sm" onClick={() => setAdd(true)}><Plus /> {t("cl.add_storage")}</Button>} />
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !q.data?.length ? <EmptyState icon={<HardDrive />} title={t("cl.no_storage")} /> : (
        <Table>
          <thead><tr><Th>{t("cl.storage")}</Th><Th>{t("cl.type")}</Th><Th>{t("cl.content")}</Th><Th>{t("cl.usage")}</Th><Th>{t("cl.state")}</Th><Th /></tr></thead>
          <tbody>
            {q.data.map((s) => (
              <Tr key={s.storage}>
                <Td className="font-semibold">{s.storage}<div className="mono text-xs font-normal text-muted">{s.path ?? (s.server ? `${s.server}:${s.export ?? s.share ?? s.datastore ?? ""}` : s.pool ?? "")}</div></Td>
                <Td>{s.type}{s.shared && <Badge tone="info" className="ml-1.5">{t("cl.shared")}</Badge>}</Td>
                <Td className="text-xs">{s.content.split(",").join(", ")}</Td>
                <Td className="min-w-48">{s.total ? <UsageBar label="" used={s.used} total={s.total} detail={`${formatBytes(s.used)} / ${formatBytes(s.total)}`} /> : "-"}</Td>
                <Td>{s.disabled ? <Badge>{t("cl.disabled")}</Badge> : s.active ? <Badge tone="online">{t("cl.active")}</Badge> : <Badge tone="warning">{t("cl.inactive")}</Badge>}</Td>
                <Td className="whitespace-nowrap text-right">
                  {admin && <>
                    <Button variant="ghost" size="icon" aria-label={s.disabled ? t("cl.enable") : t("cl.disable")} onClick={() => enable.mutate({ s: s.storage, on: s.disabled })}>{s.disabled ? <Power /> : <PowerOff />}</Button>
                    {!["local", "local-lvm", "local-zfs"].includes(s.storage) && <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => remove.mutate(s.storage)}><Trash2 /></Button>}
                  </>}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {add && <StorageDialog nodeId={nodeId} clusterNodes={clusterNodes} onClose={() => setAdd(false)} />}
    </Card>
  );
}

const CONTENT = ["images", "rootdir", "backup", "iso", "vztmpl", "snippets", "import"];

function StorageDialog({ nodeId, clusterNodes, onClose }: { nodeId: string; clusterNodes: string[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [f, setF] = useState({ storage: "", type: "nfs" as "dir" | "nfs" | "cifs" | "pbs", content: ["backup"], nodes: [] as string[], path: "", server: "", export: "", share: "", username: "", password: "", domain: "", datastore: "", namespace: "", fingerprint: "", shared: false });
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });
  const body = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v === "" ? null : v]));
  const add = useAct(() => post(`api/cluster/${nodeId}/storage`, body), [["storage", nodeId]], t("cl.saved"));
  const contentFor = f.type === "pbs" ? ["backup"] : CONTENT;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={t("cl.add_storage")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!f.storage} loading={add.isPending} onClick={() => add.mutate(undefined, { onSuccess: onClose })}>{t("cl.add")}</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="ID"><Input value={f.storage} onChange={(e) => set({ storage: e.target.value })} className="mono" /></Field>
          <Field label={t("cl.type")}>
            <Select value={f.type} onChange={(e) => { const ty = e.target.value as typeof f.type; set({ type: ty, content: ty === "pbs" ? ["backup"] : f.content }); }}>
              <option value="nfs">NFS</option><option value="cifs">SMB/CIFS</option><option value="pbs">Proxmox Backup Server</option><option value="dir">{t("cl.directory")}</option>
            </Select>
          </Field>
          {f.type === "dir" && <Field label={t("cl.path")}><Input value={f.path} onChange={(e) => set({ path: e.target.value })} className="mono" placeholder="/mnt/data" /></Field>}
          {f.type !== "dir" && <Field label={t("cl.server")}><Input value={f.server} onChange={(e) => set({ server: e.target.value })} className="mono" /></Field>}
          {f.type === "nfs" && <Field label={t("cl.export")}><Input value={f.export} onChange={(e) => set({ export: e.target.value })} className="mono" placeholder="/export/pve" /></Field>}
          {f.type === "cifs" && <Field label={t("cl.share")}><Input value={f.share} onChange={(e) => set({ share: e.target.value })} /></Field>}
          {f.type === "pbs" && <Field label={t("cl.datastore")}><Input value={f.datastore} onChange={(e) => set({ datastore: e.target.value })} /></Field>}
          {(f.type === "cifs" || f.type === "pbs") && <>
            <Field label={t("cl.username")}><Input value={f.username} onChange={(e) => set({ username: e.target.value })} placeholder={f.type === "pbs" ? "backup@pbs" : ""} autoComplete="off" /></Field>
            <Field label={t("cl.password")}><Input type="password" value={f.password} onChange={(e) => set({ password: e.target.value })} autoComplete="new-password" /></Field>
          </>}
          {f.type === "cifs" && <Field label={t("cl.domain")}><Input value={f.domain} onChange={(e) => set({ domain: e.target.value })} /></Field>}
          {f.type === "pbs" && <>
            <Field label={t("cl.namespace")}><Input value={f.namespace} onChange={(e) => set({ namespace: e.target.value })} /></Field>
            <Field label={t("cl.fingerprint")} className="sm:col-span-2"><Input value={f.fingerprint} onChange={(e) => set({ fingerprint: e.target.value })} className="mono" /></Field>
          </>}
        </div>
        <Field label={t("cl.content")}>
          <div className="flex flex-wrap gap-4">
            {contentFor.map((c) => <Checkbox key={c} checked={f.content.includes(c)} label={c} onCheckedChange={(v) => set({ content: v ? [...f.content, c] : f.content.filter((x) => x !== c) })} />)}
          </div>
        </Field>
        <Field label={t("cl.nodes")} hint={t("cl.storage_nodes_hint")}>
          <div className="flex flex-wrap gap-4">
            {clusterNodes.map((n) => <Checkbox key={n} checked={f.nodes.includes(n)} label={n} onCheckedChange={(v) => set({ nodes: v ? [...f.nodes, n] : f.nodes.filter((x) => x !== n) })} />)}
          </div>
        </Field>
        {f.type === "dir" && <Checkbox checked={f.shared} onCheckedChange={(v) => set({ shared: v })} label={t("cl.shared")} />}
      </div>
    </Dialog>
  );
}
