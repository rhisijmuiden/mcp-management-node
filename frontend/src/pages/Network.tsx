import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, GitCompareArrows, Network as NetworkIcon, Pencil, Plus, RefreshCw, RotateCcw, Shield, ShieldCheck, Trash2, Undo2, Wand2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { GuestIcon } from "@/components/guests";
import { JobView } from "@/components/jobs";
import { ApiErrors, DiffView, NodeSelect, TypeToConfirm, tabCls, useNodeParam, usePveNodes, vlanColor } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { BridgeMap, ConfigBackup, FirewallOverview, FwRule, NetIface, NetPending, SdnDesign, SdnState } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

export function NetworkPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const tab = params.get("tab") ?? "map";
  const setTab = (v: string) => { const p = new URLSearchParams(params); p.set("tab", v); setParams(p, { replace: true }); };
  return (
    <div>
      <PageHeader title={t("net.title")} description={t("net.desc")}
        actions={admin && <Link to="/proxmox/network/sdn-designer"><Button variant="primary"><Wand2 /> {t("net.open_designer")}</Button></Link>} />
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="map" className={tabCls}>{t("net.tab_map")}</Tabs.Trigger>
          <Tabs.Trigger value="interfaces" className={tabCls}>{t("net.tab_interfaces")}</Tabs.Trigger>
          {admin && <Tabs.Trigger value="firewall" className={tabCls}>{t("net.tab_firewall")}</Tabs.Trigger>}
          <Tabs.Trigger value="sdn" className={tabCls}>SDN</Tabs.Trigger>
          {admin && <Tabs.Trigger value="backups" className={tabCls}>{t("net.tab_backups")}</Tabs.Trigger>}
        </Tabs.List>
        <Tabs.Content value="map"><MapTab /></Tabs.Content>
        <Tabs.Content value="interfaces"><InterfacesTab admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="firewall"><FirewallTab /></Tabs.Content>}
        <Tabs.Content value="sdn"><SdnTab admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="backups"><BackupsTab /></Tabs.Content>}
      </Tabs.Root>
    </div>
  );
}

/* ------------------------------------------------------------------ bridges and VLANs map */
function MapTab() {
  const { t } = useTranslation();
  const { list } = usePveNodes();
  const [node, setNode] = useState("");
  const [tag, setTag] = useState<number | "all">("all");
  const map = useQuery({
    queryKey: ["net-map", node],
    queryFn: () => get<BridgeMap>(`api/network/map${node ? `?node_id=${node}` : ""}`),
    refetchInterval: 60_000,
  });
  const m = map.data;
  const total = m?.tags.reduce((a, x) => a + x.count, 0) ?? 0;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={node} onChange={(e) => setNode(e.target.value)} className="w-auto min-w-48">
          <option value="">{t("net.all_nodes")}</option>
          {list.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
        </Select>
        <div className="flex flex-wrap gap-2" role="group" aria-label="VLAN">
          <VlanChip active={tag === "all"} onClick={() => setTag("all")} color="var(--line)" label={t("net.all_vlans")} count={total} />
          {m?.tags.map((x) => (
            <VlanChip key={x.tag} active={tag === x.tag} onClick={() => setTag(x.tag)} color={vlanColor(x.tag)} label={x.tag ? `VLAN ${x.tag}` : t("net.untagged")} count={x.count} />
          ))}
        </div>
        <div className="flex-1" />
        <Button size="sm" onClick={() => map.refetch()} loading={map.isFetching}><RefreshCw /> {t("common.refresh")}</Button>
      </div>
      {m && <ApiErrors items={m.errors} />}
      {map.isLoading && <Spinner />}
      {map.isError && <Alert tone="offline">{errorMessage(map.error, t)}</Alert>}
      {m && !m.bridges.length && <Card><EmptyState icon={<NetworkIcon />} title={t("net.no_bridges")}>{t("net.no_bridges_hint")}</EmptyState></Card>}
      {m?.bridges.map((b) => {
        const lanes = b.lanes.filter((l) => tag === "all" || l.tag === tag);
        if (tag !== "all" && !lanes.length) return null;
        const ports = [...new Set(b.nodes.flatMap((n) => n.ports))];
        return (
          <Card key={b.name} className="p-5">
            <div className="flex flex-wrap items-center gap-3">
              {b.kind === "bridge" && (
                <span className="inline-flex items-center gap-2 rounded-md bg-sunken px-3 py-1.5 text-[13px] font-semibold">
                  <NetworkIcon className="size-4" /> {ports.length ? ports.join(" · ") : t("net.no_ports")}
                </span>
              )}
              {b.kind === "bridge" && <span className="h-0.5 w-8 bg-ink" aria-hidden />}
              <div className="min-w-0">
                <h2 className="display text-xl leading-7">{b.name}</h2>
                <div className="text-xs text-muted">
                  {b.kind === "vnet" ? t("net.sdn_vnet", { tag: b.vnet_tag ?? "-" }) : b.kind === "missing" ? t("net.missing_bridge") : b.comments || b.nodes.map((n) => n.node + (n.cidr ? ` ${n.cidr}` : "")).join(" · ")}
                </div>
              </div>
              <div className="flex-1" />
              {b.kind === "bridge" && <Badge tone={b.vlan_aware ? "online" : "neutral"}>{b.vlan_aware ? t("net.vlan_aware") : t("net.not_vlan_aware")}</Badge>}
              {b.kind === "missing" && <Badge tone="offline">{t("net.missing")}</Badge>}
              <span className="text-xs text-muted">{t("net.interfaces_n", { count: b.interfaces })}</span>
            </div>
            <div className="ml-2 mt-4 space-y-2.5 border-l-2 border-ink pl-5">
              {!lanes.length && <div className="text-xs text-muted">{t("net.no_guests_on_bridge")}</div>}
              {lanes.map((ln) => (
                <div key={ln.tag} className="flex flex-wrap items-stretch gap-3">
                  <div className="flex w-[160px] shrink-0 flex-col justify-center rounded-md border bg-surface px-3 py-2" style={{ borderColor: vlanColor(ln.tag) }}>
                    <span className="flex items-center gap-1.5 text-sm font-bold"><span className="size-2.5 rounded-[3px]" style={{ background: vlanColor(ln.tag) }} />{ln.tag ? `VLAN ${ln.tag}` : t("net.untagged")}</span>
                    <span className="text-xs text-muted">{t("net.guests_n", { count: ln.guests.length })}</span>
                  </div>
                  <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                    {ln.guests.map((g) => (
                      <Link key={`${g.node_id}-${g.vmid}-${g.nic}`} to={`/proxmox/guests/${g.node_id}/${g.vmid}`}
                        className="flex items-center gap-2 rounded-md border border-line bg-raised px-2.5 py-1.5 text-sm hover:border-ink/40">
                        <span className={cn("size-2 rounded-full", g.status === "running" ? "bg-online" : "bg-muted/50")} aria-hidden />
                        <GuestIcon type={g.type} className="size-3.5" />
                        <span className="font-bold">{g.vmid}</span>
                        <span>{g.name}</span>
                        <span className="text-xs text-muted">{g.nic} · {g.node}{g.trunks.length ? ` · trunks ${g.trunks.join(",")}` : ""}</span>
                        {g.firewall && <ShieldCheck className="size-3.5 text-online" aria-label={t("net.fw_on_nic")} />}
                        {g.link_down && <Badge tone="warning">{t("net.link_down")}</Badge>}
                      </Link>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        );
      })}
      <div className="flex flex-wrap gap-4 text-xs text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full bg-online" />{t("px.running")}</span>
        <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full bg-muted/50" />{t("px.stopped")}</span>
        <span className="inline-flex items-center gap-1.5"><ShieldCheck className="size-3.5 text-online" />{t("net.fw_on_nic")}</span>
      </div>
    </div>
  );
}

function VlanChip({ active, onClick, color, label, count }: { active: boolean; onClick: () => void; color: string; label: string; count: number }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={active}
      className={cn("inline-flex h-9 items-center gap-2 rounded-full bg-raised px-3.5 text-[13px] font-semibold", active ? "border-2 border-ink" : "border border-line")}>
      <span className="size-2.5 rounded-[3px]" style={{ background: color }} />{label}<span className="text-xs text-muted">{count}</span>
    </button>
  );
}

/* ------------------------------------------------------------------ interfaces, bridge editor, safe apply */
interface BridgeForm { iface: string; ports: string; vlan_aware: boolean; vids: string; cidr: string; gateway: string; mtu: string; comments: string; autostart: boolean }

function InterfacesTab({ admin }: { admin: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { nodeId, setNodeId, node, nodes } = useNodeParam();
  const ifs = useQuery({ queryKey: ["net-ifs", nodeId], queryFn: () => get<NetIface[]>(`api/network/${nodeId}/interfaces`), enabled: !!nodeId && !!node?.online });
  const pending = useQuery({ queryKey: ["net-pending", nodeId], queryFn: () => get<NetPending>(`api/network/${nodeId}/pending`), enabled: admin && !!nodeId && !!node?.online });
  const [edit, setEdit] = useState<{ form: BridgeForm; create: boolean } | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["net-ifs", nodeId] }); qc.invalidateQueries({ queryKey: ["net-pending", nodeId] }); qc.invalidateQueries({ queryKey: ["net-map"] }); };
  const remove = useMutation({ mutationFn: (iface: string) => del<NetPending>(`api/network/${nodeId}/bridges/${iface}`), onSuccess: refresh, onError: (e) => toast.error(errorMessage(e, t)) });
  const revert = useMutation({ mutationFn: () => post<NetPending>(`api/network/${nodeId}/revert`), onSuccess: refresh, onError: (e) => toast.error(errorMessage(e, t)) });
  const apply = useMutation({
    mutationFn: (typed: string) => post<NetPending>(`api/network/${nodeId}/apply`, { dry_run: false, confirm: typed }),
    onSuccess: (r) => { setConfirm(false); if (r.job) setJobId(r.job.id); refresh(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const p = pending.data;
  const eth = (ifs.data ?? []).filter((i) => ["eth", "bond", "vlan"].includes(i.type)).map((i) => i.iface);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <NodeSelect value={nodeId} onChange={setNodeId} nodes={nodes} />
        <div className="flex-1" />
        {admin && <Button onClick={() => setEdit({ create: true, form: { iface: nextBridge(ifs.data ?? []), ports: "", vlan_aware: true, vids: "2-4094", cidr: "", gateway: "", mtu: "", comments: "", autostart: true } })} disabled={!node?.online}><Plus /> {t("net.new_bridge")}</Button>}
      </div>
      {node && !node.online && <Alert tone="warning">{t("net.node_offline")}</Alert>}
      {p?.has_changes && (
        <Card className="border-warning">
          <CardHeader icon={<GitCompareArrows />} title={t("net.pending_title")} description={t("net.pending_desc")}
            action={<>
              <Button size="sm" onClick={() => revert.mutate()} loading={revert.isPending}><Undo2 /> {t("net.discard")}</Button>
              <Button size="sm" variant="primary" onClick={() => setConfirm(true)}>{t("net.apply")}</Button>
            </>} />
          <CardBody className="space-y-3">
            {p.warnings.map((w) => <Alert key={w} tone="warning">{w}</Alert>)}
            <DiffView diff={p.diff} />
          </CardBody>
        </Card>
      )}
      {jobId && <Card><CardHeader title={t("net.apply_running")} action={<Button size="sm" onClick={() => setJobId(null)}>{t("common.close")}</Button>} /><CardBody><JobView jobId={jobId} onDone={refresh} /></CardBody></Card>}
      <Card>
        {ifs.isLoading ? <CardBody><Spinner /></CardBody> : ifs.isError ? <CardBody><Alert tone="offline">{errorMessage(ifs.error, t)}</Alert></CardBody> : (
          <Table>
            <thead><Tr><Th>{t("net.iface")}</Th><Th>{t("net.type")}</Th><Th>{t("net.ports")}</Th><Th>{t("net.address")}</Th><Th>VLAN</Th><Th>{t("net.comment")}</Th><Th /></Tr></thead>
            <tbody>
              {(ifs.data ?? []).map((i) => (
                <Tr key={i.iface}>
                  <Td className="font-semibold"><span className={cn("mr-2 inline-block size-2 rounded-full", i.active ? "bg-online" : "bg-muted/40")} />{i.iface}{i.pending && <Badge tone="warning" className="ml-2">{t("net.pending")}</Badge>}</Td>
                  <Td>{i.type}{i.bond_mode ? ` (${i.bond_mode})` : ""}</Td>
                  <Td className="mono text-xs">{i.ports.join(" ") || "-"}</Td>
                  <Td className="mono text-xs">{i.cidr ?? "-"}{i.gateway ? <div className="text-muted">gw {i.gateway}</div> : null}</Td>
                  <Td>{i.type === "bridge" ? (i.vlan_aware ? <Badge tone="online">{i.vids || "aware"}</Badge> : "-") : i.vlan_id ?? "-"}</Td>
                  <Td className="text-xs text-muted">{i.comments}</Td>
                  <Td className="text-right">
                    {admin && i.type === "bridge" && (
                      <div className="flex justify-end gap-1">
                        <Button size="icon" variant="ghost" aria-label={t("common.edit")} onClick={() => setEdit({ create: false, form: { iface: i.iface, ports: i.ports.join(" "), vlan_aware: i.vlan_aware, vids: i.vids ?? "", cidr: i.cidr ?? "", gateway: i.gateway ?? "", mtu: i.mtu ? String(i.mtu) : "", comments: i.comments, autostart: i.autostart } })}><Pencil /></Button>
                        <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => remove.mutate(i.iface)}><Trash2 /></Button>
                      </div>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {edit && <BridgeDialog nodeId={nodeId} create={edit.create} initial={edit.form} ports={eth} onClose={() => setEdit(null)} onSaved={refresh} />}
      <TypeToConfirm open={confirm} onOpenChange={setConfirm} word={node?.name ?? ""} title={t("net.apply_title")} action={t("net.apply")} loading={apply.isPending} onConfirm={(w) => apply.mutate(w)}>
        <Alert tone="info">{t("net.apply_explain")}</Alert>
        {p?.warnings.map((w) => <Alert key={w} tone="warning">{w}</Alert>)}
      </TypeToConfirm>
    </div>
  );
}

function nextBridge(ifs: NetIface[]): string {
  const used = new Set(ifs.map((i) => i.iface));
  for (let n = 0; n < 100; n++) if (!used.has(`vmbr${n}`)) return `vmbr${n}`;
  return "vmbr99";
}

function BridgeDialog({ nodeId, create, initial, ports, onClose, onSaved }: { nodeId: string; create: boolean; initial: BridgeForm; ports: string[]; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [f, setF] = useState(initial);
  const set = (p: Partial<BridgeForm>) => setF((o) => ({ ...o, ...p }));
  const body = () => ({ iface: f.iface, ports: f.ports.split(/[\s,]+/).filter(Boolean), vlan_aware: f.vlan_aware, vids: f.vlan_aware && f.vids ? f.vids : null, cidr: f.cidr || null, gateway: f.gateway || null, mtu: f.mtu ? Number(f.mtu) : null, comments: f.comments, autostart: f.autostart });
  const save = useMutation({
    mutationFn: () => (create ? post<NetPending>(`api/network/${nodeId}/bridges`, body()) : put<NetPending>(`api/network/${nodeId}/bridges/${f.iface}`, body())),
    onSuccess: () => { toast.success(t("net.staged")); onSaved(); onClose(); },
  });
  const chosen = f.ports.split(/[\s,]+/).filter(Boolean);
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={create ? t("net.new_bridge") : t("net.edit_bridge", { name: f.iface })} description={t("net.bridge_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("net.stage")}</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("net.iface")}><Input value={f.iface} disabled={!create} onChange={(e) => set({ iface: e.target.value.trim() })} className="mono" /></Field>
          <Field label={t("net.ports")} hint={t("net.ports_hint")}><Input value={f.ports} onChange={(e) => set({ ports: e.target.value })} className="mono" placeholder="eno1" /></Field>
        </div>
        {ports.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {ports.map((p) => (
              <button key={p} type="button" onClick={() => set({ ports: (chosen.includes(p) ? chosen.filter((x) => x !== p) : [...chosen, p]).join(" ") })}
                className={cn("rounded-full border px-3 py-1 text-xs font-semibold", chosen.includes(p) ? "border-ink bg-ink text-raised" : "border-line")}>{p}</button>
            ))}
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t("net.cidr")} hint={t("net.cidr_hint")}><Input value={f.cidr} onChange={(e) => set({ cidr: e.target.value.trim() })} className="mono" placeholder="10.0.30.1/24" /></Field>
          <Field label={t("net.gateway")}><Input value={f.gateway} onChange={(e) => set({ gateway: e.target.value.trim() })} className="mono" /></Field>
          <Field label="MTU"><Input type="number" value={f.mtu} onChange={(e) => set({ mtu: e.target.value })} placeholder="1500" /></Field>
        </div>
        <div className="grid items-end gap-3 sm:grid-cols-2">
          <Switch checked={f.vlan_aware} onCheckedChange={(v) => set({ vlan_aware: v })} label={t("net.vlan_aware")} description={t("net.vlan_aware_hint")} />
          {f.vlan_aware && <Field label={t("net.vids")}><Input value={f.vids} onChange={(e) => set({ vids: e.target.value })} className="mono" /></Field>}
        </div>
        <Field label={t("net.comment")}><Input value={f.comments} onChange={(e) => set({ comments: e.target.value })} /></Field>
        <Checkbox checked={f.autostart} onCheckedChange={(v) => set({ autostart: v })} label={t("net.autostart")} />
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
        <Alert tone="info">{t("net.stage_hint")}</Alert>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ firewall */
const isErr = (x: unknown): x is { error: string } => !!x && typeof x === "object" && !Array.isArray(x) && "error" in (x as object);

function FirewallTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { nodeId, setNodeId, node, nodes } = useNodeParam();
  const fw = useQuery({ queryKey: ["fw", nodeId], queryFn: () => get<FirewallOverview>(`api/firewall/${nodeId}`), enabled: !!nodeId && !!node?.online });
  const [rule, setRule] = useState<{ level: string; type?: string; vmid?: number; group?: string } | null>(null);
  const [guest, setGuest] = useState<{ type: "qemu" | "lxc"; vmid: number; name: string } | null>(null);
  const [clusterToggle, setClusterToggle] = useState<boolean | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["fw", nodeId] });
  const opts = useMutation({ mutationFn: (b: Record<string, unknown>) => put(`api/firewall/${nodeId}/options`, b), onSuccess: () => { setClusterToggle(null); refresh(); }, onError: (e) => toast.error(errorMessage(e, t)) });
  const delRule = useMutation({ mutationFn: (b: Record<string, unknown>) => post(`api/firewall/${nodeId}/rules/delete`, b), onSuccess: refresh, onError: (e) => toast.error(errorMessage(e, t)) });
  const f = fw.data;
  const clusterOn = !!f?.cluster_options?.enable;
  const nodeOn = f?.node_options?.enable !== 0;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3"><NodeSelect value={nodeId} onChange={setNodeId} nodes={nodes} /></div>
      {fw.isLoading && <Spinner />}
      {fw.isError && <Alert tone="offline">{errorMessage(fw.error, t)}</Alert>}
      {f && (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader icon={<Shield />} title={t("net.fw_cluster")} description={t("net.fw_cluster_desc")} action={<Badge tone={clusterOn ? "online" : "neutral"}>{clusterOn ? t("common.enabled") : t("common.disabled")}</Badge>} />
              <CardBody className="flex flex-wrap items-center gap-3 text-sm">
                <span>{t("net.policy_in")}: <b>{String(f.cluster_options.policy_in ?? "DROP")}</b></span>
                <span>{t("net.policy_out")}: <b>{String(f.cluster_options.policy_out ?? "ACCEPT")}</b></span>
                <div className="flex-1" />
                <Button size="sm" onClick={() => setClusterToggle(!clusterOn)}>{clusterOn ? t("net.disable") : t("net.enable")}</Button>
              </CardBody>
            </Card>
            <Card>
              <CardHeader icon={<Shield />} title={t("net.fw_node", { node: node?.name })} description={t("net.fw_node_desc")} action={<Badge tone={nodeOn ? "online" : "neutral"}>{nodeOn ? t("common.enabled") : t("common.disabled")}</Badge>} />
              <CardBody className="flex justify-end"><Button size="sm" loading={opts.isPending} onClick={() => opts.mutate({ level: "node", enable: !nodeOn })}>{nodeOn ? t("net.disable") : t("net.enable")}</Button></CardBody>
            </Card>
          </div>
          <RulesCard title={t("net.fw_cluster_rules")} rules={f.cluster_rules} onAdd={() => setRule({ level: "cluster" })} onDelete={(pos) => delRule.mutate({ level: "cluster", pos })} />
          <RulesCard title={t("net.fw_node_rules")} rules={f.node_rules} onAdd={() => setRule({ level: "node" })} onDelete={(pos) => delRule.mutate({ level: "node", pos })} />
          <div className="grid gap-4 md:grid-cols-3">
            <SmallList title={t("net.fw_groups")} items={isErr(f.groups) ? [] : f.groups.map((g) => `${g.group}${g.comment ? ` · ${g.comment}` : ""}`)} />
            <SmallList title={t("net.fw_aliases")} items={isErr(f.aliases) ? [] : f.aliases.map((a) => `${a.name} = ${a.cidr}`)} />
            <SmallList title={t("net.fw_ipsets")} items={isErr(f.ipsets) ? [] : f.ipsets.map((a) => a.name)} />
          </div>
          <Card>
            <CardHeader title={t("net.fw_guests")} description={t("net.fw_guests_desc")} />
            <Table>
              <thead><Tr><Th>VMID</Th><Th>{t("common.name")}</Th><Th>NIC</Th><Th /></Tr></thead>
              <tbody>
                {f.guests.map((g) => (
                  <Tr key={`${g.type}${g.vmid}`}>
                    <Td className="font-semibold"><GuestIcon type={g.type} className="mr-1.5 inline size-3.5" />{g.vmid}</Td>
                    <Td>{g.name}</Td>
                    <Td className="space-x-1.5">{g.nics.map((n) => <Badge key={n.nic} tone={n.firewall ? "online" : "neutral"}>{n.nic} · {n.bridge}{n.firewall ? " · fw" : ""}</Badge>)}</Td>
                    <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => setGuest(g)}><Eye /> {t("net.fw_rules")}</Button></Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Card>
        </>
      )}
      {rule && <RuleDialog nodeId={nodeId} target={rule} onClose={() => setRule(null)} onSaved={() => { refresh(); qc.invalidateQueries({ queryKey: ["fw-guest"] }); }} />}
      {guest && <GuestFirewall nodeId={nodeId} g={guest} onClose={() => setGuest(null)} onAdd={() => setRule({ level: "guest", type: guest.type, vmid: guest.vmid })} />}
      <TypeToConfirm open={clusterToggle !== null} onOpenChange={(v) => !v && setClusterToggle(null)} word={node?.cluster_name || node?.name || ""}
        title={clusterToggle ? t("net.fw_enable_cluster") : t("net.fw_disable_cluster")} action={clusterToggle ? t("net.enable") : t("net.disable")} loading={opts.isPending}
        onConfirm={(w) => opts.mutate({ level: "cluster", enable: clusterToggle, confirm: w })}>
        <Alert tone="warning">{t("net.fw_cluster_warning")}</Alert>
      </TypeToConfirm>
    </div>
  );
}

function SmallList({ title, items }: { title: string; items: string[] }) {
  const { t } = useTranslation();
  return (
    <Card>
      <CardHeader title={title} />
      <CardBody className="space-y-1 text-sm">{items.length ? items.map((i) => <div key={i} className="mono text-xs">{i}</div>) : <span className="text-muted">{t("net.none")}</span>}</CardBody>
    </Card>
  );
}

function RulesCard({ title, rules, onAdd, onDelete }: { title: string; rules: FwRule[] | { error: string }; onAdd: () => void; onDelete: (pos: number) => void }) {
  const { t } = useTranslation();
  return (
    <Card>
      <CardHeader title={title} action={<Button size="sm" onClick={onAdd}><Plus /> {t("net.add_rule")}</Button>} />
      {isErr(rules) ? <CardBody><Alert tone="warning">{rules.error}</Alert></CardBody> : <RulesTable rules={rules} onDelete={onDelete} />}
    </Card>
  );
}

function RulesTable({ rules, onDelete }: { rules: FwRule[]; onDelete: (pos: number) => void }) {
  const { t } = useTranslation();
  if (!rules.length) return <CardBody className="text-sm text-muted">{t("net.no_rules")}</CardBody>;
  return (
    <Table>
      <thead><Tr><Th>#</Th><Th>{t("net.direction")}</Th><Th>{t("net.action")}</Th><Th>{t("net.match")}</Th><Th>{t("net.comment")}</Th><Th /></Tr></thead>
      <tbody>
        {rules.map((r) => (
          <Tr key={r.pos} className={r.enable === 0 ? "opacity-50" : ""}>
            <Td>{r.pos}</Td>
            <Td>{r.type}</Td>
            <Td><Badge tone={r.action === "ACCEPT" ? "online" : r.action === "DROP" || r.action === "REJECT" ? "offline" : "info"}>{r.action}</Badge></Td>
            <Td className="mono text-xs">{[r.macro && `macro ${r.macro}`, r.proto, r.dport && `dport ${r.dport}`, r.source && `from ${r.source}`, r.dest && `to ${r.dest}`, r.iface && `if ${r.iface}`].filter(Boolean).join(" · ") || "*"}</Td>
            <Td className="text-xs text-muted">{r.comment}</Td>
            <Td className="text-right"><Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => onDelete(r.pos)}><Trash2 /></Button></Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function GuestFirewall({ nodeId, g, onClose, onAdd }: { nodeId: string; g: { type: "qemu" | "lxc"; vmid: number; name: string }; onClose: () => void; onAdd: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["fw-guest", nodeId, g.vmid], queryFn: () => get<{ options: Record<string, unknown>; rules: FwRule[] }>(`api/firewall/${nodeId}/guest/${g.type}/${g.vmid}`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["fw-guest", nodeId, g.vmid] });
  const opt = useMutation({ mutationFn: (enable: boolean) => put(`api/firewall/${nodeId}/options`, { level: "guest", type: g.type, vmid: g.vmid, enable }), onSuccess: refresh, onError: (e) => toast.error(errorMessage(e, t)) });
  const delRule = useMutation({ mutationFn: (pos: number) => post(`api/firewall/${nodeId}/rules/delete`, { level: "guest", type: g.type, vmid: g.vmid, pos }), onSuccess: refresh, onError: (e) => toast.error(errorMessage(e, t)) });
  const on = !!q.data?.options?.enable;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={t("net.fw_guest_title", { vmid: g.vmid, name: g.name })}
      footer={<><Button onClick={onAdd}><Plus /> {t("net.add_rule")}</Button><div className="flex-1" /><Button onClick={onClose}>{t("common.close")}</Button></>}>
      {q.isLoading ? <Spinner /> : (
        <div className="space-y-4">
          <Switch checked={on} onCheckedChange={(v) => opt.mutate(v)} label={t("net.fw_guest_enable")} description={t("net.fw_guest_enable_hint")} />
          <Card><RulesTable rules={q.data?.rules ?? []} onDelete={(pos) => delRule.mutate(pos)} /></Card>
        </div>
      )}
    </Dialog>
  );
}

function RuleDialog({ nodeId, target, onClose, onSaved }: { nodeId: string; target: { level: string; type?: string; vmid?: number; group?: string }; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [r, setR] = useState({ type: "in", action: "ACCEPT", proto: "tcp", dport: "", source: "", dest: "", macro: "", comment: "", enable: true });
  const set = (p: Partial<typeof r>) => setR((o) => ({ ...o, ...p }));
  const save = useMutation({
    mutationFn: () => post(`api/firewall/${nodeId}/rules`, { ...target, rule: { type: r.type, action: r.action, enable: r.enable, proto: r.macro ? null : r.proto || null, dport: r.macro ? null : r.dport || null, source: r.source || null, dest: r.dest || null, macro: r.macro || null, comment: r.comment } }),
    onSuccess: () => { toast.success(t("common.saved")); onSaved(); onClose(); },
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("net.add_rule")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("net.direction")}><Select value={r.type} onChange={(e) => set({ type: e.target.value })}><option value="in">in</option><option value="out">out</option></Select></Field>
          <Field label={t("net.action")}><Select value={r.action} onChange={(e) => set({ action: e.target.value })}>{["ACCEPT", "DROP", "REJECT"].map((a) => <option key={a}>{a}</option>)}</Select></Field>
        </div>
        <Field label={t("net.macro")} hint={t("net.macro_hint")}><Input value={r.macro} onChange={(e) => set({ macro: e.target.value.trim() })} placeholder="SSH, HTTPS, DNS…" /></Field>
        {!r.macro && (
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("net.proto")}><Select value={r.proto} onChange={(e) => set({ proto: e.target.value })}>{["tcp", "udp", "icmp", ""].map((p) => <option key={p} value={p}>{p || t("net.any")}</option>)}</Select></Field>
            <Field label={t("net.dport")}><Input value={r.dport} onChange={(e) => set({ dport: e.target.value.trim() })} className="mono" placeholder="443" /></Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("net.source")}><Input value={r.source} onChange={(e) => set({ source: e.target.value.trim() })} className="mono" placeholder="10.0.0.0/24" /></Field>
          <Field label={t("net.dest")}><Input value={r.dest} onChange={(e) => set({ dest: e.target.value.trim() })} className="mono" /></Field>
        </div>
        <Field label={t("net.comment")}><Input value={r.comment} onChange={(e) => set({ comment: e.target.value })} /></Field>
        <Checkbox checked={r.enable} onCheckedChange={(v) => set({ enable: v })} label={t("net.rule_enabled")} />
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ SDN */
function SdnTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { nodeId, setNodeId, node, nodes } = useNodeParam();
  const st = useQuery({ queryKey: ["sdn-state", nodeId], queryFn: () => get<SdnState>(`api/sdn/${nodeId}/state`), enabled: !!nodeId && !!node?.online });
  const designs = useQuery({ queryKey: ["sdn-designs"], queryFn: () => get<SdnDesign[]>("api/sdn/designs"), enabled: admin });
  const imp = useMutation({ mutationFn: (zone: string) => post<SdnDesign>("api/sdn/designs/import", { node_id: nodeId, zone }), onSuccess: (d) => navigate(`/proxmox/network/sdn-designer/${d.id}`), onError: (e) => toast.error(errorMessage(e, t)) });
  const remove = useMutation({ mutationFn: (id: string) => del(`api/sdn/designs/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["sdn-designs"] }) });
  const s = st.data;
  const subnetsOf = (vnet: string) => (s?.subnets ?? []).filter((x) => x.vnet === vnet).map((x) => String(x.cidr ?? x.subnet));
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3"><NodeSelect value={nodeId} onChange={setNodeId} nodes={nodes} /></div>
      {st.isError && <Alert tone="offline">{errorMessage(st.error, t)}</Alert>}
      {s && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader title={t("net.sdn_zones")} />
            {!s.zones.length ? <CardBody className="text-sm text-muted">{t("net.sdn_no_zones")}</CardBody> : (
              <Table>
                <thead><Tr><Th>Zone</Th><Th>{t("net.type")}</Th><Th>{t("net.bridge")}</Th><Th>Nodes</Th><Th /></Tr></thead>
                <tbody>
                  {s.zones.map((z) => (
                    <Tr key={String(z.zone)}>
                      <Td className="font-semibold">{String(z.zone)}</Td><Td>{String(z.type)}</Td><Td>{String(z.bridge ?? "-")}</Td><Td className="text-xs">{String(z.nodes ?? t("net.all_nodes"))}</Td>
                      <Td className="text-right">{admin && <Button size="sm" variant="ghost" loading={imp.isPending} onClick={() => imp.mutate(String(z.zone))}><Pencil /> {t("net.sdn_edit_in_designer")}</Button>}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
          <Card>
            <CardHeader title="VNets" />
            {!s.vnets.length ? <CardBody className="text-sm text-muted">{t("net.sdn_no_vnets")}</CardBody> : (
              <Table>
                <thead><Tr><Th>VNet</Th><Th>Zone</Th><Th>Tag</Th><Th>Subnets</Th></Tr></thead>
                <tbody>
                  {s.vnets.map((v) => (
                    <Tr key={v.vnet}>
                      <Td className="font-semibold"><span className="mr-2 inline-block size-2.5 rounded-[3px]" style={{ background: vlanColor(v.tag) }} />{v.vnet}{v.alias ? <span className="ml-1 text-muted">({v.alias})</span> : null}</Td>
                      <Td>{v.zone}</Td><Td>{v.tag ?? "-"}</Td><Td className="mono text-xs">{subnetsOf(v.vnet).join(", ") || "-"}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </div>
      )}
      {admin && (
        <Card>
          <CardHeader title={t("net.sdn_designs")} description={t("net.sdn_designs_desc")} action={<Link to="/proxmox/network/sdn-designer"><Button size="sm" variant="primary"><Plus /> {t("net.sdn_new_design")}</Button></Link>} />
          {!designs.data?.length ? <CardBody className="text-sm text-muted">{t("net.sdn_no_designs")}</CardBody> : (
            <Table>
              <thead><Tr><Th>{t("common.name")}</Th><Th>Zone</Th><Th>Status</Th><Th>{t("net.updated")}</Th><Th /></Tr></thead>
              <tbody>
                {designs.data.map((d) => (
                  <Tr key={d.id}>
                    <Td className="font-semibold"><Link to={`/proxmox/network/sdn-designer/${d.id}`} className="hover:underline">{d.name}</Link></Td>
                    <Td>{d.spec.zone.id} · {d.spec.zone.type}</Td>
                    <Td><DesignStatus status={d.status} /></Td>
                    <Td className="text-xs text-muted">{formatDate(d.updated_at, i18n.language)}</Td>
                    <Td className="text-right"><Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => remove.mutate(d.id)}><Trash2 /></Button></Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      )}
    </div>
  );
}

export function DesignStatus({ status }: { status: SdnDesign["status"] }) {
  const { t } = useTranslation();
  const tone = status === "applied" ? "online" : status === "failed" ? "offline" : status === "rolled_back" ? "warning" : "neutral";
  return <Badge tone={tone}>{t(`net.design_${status}`)}</Badge>;
}

/* ------------------------------------------------------------------ config backups */
function BackupsTab() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const { list } = usePveNodes();
  const backups = useQuery({ queryKey: ["config-backups"], queryFn: () => get<ConfigBackup[]>("api/network/backups") });
  const [view, setView] = useState<string | null>(null);
  const [restore, setRestore] = useState<ConfigBackup | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const names = useMemo(() => Object.fromEntries(list.map((n) => [n.id, n.name])), [list]);
  const go = useMutation({
    mutationFn: ({ b, w }: { b: ConfigBackup; w: string }) => post<{ job?: { id: string } }>(`api/network/backups/${b.id}/restore`, { confirm: w }),
    onSuccess: (r) => { setRestore(null); if (r.job) setJobId(r.job.id); qc.invalidateQueries({ queryKey: ["config-backups"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div className="space-y-5">
      <Alert tone="info">{t("net.backups_desc")}</Alert>
      {jobId && <Card><CardHeader title={t("net.restore_running")} action={<Button size="sm" onClick={() => setJobId(null)}>{t("common.close")}</Button>} /><CardBody><JobView jobId={jobId} /></CardBody></Card>}
      <Card>
        {!backups.data?.length ? <EmptyState icon={<RotateCcw />} title={t("net.no_backups")} /> : (
          <Table>
            <thead><Tr><Th>{t("net.when")}</Th><Th>{t("net.kind")}</Th><Th>Node</Th><Th>{t("net.reason")}</Th><Th>{t("net.by")}</Th><Th /></Tr></thead>
            <tbody>
              {backups.data.map((b) => (
                <Tr key={b.id}>
                  <Td className="text-xs">{formatDate(b.created_at, i18n.language)}{b.restored_at && <Badge tone="warning" className="ml-2">{t("net.restored")}</Badge>}</Td>
                  <Td><Badge tone="info">{b.kind === "sdn" ? "SDN" : t("net.kind_network")}</Badge></Td>
                  <Td>{names[b.node_id] ?? b.node_id}</Td>
                  <Td className="text-xs text-muted">{b.reason}</Td>
                  <Td className="text-xs">{b.created_by ?? "-"}</Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="ghost" onClick={() => setView(b.id)}><Eye /> {t("net.view")}</Button>
                      <Button size="sm" onClick={() => setRestore(b)}><RotateCcw /> {t("net.restore")}</Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {view && <BackupView id={view} onClose={() => setView(null)} />}
      <TypeToConfirm open={!!restore} onOpenChange={(v) => !v && setRestore(null)} word={restore ? names[restore.node_id] ?? "" : ""} title={t("net.restore_title")} action={t("net.restore")} loading={go.isPending} onConfirm={(w) => restore && go.mutate({ b: restore, w })}>
        <Alert tone="warning">{restore?.kind === "sdn" ? t("net.restore_sdn_explain") : t("net.restore_net_explain")}</Alert>
      </TypeToConfirm>
    </div>
  );
}

function BackupView({ id, onClose }: { id: string; onClose: () => void }) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["config-backup", id], queryFn: () => get<ConfigBackup>(`api/network/backups/${id}`) });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} xl title={t("net.backup_content")}>
      {q.isLoading ? <Spinner /> : (
        <div className="space-y-4">
          {Object.entries(q.data?.content ?? {}).map(([path, content]) => (
            <div key={path}>
              <div className="mono mb-1 text-xs font-semibold">{path}</div>
              <pre className="max-h-72 overflow-auto rounded-md bg-sunken p-3 text-[12px]">{content ?? t("net.file_missing")}</pre>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
