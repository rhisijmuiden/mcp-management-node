import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, ArrowRight, Check, CircleDot, Plus, Rocket, Save, Server, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { JobView } from "@/components/jobs";
import { NodeSelect, TypeToConfirm, usePveNodes, vlanColor } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Checkbox, Field, Input, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner } from "@/components/ui/misc";
import { errorMessage, get, post, put } from "@/lib/api";
import type { NetIface, SdnDesign, SdnOp, SdnPlan, SdnSpec, SdnSubnetSpec, SdnVnetSpec, ZoneType } from "@/lib/types";
import { cn } from "@/lib/utils";

const ZONE_TYPES: ZoneType[] = ["vlan", "simple", "vxlan", "evpn", "qinq"];
const ID_RE = /^[a-z][a-z0-9]{1,7}$/;

const emptySpec = (): SdnSpec => ({
  zone: { id: "", type: "vlan", bridge: "vmbr0", tag: null, peers: [], controller: null, vrf_vxlan: null, mtu: null, nodes: [], ipam: true, dhcp: false },
  vnets: [],
  subnets: [],
});

/** Debounce a value so the live plan is not requested on every keystroke. */
function useDebounced<T>(value: T, ms = 600): T {
  const [v, setV] = useState(value);
  useEffect(() => { const h = setTimeout(() => setV(value), ms); return () => clearTimeout(h); }, [value, ms]);
  return v;
}

export function SdnDesignerPage() {
  const { t } = useTranslation();
  const { id } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { list } = usePveNodes();
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [spec, setSpec] = useState<SdnSpec>(emptySpec);
  const [designId, setDesignId] = useState<string | null>(id ?? null);
  const [confirm, setConfirm] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const design = useQuery({ queryKey: ["sdn-design", id], queryFn: () => get<SdnDesign>(`api/sdn/designs/${id}`), enabled: !!id });
  useEffect(() => {
    if (design.data) { setName(design.data.name); setNodeId(design.data.node_id); setSpec(design.data.spec); setDesignId(design.data.id); }
  }, [design.data]);
  useEffect(() => {
    if (!id && !nodeId && list.length) setNodeId((list.find((n) => n.online) ?? list[0]).id);
  }, [id, nodeId, list]);

  const ifaces = useQuery({ queryKey: ["net-ifaces", nodeId], queryFn: () => get<NetIface[]>(`api/network/${nodeId}/interfaces`), enabled: !!nodeId });
  const bridges = (ifaces.data ?? []).filter((i) => i.type === "bridge" || i.type === "OVSBridge");

  const debounced = useDebounced(spec);
  const planReady = !!nodeId && ID_RE.test(debounced.zone.id);
  const plan = useQuery({
    queryKey: ["sdn-plan", nodeId, debounced],
    queryFn: () => post<SdnPlan>("api/sdn/plan", { node_id: nodeId, spec: debounced }),
    enabled: planReady,
    retry: false,
    placeholderData: (prev) => prev,
  });
  const p = plan.data;
  const clusterNodes = p?.cluster_nodes ?? [];

  const save = useMutation({
    mutationFn: () => {
      const body = { name: name || `Zone ${spec.zone.id}`, node_id: nodeId, spec };
      return designId ? put<SdnDesign>(`api/sdn/designs/${designId}`, body) : post<SdnDesign>("api/sdn/designs", body);
    },
    onSuccess: (d) => {
      setDesignId(d.id);
      qc.invalidateQueries({ queryKey: ["sdn-designs"] });
      if (!id) nav(`/proxmox/network/sdn-designer/${d.id}`, { replace: true });
      toast.success(t("sdn.saved"));
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });

  const apply = useMutation({
    mutationFn: async (typed: string) => {
      const d = await save.mutateAsync();
      return post<SdnPlan>(`api/sdn/designs/${d.id}/apply`, { dry_run: false, confirm: typed });
    },
    onSuccess: (r) => {
      setConfirm(false);
      if (r.job) setJobId(r.job.id);
      else toast.success(t("sdn.nothing_to_do"));
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });

  const set = (patch: Partial<SdnSpec>) => setSpec((s) => ({ ...s, ...patch }));
  const setZone = (patch: Partial<SdnSpec["zone"]>) => setSpec((s) => ({ ...s, zone: { ...s.zone, ...patch } }));

  const steps = [t("sdn.step_zone"), t("sdn.step_vnets"), t("sdn.step_subnets"), t("sdn.step_nodes"), t("sdn.step_review")];
  const stepValid = [
    ID_RE.test(spec.zone.id) && !!nodeId && (!["vlan", "qinq"].includes(spec.zone.type) || !!spec.zone.bridge),
    spec.vnets.every((v) => ID_RE.test(v.id)),
    spec.subnets.every((s) => s.cidr.includes("/")),
    true,
    true,
  ];

  if (id && design.isLoading) return <Spinner />;
  if (id && design.isError) return <Alert tone="offline">{errorMessage(design.error, t)}</Alert>;

  return (
    <div>
      <PageHeader title={t("sdn.title")} description={t("sdn.desc")}
        actions={<>
          <Link to="/proxmox/network?tab=sdn"><Button><ArrowLeft /> {t("sdn.back")}</Button></Link>
          <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!stepValid[0]}><Save /> {t("sdn.save")}</Button>
        </>} />

      <ol className="mb-6 grid grid-cols-5 gap-2" aria-label={t("sdn.steps")}>
        {steps.map((s, i) => (
          <li key={s}>
            <button type="button" onClick={() => setStep(i)} aria-current={step === i ? "step" : undefined}
              className={cn("flex w-full items-center gap-2 rounded-md border px-3 py-2.5 text-left text-[13px] font-semibold",
                step === i ? "border-ink bg-raised" : "border-line bg-surface text-muted hover:text-ink")}>
              <span className={cn("grid size-6 shrink-0 place-items-center rounded-full text-xs",
                i < step && stepValid[i] ? "bg-online text-white" : step === i ? "bg-ink text-white" : "bg-sunken")}>
                {i < step && stepValid[i] ? <Check className="size-3.5" /> : i + 1}
              </span>
              <span className="truncate">{s}</span>
            </button>
          </li>
        ))}
      </ol>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <Card>
          <CardHeader title={steps[step]} description={t(`sdn.step_${["zone", "vnets", "subnets", "nodes", "review"][step]}_hint`)} />
          <CardBody className="space-y-4">
            {step === 0 && (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label={t("sdn.design_name")}><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("sdn.design_name_ph")} /></Field>
                  <NodeSelect label={t("sdn.via_node")} value={nodeId} onChange={setNodeId} nodes={list} className="w-full" />
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label={t("sdn.zone_id")} hint={t("sdn.id_hint")}>
                    <Input value={spec.zone.id} onChange={(e) => setZone({ id: e.target.value.toLowerCase() })} className="mono" disabled={!!design.data && design.data.status === "applied"} />
                  </Field>
                  <Field label={t("sdn.zone_type")}>
                    <Select value={spec.zone.type} onChange={(e) => setZone({ type: e.target.value as ZoneType })}>
                      {ZONE_TYPES.map((z) => <option key={z} value={z}>{t(`sdn.type_${z}`)}</option>)}
                    </Select>
                  </Field>
                </div>
                <p className="text-xs text-muted">{t(`sdn.type_${spec.zone.type}_hint`)}</p>
                {["vlan", "qinq"].includes(spec.zone.type) && (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label={t("sdn.bridge")}>
                      <Select value={spec.zone.bridge ?? ""} onChange={(e) => setZone({ bridge: e.target.value || null })}>
                        <option value="">…</option>
                        {bridges.map((b) => <option key={b.iface} value={b.iface}>{b.iface}{b.vlan_aware ? "" : ` · ${t("net.not_vlan_aware")}`}</option>)}
                        {spec.zone.bridge && !bridges.some((b) => b.iface === spec.zone.bridge) && <option value={spec.zone.bridge}>{spec.zone.bridge}</option>}
                      </Select>
                    </Field>
                    {spec.zone.type === "qinq" && (
                      <Field label={t("sdn.service_tag")}><Input type="number" min={1} max={4094} value={spec.zone.tag ?? ""} onChange={(e) => setZone({ tag: e.target.value ? Number(e.target.value) : null })} /></Field>
                    )}
                  </div>
                )}
                {spec.zone.type === "vxlan" && (
                  <Field label={t("sdn.peers")} hint={t("sdn.peers_hint")}>
                    <Input value={spec.zone.peers.join(", ")} onChange={(e) => setZone({ peers: e.target.value.split(/[\s,]+/).filter(Boolean) })} className="mono" />
                  </Field>
                )}
                {spec.zone.type === "evpn" && (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label={t("sdn.controller")}><Input value={spec.zone.controller ?? ""} onChange={(e) => setZone({ controller: e.target.value || null })} className="mono" /></Field>
                    <Field label={t("sdn.vrf_vxlan")}><Input type="number" value={spec.zone.vrf_vxlan ?? ""} onChange={(e) => setZone({ vrf_vxlan: e.target.value ? Number(e.target.value) : null })} /></Field>
                  </div>
                )}
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label={t("sdn.mtu")} hint={t("sdn.mtu_hint")}><Input type="number" min={576} max={65520} value={spec.zone.mtu ?? ""} onChange={(e) => setZone({ mtu: e.target.value ? Number(e.target.value) : null })} /></Field>
                </div>
                <Switch checked={spec.zone.ipam} onCheckedChange={(v) => setZone({ ipam: v })} label={t("sdn.ipam")} description={t("sdn.ipam_hint")} />
                {spec.zone.type === "simple" && <Switch checked={spec.zone.dhcp} onCheckedChange={(v) => setZone({ dhcp: v })} label={t("sdn.dhcp")} description={t("sdn.dhcp_hint")} />}
              </>
            )}

            {step === 1 && <VnetsStep spec={spec} set={set} />}
            {step === 2 && <SubnetsStep spec={spec} set={set} />}

            {step === 3 && (
              <>
                <Switch checked={!spec.zone.nodes.length} onCheckedChange={(v) => setZone({ nodes: v ? [] : [...clusterNodes] })} label={t("sdn.all_nodes")} description={t("sdn.all_nodes_hint")} />
                {!!spec.zone.nodes.length && (
                  <div className="grid gap-2 sm:grid-cols-2">
                    {[...new Set([...clusterNodes, ...spec.zone.nodes])].map((n) => (
                      <Checkbox key={n} checked={spec.zone.nodes.includes(n)} label={n}
                        onCheckedChange={(v) => setZone({ nodes: v ? [...spec.zone.nodes, n] : spec.zone.nodes.filter((x) => x !== n) })} />
                    ))}
                  </div>
                )}
                {!clusterNodes.length && <p className="text-xs text-muted">{t("sdn.nodes_from_plan")}</p>}
              </>
            )}

            {step === 4 && (
              <ReviewStep plan={p} loading={plan.isFetching} error={plan.isError ? errorMessage(plan.error, t) : null} ready={planReady} />
            )}

            <div className="flex items-center justify-between border-t border-line pt-4">
              <Button onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0}><ArrowLeft /> {t("sdn.prev")}</Button>
              {step < 4
                ? <Button variant="primary" onClick={() => setStep((s) => s + 1)} disabled={!stepValid[step]}>{t("sdn.next")} <ArrowRight /></Button>
                : <Button variant="danger" onClick={() => setConfirm(true)} disabled={!p?.can_apply || plan.isFetching || !p.ops.some((o) => o.op !== "none")}><Rocket /> {t("sdn.rollout")}</Button>}
            </div>
          </CardBody>
        </Card>

        <div className="space-y-6">
          <Preview spec={spec} plan={p} clusterNodes={clusterNodes} />
          {step !== 4 && p && (p.errors.length > 0 || p.warnings.length > 0) && (
            <Card><CardBody className="space-y-2">
              {p.errors.map((e) => <Alert key={e} tone="offline">{e}</Alert>)}
              {p.warnings.map((w) => <Alert key={w} tone="warning">{w}</Alert>)}
            </CardBody></Card>
          )}
          {jobId && (
            <Card>
              <CardHeader title={t("sdn.rollout_running")} action={<Button size="sm" onClick={() => setJobId(null)}>{t("common.close")}</Button>} />
              <CardBody>
                <JobView jobId={jobId} onDone={() => { qc.invalidateQueries({ queryKey: ["sdn-plan"] }); qc.invalidateQueries({ queryKey: ["sdn-design"] }); qc.invalidateQueries({ queryKey: ["net-map"] }); }} />
                <p className="mt-3 text-xs text-muted">{t("sdn.rollback_hint")} <Link className="font-semibold underline" to="/proxmox/network?tab=backups">{t("net.tab_backups")}</Link></p>
              </CardBody>
            </Card>
          )}
        </div>
      </div>

      <TypeToConfirm open={confirm} onOpenChange={setConfirm} word={spec.zone.id} title={t("sdn.confirm_title")} action={t("sdn.rollout")} loading={apply.isPending} onConfirm={(w) => apply.mutate(w)}>
        <Alert tone="warning">{t("sdn.confirm_explain", { count: p?.ops.filter((o) => o.op !== "reload" && o.op !== "none").length ?? 0 })}</Alert>
      </TypeToConfirm>
    </div>
  );
}

/* ------------------------------------------------------------------ steps */
function VnetsStep({ spec, set }: { spec: SdnSpec; set: (p: Partial<SdnSpec>) => void }) {
  const { t } = useTranslation();
  const tagged = ["vlan", "qinq", "vxlan", "evpn"].includes(spec.zone.type);
  const upd = (i: number, patch: Partial<SdnVnetSpec>) => {
    const old = spec.vnets[i];
    const vnets = spec.vnets.map((v, j) => (j === i ? { ...v, ...patch } : v));
    // keep subnets attached when a VNet is renamed
    const subnets = patch.id !== undefined ? spec.subnets.map((s) => (s.vnet === old.id ? { ...s, vnet: patch.id! } : s)) : spec.subnets;
    set({ vnets, subnets });
  };
  const add = () => {
    const used = new Set(spec.vnets.map((v) => v.tag));
    let tag = spec.vnets.length ? Math.max(...spec.vnets.map((v) => v.tag ?? 0)) + 10 : 100;
    while (used.has(tag)) tag++;
    const base = (spec.zone.id || "vnet").slice(0, 5);
    let n = spec.vnets.length + 1;
    while (spec.vnets.some((v) => v.id === `${base}${n}`)) n++;
    set({ vnets: [...spec.vnets, { id: `${base}${n}`.slice(0, 8), alias: "", tag: tagged ? tag : null, vlanaware: false }] });
  };
  return (
    <div className="space-y-3">
      {!spec.vnets.length && <p className="text-sm text-muted">{t("sdn.no_vnets")}</p>}
      {spec.vnets.map((v, i) => (
        <div key={i} className="grid items-end gap-3 rounded-md border border-line p-3 sm:grid-cols-[110px_1fr_100px_auto]" style={{ borderLeft: `4px solid ${vlanColor(v.tag)}` }}>
          <Field label={t("sdn.vnet_id")}><Input value={v.id} onChange={(e) => upd(i, { id: e.target.value.toLowerCase() })} className={cn("mono", !ID_RE.test(v.id) && "border-offline")} /></Field>
          <Field label={t("sdn.alias")}><Input value={v.alias} onChange={(e) => upd(i, { alias: e.target.value })} placeholder={t("sdn.alias_ph")} /></Field>
          {tagged ? <Field label={spec.zone.type === "vxlan" || spec.zone.type === "evpn" ? "VNI" : "VLAN tag"}><Input type="number" value={v.tag ?? ""} onChange={(e) => upd(i, { tag: e.target.value ? Number(e.target.value) : null })} /></Field> : <div />}
          <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => set({ vnets: spec.vnets.filter((_, j) => j !== i), subnets: spec.subnets.filter((s) => s.vnet !== v.id) })}><Trash2 /></Button>
          <div className="sm:col-span-4"><Checkbox checked={v.vlanaware} onCheckedChange={(x) => upd(i, { vlanaware: x })} label={t("sdn.vnet_vlanaware")} /></div>
        </div>
      ))}
      <Button onClick={add}><Plus /> {t("sdn.add_vnet")}</Button>
    </div>
  );
}

function SubnetsStep({ spec, set }: { spec: SdnSpec; set: (p: Partial<SdnSpec>) => void }) {
  const { t } = useTranslation();
  const upd = (i: number, patch: Partial<SdnSubnetSpec>) => set({ subnets: spec.subnets.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const add = () => {
    const vnet = spec.vnets.find((v) => !spec.subnets.some((s) => s.vnet === v.id)) ?? spec.vnets[0];
    if (!vnet) return;
    const third = vnet.tag && vnet.tag < 255 ? vnet.tag : spec.subnets.length + 10;
    set({ subnets: [...spec.subnets, { vnet: vnet.id, cidr: `10.${third}.0.0/24`.replace(/^10\.(\d+)\.0\.0/, "10.10.$1.0"), gateway: `10.10.${third}.1`, snat: false }] });
  };
  if (!spec.vnets.length) return <p className="text-sm text-muted">{t("sdn.subnets_need_vnet")}</p>;
  return (
    <div className="space-y-3">
      {!spec.subnets.length && <p className="text-sm text-muted">{t("sdn.no_subnets")}</p>}
      {spec.subnets.map((s, i) => {
        const v = spec.vnets.find((x) => x.id === s.vnet);
        return (
          <div key={i} className="space-y-3 rounded-md border border-line p-3" style={{ borderLeft: `4px solid ${vlanColor(v?.tag)}` }}>
            <div className="grid items-end gap-3 sm:grid-cols-[120px_1fr_1fr_auto]">
              <Field label="VNet">
                <Select value={s.vnet} onChange={(e) => upd(i, { vnet: e.target.value })}>
                  {spec.vnets.map((x) => <option key={x.id} value={x.id}>{x.id}</option>)}
                </Select>
              </Field>
              <Field label={t("sdn.cidr")}><Input value={s.cidr} onChange={(e) => upd(i, { cidr: e.target.value.trim() })} className="mono" placeholder="10.10.100.0/24" /></Field>
              <Field label={t("sdn.gateway")}><Input value={s.gateway ?? ""} onChange={(e) => upd(i, { gateway: e.target.value.trim() || null })} className="mono" /></Field>
              <Button variant="ghost" size="icon" aria-label={t("common.delete")} onClick={() => set({ subnets: spec.subnets.filter((_, j) => j !== i) })}><Trash2 /></Button>
            </div>
            <div className="flex flex-wrap items-center gap-5">
              <Checkbox checked={s.snat} onCheckedChange={(x) => upd(i, { snat: x })} label={t("sdn.snat")} />
            </div>
            {spec.zone.dhcp && (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t("sdn.dhcp_start")}><Input value={s.dhcp_start ?? ""} onChange={(e) => upd(i, { dhcp_start: e.target.value.trim() || null })} className="mono" /></Field>
                <Field label={t("sdn.dhcp_end")}><Input value={s.dhcp_end ?? ""} onChange={(e) => upd(i, { dhcp_end: e.target.value.trim() || null })} className="mono" /></Field>
              </div>
            )}
          </div>
        );
      })}
      <Button onClick={add}><Plus /> {t("sdn.add_subnet")}</Button>
    </div>
  );
}

function ReviewStep({ plan, loading, error, ready }: { plan?: SdnPlan; loading: boolean; error: string | null; ready: boolean }) {
  const { t } = useTranslation();
  if (!ready) return <Alert tone="warning">{t("sdn.review_need_zone")}</Alert>;
  if (error) return <Alert tone="offline">{error}</Alert>;
  if (!plan) return <Spinner />;
  const changes = plan.ops.filter((o) => o.op !== "none");
  return (
    <div className="space-y-3">
      {loading && <div className="flex items-center gap-2 text-xs text-muted"><Spinner className="size-3.5" /> {t("sdn.recalculating")}</div>}
      {plan.errors.map((e) => <Alert key={e} tone="offline">{e}</Alert>)}
      {plan.warnings.map((w) => <Alert key={w} tone="warning">{w}</Alert>)}
      {!changes.length && <Alert tone="online">{t("sdn.in_sync")}</Alert>}
      {changes.length > 0 && <p className="text-sm text-muted">{t("sdn.review_intro", { cluster: plan.cluster ?? "-" })}</p>}
      <ul className="space-y-2">
        {changes.map((o, i) => <OpRow key={i} op={o} />)}
      </ul>
      {plan.can_apply && changes.length > 0 && <Alert tone="info">{t("sdn.review_safety")}</Alert>}
    </div>
  );
}

function OpRow({ op }: { op: SdnOp }) {
  const { t } = useTranslation();
  const tone = op.op === "create" ? "online" : op.op === "delete" ? "offline" : op.op === "update" ? "warning" : "info";
  const keys = [...new Set([...Object.keys(op.before ?? {}), ...Object.keys(op.after ?? {})])].filter((k) => (op.before ?? {})[k] !== (op.after ?? {})[k]);
  return (
    <li className="rounded-md border border-line p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone={tone}>{t(`sdn.op_${op.op}`)}</Badge>
        <span className="font-semibold">{op.kind}</span>
        <span className="mono">{op.id}</span>
        <span className="text-muted">{op.summary}</span>
      </div>
      {op.op === "update" && keys.length > 0 && (
        <div className="mono mt-2 space-y-0.5 rounded bg-sunken p-2 text-[12px]">
          {keys.map((k) => (
            <div key={k}><span className="text-muted">{k}:</span> <span className="text-offline line-through">{op.before?.[k] ?? "∅"}</span> → <span className="text-online">{op.after?.[k] ?? "∅"}</span></div>
          ))}
        </div>
      )}
      {!!op.used_by?.length && <div className="mt-2 flex items-center gap-1.5 text-xs text-offline"><AlertTriangle className="size-3.5" /> {t("sdn.used_by", { list: op.used_by.join(", ") })}</div>}
    </li>
  );
}

/* ------------------------------------------------------------------ live preview */
function Preview({ spec, plan, clusterNodes }: { spec: SdnSpec; plan?: SdnPlan; clusterNodes: string[] }) {
  const { t } = useTranslation();
  const opOf = useMemo(() => {
    const m = new Map<string, SdnOp["op"]>();
    for (const o of plan?.ops ?? []) m.set(`${o.kind}:${o.id}`, o.op);
    return m;
  }, [plan]);
  const mark = (kind: string, id: string): SdnOp["op"] | undefined => opOf.get(`${kind}:${id}`) ?? (plan ? "none" : undefined);
  const deleted = (plan?.ops ?? []).filter((o) => o.op === "delete");
  const nodes = spec.zone.nodes.length ? spec.zone.nodes : clusterNodes;
  const zoneOp = mark("zone", spec.zone.id);
  return (
    <Card>
      <CardHeader title={t("sdn.preview")} description={t("sdn.preview_hint")} />
      <CardBody className="space-y-4">
        <div className="rounded-lg border-2 border-ink p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="display text-lg">{spec.zone.id || t("sdn.unnamed_zone")}</span>
            <Badge tone="accent">{spec.zone.type.toUpperCase()}</Badge>
            {["vlan", "qinq"].includes(spec.zone.type) && spec.zone.bridge && <Badge tone="neutral">{t("sdn.on_bridge", { bridge: spec.zone.bridge })}{spec.zone.type === "qinq" && spec.zone.tag ? ` · S-tag ${spec.zone.tag}` : ""}</Badge>}
            {spec.zone.mtu && <Badge tone="neutral">MTU {spec.zone.mtu}</Badge>}
            <ChangeMark op={zoneOp} />
          </div>

          <div className="mt-4 space-y-2.5 border-l-2 border-ink pl-4">
            {!spec.vnets.length && <div className="text-xs text-muted">{t("sdn.no_vnets")}</div>}
            {spec.vnets.map((v) => {
              const subs = spec.subnets.filter((s) => s.vnet === v.id);
              return (
                <div key={v.id} className="rounded-md border bg-raised p-3" style={{ borderColor: vlanColor(v.tag) }}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="size-3 rounded-[3px]" style={{ background: vlanColor(v.tag) }} aria-hidden />
                    <span className="mono font-bold">{v.id}</span>
                    {v.alias && <span className="text-sm">{v.alias}</span>}
                    {v.tag && <Badge tone="neutral">{spec.zone.type === "vxlan" || spec.zone.type === "evpn" ? "VNI" : "VLAN"} {v.tag}</Badge>}
                    {v.vlanaware && <Badge tone="info">{t("net.vlan_aware")}</Badge>}
                    <ChangeMark op={mark("vnet", v.id)} />
                  </div>
                  {subs.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {subs.map((s) => (
                        <span key={s.cidr} className="mono inline-flex items-center gap-1.5 rounded bg-sunken px-2 py-1 text-[12px]">
                          {s.cidr}{s.gateway ? ` · gw ${s.gateway}` : ""}{s.snat ? " · SNAT" : ""}{s.dhcp_start ? ` · DHCP ${s.dhcp_start}-${s.dhcp_end ?? ""}` : ""}
                          <ChangeMark op={mark("subnet", `${v.id}:${s.cidr}`)} small />
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            {deleted.filter((o) => o.kind !== "zone").map((o) => (
              <div key={`${o.kind}-${o.id}`} className="rounded-md border border-dashed border-offline p-2 text-sm text-offline line-through">{o.kind} {o.id}</div>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-2 text-[13px] font-semibold">{t("sdn.matrix")}</div>
          {!nodes.length ? <p className="text-xs text-muted">{t("sdn.nodes_from_plan")}</p> : (
            <div className="overflow-x-auto">
              <table className="text-[12px]">
                <thead>
                  <tr>
                    <th className="p-1.5 text-left font-semibold text-muted"><Server className="inline size-3.5" /></th>
                    {spec.vnets.map((v) => <th key={v.id} className="mono p-1.5 font-semibold" style={{ color: "var(--ink)" }}>{v.id}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((n) => (
                    <tr key={n} className="border-t border-line">
                      <td className="p-1.5 font-semibold">{n}</td>
                      {spec.vnets.map((v) => (
                        <td key={v.id} className="p-1.5 text-center">
                          <CircleDot className="inline size-4" style={{ color: vlanColor(v.tag) }} aria-label={`${v.id} @ ${n}`} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-3 text-xs text-muted">
          <span className="inline-flex items-center gap-1"><ChangeMark op="create" small /> {t("sdn.legend_new")}</span>
          <span className="inline-flex items-center gap-1"><ChangeMark op="update" small /> {t("sdn.legend_changed")}</span>
          <span className="inline-flex items-center gap-1"><ChangeMark op="none" small /> {t("sdn.legend_live")}</span>
        </div>
      </CardBody>
    </Card>
  );
}

function ChangeMark({ op, small }: { op?: SdnOp["op"]; small?: boolean }) {
  const { t } = useTranslation();
  if (!op) return null;
  const cls = op === "create" ? "bg-online/15 text-online" : op === "update" ? "bg-warning-soft text-warning" : op === "delete" ? "bg-offline/15 text-offline" : "bg-sunken text-muted";
  const label = op === "create" ? t("sdn.legend_new") : op === "update" ? t("sdn.legend_changed") : op === "delete" ? t("sdn.op_delete") : t("sdn.legend_live");
  return <span className={cn("rounded px-1.5 font-semibold", small ? "text-[10px]" : "text-[11px] py-0.5", cls)}>{label}</span>;
}
