import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Box, Cloud, Disc, ExternalLink, GitBranch, Search, Sparkles, Store } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { ConsoleView } from "@/components/console-lazy";
import { useAllNodes } from "@/components/guests";
import { JobView } from "@/components/jobs";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, Spinner } from "@/components/ui/misc";
import { errorMessage, get, post } from "@/lib/api";
import type { CommunityScript, CommunityScriptFull, CommunityState, ConsoleSession, NodeJob, NodeResources, SuggestNode, TemplateLibrary } from "@/lib/types";
import { cn, formatBytes } from "@/lib/utils";

const tabCls = "flex items-center gap-2 rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 [&_svg]:size-4";

type Done = { job?: NodeJob; session?: ConsoleSession };

/* ------------------------------------------------------------------ shared pieces */
function useNodeChoice(environmentId?: string) {
  const nodes = useAllNodes();
  const suggest = useQuery({
    queryKey: ["suggest-node", environmentId ?? ""],
    queryFn: () => get<SuggestNode>(`api/guests/suggest-node${environmentId ? `?environment_id=${environmentId}` : ""}`),
  });
  const [nodeId, setNodeId] = useState("");
  useEffect(() => { if (!nodeId && suggest.data?.node_id) setNodeId(suggest.data.node_id); }, [suggest.data, nodeId]);
  const candidates = (nodes.data ?? []).filter((n) => n.capabilities.includes("guests") && (!environmentId || n.environment_id === environmentId));
  const res = useQuery({
    queryKey: ["node-resources", nodeId],
    queryFn: () => get<NodeResources>(`api/nodes/${nodeId}/resources`),
    enabled: !!nodeId,
    staleTime: 30_000,
  });
  return { nodeId, setNodeId, candidates, suggest: suggest.data, res };
}

type NodeChoice = ReturnType<typeof useNodeChoice>;

function NodePicker({ c }: { c: NodeChoice }) {
  const { t } = useTranslation();
  const score = (id: string) => c.suggest?.ranking?.find((r) => r.node_id === id)?.score;
  return (
    <Field label={t("gx.node")} hint={c.suggest?.node_id === c.nodeId ? t("gx.node_suggested") : undefined}>
      <Select value={c.nodeId} onChange={(e) => c.setNodeId(e.target.value)}>
        {!c.nodeId && <option value="">…</option>}
        {c.candidates.map((n) => {
          const s = score(n.id);
          return <option key={n.id} value={n.id} disabled={!n.online}>{n.name}{n.id === c.suggest?.node_id ? ` · ${t("gx.least_loaded")}` : ""}{s !== undefined ? ` (${Math.round(s * 100)}%)` : ""}{n.online ? "" : ` · ${t("px.offline")}`}</option>;
        })}
      </Select>
    </Field>
  );
}

function storagesFor(res: NodeResources | undefined, content: string) {
  return (res?.storages ?? []).filter((s) => s.active && s.content.includes(content));
}

function StorageSelect({ res, content, value, onChange, label }: { res?: NodeResources; content: string; value: string; onChange: (v: string) => void; label: string }) {
  const list = storagesFor(res, content);
  useEffect(() => { if (list.length && !list.some((s) => s.storage === value)) onChange(list[0].storage); }, [list.map((s) => s.storage).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Field label={label}>
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        {list.map((s) => <option key={s.storage} value={s.storage}>{s.storage} ({s.type}{s.avail != null ? ` · ${formatBytes(s.avail)} free` : ""})</option>)}
        {!list.length && <option value={value}>{value}</option>}
      </Select>
    </Field>
  );
}

interface Basics { name: string; vmid: string; cores: string; memory: string; disk: string; storage: string; bridge: string; vlan: string; ip: string; gateway: string; start: boolean; onboot: boolean; tags: string }

const basicsDefaults = (memory: number, disk: number): Basics => ({ name: "", vmid: "", cores: "2", memory: String(memory), disk: String(disk), storage: "local-lvm", bridge: "vmbr0", vlan: "", ip: "dhcp", gateway: "", start: true, onboot: false, tags: "" });

function BasicsFields({ c, f, set, content, showIp = true, nameLabel }: { c: NodeChoice; f: Basics; set: (p: Partial<Basics>) => void; content: "rootdir" | "images"; showIp?: boolean; nameLabel: string }) {
  const { t } = useTranslation();
  const bridges = c.res.data?.bridges ?? [];
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        <NodePicker c={c} />
        <Field label={nameLabel}><Input value={f.name} onChange={(e) => set({ name: e.target.value.replace(/[^A-Za-z0-9-]/g, "").slice(0, 63) })} placeholder="web-01" /></Field>
        <Field label="VMID"><Input type="number" min={100} value={f.vmid} onChange={(e) => set({ vmid: e.target.value })} placeholder={c.res.data?.nextid ? String(c.res.data.nextid) : t("gx.auto")} /></Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-4">
        <Field label={t("gx.cores_label")}><Input type="number" min={1} value={f.cores} onChange={(e) => set({ cores: e.target.value })} /></Field>
        <Field label={t("gx.memory_mb")}><Input type="number" min={64} step={256} value={f.memory} onChange={(e) => set({ memory: e.target.value })} /></Field>
        <Field label={t("gx.disk_gb")}><Input type="number" min={1} value={f.disk} onChange={(e) => set({ disk: e.target.value })} /></Field>
        <StorageSelect res={c.res.data} content={content} value={f.storage} onChange={(v) => set({ storage: v })} label={t("gx.storage")} />
      </div>
      <div className={cn("grid gap-3", showIp ? "sm:grid-cols-4" : "sm:grid-cols-2")}>
        <Field label={t("gx.bridge")}>
          <Select value={f.bridge} onChange={(e) => set({ bridge: e.target.value })}>
            {(bridges.length ? bridges : [f.bridge]).map((b) => <option key={b} value={b}>{b}</option>)}
          </Select>
        </Field>
        <Field label="VLAN"><Input type="number" min={1} max={4094} value={f.vlan} onChange={(e) => set({ vlan: e.target.value })} placeholder={t("gx.none")} /></Field>
        {showIp && <Field label={t("gx.ip")} hint={t("gx.ip_hint")}><Input className="mono" value={f.ip} onChange={(e) => set({ ip: e.target.value.trim() })} /></Field>}
        {showIp && <Field label={t("gx.gateway")}><Input className="mono" value={f.gateway} onChange={(e) => set({ gateway: e.target.value.trim() })} disabled={f.ip === "dhcp"} /></Field>}
      </div>
      <Field label={t("gx.tags")} hint={t("gx.tags_hint")}><Input value={f.tags} onChange={(e) => set({ tags: e.target.value })} /></Field>
      <div className="flex flex-wrap gap-6">
        <Switch checked={f.start} onCheckedChange={(v) => set({ start: v })} label={t("gx.start_after")} />
        <Switch checked={f.onboot} onCheckedChange={(v) => set({ onboot: v })} label={t("gx.onboot")} />
      </div>
    </>
  );
}

function basicsBody(c: NodeChoice, f: Basics) {
  return {
    node_id: c.nodeId, name: f.name, vmid: f.vmid ? Number(f.vmid) : undefined, cores: Number(f.cores), memory: Number(f.memory),
    disk_gb: Number(f.disk), storage: f.storage, start: f.start, onboot: f.onboot,
    tags: f.tags.split(/[,; ]+/).map((x) => x.trim()).filter(Boolean),
    net: { bridge: f.bridge, vlan: f.vlan ? Number(f.vlan) : undefined, ip: f.ip || "dhcp", gateway: f.ip !== "dhcp" && f.gateway ? f.gateway : undefined },
  };
}

function Secrets({ password, setPassword, keys, setKeys, passwordLabel }: { password: string; setPassword: (v: string) => void; keys: string; setKeys: (v: string) => void; passwordLabel: string }) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label={passwordLabel}><Input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
      <Field label={t("gx.ssh_keys")} hint={t("gx.ssh_keys_hint")}><Textarea rows={2} className="mono text-[12px]" value={keys} onChange={(e) => setKeys(e.target.value)} placeholder="ssh-ed25519 AAAA…" /></Field>
    </div>
  );
}

function Footer({ children }: { children: ReactNode }) {
  return <div className="sticky -bottom-4 z-10 -mx-5 -mb-4 mt-6 flex flex-wrap items-center justify-end gap-2 border-t border-line bg-raised px-5 py-3">{children}</div>;
}

/* ------------------------------------------------------------------ CT from template */
function TemplateTab({ envId, onDone }: { envId?: string; onDone: (d: Done) => void }) {
  const { t } = useTranslation();
  const c = useNodeChoice(envId);
  const lib = useQuery({ queryKey: ["templates"], queryFn: () => get<TemplateLibrary>("api/templates") });
  const [f, setF] = useState<Basics>(basicsDefaults(1024, 8));
  const set = (p: Partial<Basics>) => setF((o) => ({ ...o, ...p }));
  const [tpl, setTpl] = useState("");
  const [swap, setSwap] = useState("512");
  const [password, setPassword] = useState("");
  const [keys, setKeys] = useState("");
  const [unpriv, setUnpriv] = useState(true);
  const [nesting, setNesting] = useState(true);
  const onNode = c.res.data?.templates ?? [];
  const libItems = lib.data?.templates ?? [];
  useEffect(() => { if (!tpl && libItems.length) setTpl(`lib:${libItems[0].id}`); }, [libItems, tpl]);
  const status = (id: string) => libItems.find((x) => x.id === id)?.locations?.find((l) => l.node_id === c.nodeId)?.status;
  const go = useMutation({
    mutationFn: () => post<NodeJob>("api/guests/create/ct", {
      ...basicsBody(c, f), swap: Number(swap), unprivileged: unpriv, nesting,
      password: password || undefined, ssh_keys: keys || undefined,
      template_id: tpl.startsWith("lib:") ? tpl.slice(4) : undefined, ostemplate: tpl.startsWith("vol:") ? tpl.slice(4) : undefined,
    }),
    onSuccess: (job) => onDone({ job }),
  });
  const tplStatus = tpl.startsWith("lib:") ? status(tpl.slice(4)) : undefined;
  return (
    <div className="space-y-4">
      <Field label={t("gx.template_pick")} hint={tplStatus && tplStatus !== "current" ? t("gx.template_will_push") : undefined}>
        <Select value={tpl} onChange={(e) => setTpl(e.target.value)}>
          {!libItems.length && !onNode.length && <option value="">{t("gx.no_templates")}</option>}
          {libItems.length > 0 && (
            <optgroup label={t("tpl.library")}>
              {libItems.map((x) => { const st = status(x.id); return <option key={x.id} value={`lib:${x.id}`}>{x.name}{st ? ` · ${t(`tpl.status_${st}`)}` : ""}</option>; })}
            </optgroup>
          )}
          {onNode.length > 0 && (
            <optgroup label={t("gx.on_node")}>
              {onNode.map((x) => <option key={x.volid} value={`vol:${x.volid}`}>{x.filename}</option>)}
            </optgroup>
          )}
        </Select>
      </Field>
      <BasicsFields c={c} f={f} set={set} content="rootdir" nameLabel={t("gx.hostname")} />
      <div className="grid gap-3 sm:grid-cols-4">
        <Field label={t("gx.swap_mb")}><Input type="number" min={0} step={256} value={swap} onChange={(e) => setSwap(e.target.value)} /></Field>
        <div className="flex items-end gap-6 sm:col-span-3">
          <Checkbox checked={unpriv} onCheckedChange={setUnpriv} label={t("gx.unprivileged")} />
          <Checkbox checked={nesting} onCheckedChange={setNesting} label={t("gx.nesting")} description={t("gx.nesting_hint")} />
        </div>
      </div>
      <Secrets password={password} setPassword={setPassword} keys={keys} setKeys={setKeys} passwordLabel={t("gx.root_password")} />
      {go.isError && <Alert tone="offline">{errorMessage(go.error, t)}</Alert>}
      <Footer>
        <Button variant="primary" loading={go.isPending} disabled={!c.nodeId || !f.name || !tpl || (!password && !keys)} onClick={() => go.mutate()}><Box /> {t("gx.create_ct")}</Button>
      </Footer>
    </div>
  );
}

/* ------------------------------------------------------------------ community scripts */
function CommunityTab({ envId, onDone }: { envId?: string; onDone: (d: Done) => void }) {
  const { t, i18n } = useTranslation();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<"all" | "ct" | "vm">("ct");
  const [slug, setSlug] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["community-scripts"],
    queryFn: () => get<{ state: CommunityState; scripts: CommunityScript[] }>("api/community/scripts"),
    staleTime: 60_000,
  });
  const sync = useMutation({ mutationFn: () => post("api/community/sync"), onSuccess: () => list.refetch() });
  const rows = useMemo(() => {
    const n = q.trim().toLowerCase();
    return (list.data?.scripts ?? []).filter((s) => (kind === "all" || s.kind === kind) && (!n || s.name.toLowerCase().includes(n) || s.slug.includes(n) || s.tags.some((x) => x.includes(n))));
  }, [list.data, q, kind]);
  const st = list.data?.state;
  if (slug) return <CommunityInstall slug={slug} envId={envId} onBack={() => setSlug(null)} onDone={onDone} />;
  return (
    <div className="space-y-4">
      <Alert tone="info">
        {t("cs.intro")}{" "}
        <a href="https://community-scripts.org/" target="_blank" rel="noreferrer noopener" className="font-semibold underline">community-scripts.org</a>
      </Alert>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("cs.search")} className="h-9 w-64 pl-9" autoFocus />
        </div>
        <Segmented value={kind} onChange={setKind} options={[{ value: "ct", label: t("cs.kind_ct") }, { value: "vm", label: t("cs.kind_vm") }, { value: "all", label: t("px.all") }]} />
        <div className="flex-1" />
        <span className="text-xs text-muted">
          {st?.synced_at ? t("cs.synced", { count: st.count ?? 0, when: new Date(st.synced_at).toLocaleString(i18n.language === "nl" ? "nl-NL" : "en-GB") }) : t("cs.never_synced")}
        </span>
        <Button size="sm" onClick={() => sync.mutate()} loading={sync.isPending}>{t("cs.sync_now")}</Button>
      </div>
      {st?.error && <Alert tone="offline">{st.error}</Alert>}
      {sync.isError && <Alert tone="offline">{errorMessage(sync.error, t)}</Alert>}
      {list.isLoading ? <Spinner /> : !rows.length ? (
        <p className="py-8 text-center text-sm text-muted">{st?.synced_at ? t("cs.no_match") : t("cs.empty")}</p>
      ) : (
        <div className="grid max-h-[52vh] gap-2 overflow-y-auto pr-1 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map((s) => (
            <button key={s.slug} onClick={() => setSlug(s.slug)} className="cursor-pointer rounded-md border border-line bg-raised p-3 text-left transition-colors hover:border-nacht hover:bg-sunken dark:hover:border-lucht">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-semibold">{s.name}</span>
                <Badge tone={s.kind === "ct" ? "neutral" : "info"}>{s.kind.toUpperCase()}</Badge>
              </div>
              <div className="mt-1 text-xs text-muted">
                {[s.defaults.os && `${s.defaults.os} ${s.defaults.version ?? ""}`.trim(), s.defaults.cpu && `${s.defaults.cpu} CPU`, s.defaults.ram && `${s.defaults.ram} MB`, s.defaults.disk && `${s.defaults.disk} GB`].filter(Boolean).join(" · ")}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CommunityInstall({ slug, envId, onBack, onDone }: { slug: string; envId?: string; onBack: () => void; onDone: (d: Done) => void }) {
  const { t } = useTranslation();
  const c = useNodeChoice(envId);
  const info = useQuery({ queryKey: ["community-script", slug], queryFn: () => get<CommunityScriptFull>(`api/community/scripts/${slug}`), staleTime: 60_000 });
  const s = info.data;
  const [view, setView] = useState<"script" | "install">("script");
  const [mode, setMode] = useState<"default" | "advanced">("default");
  const [reviewed, setReviewed] = useState(false);
  const [o, setO] = useState({ hostname: "", vmid: "", cpu: "", ram: "", disk: "", storage: "", bridge: "", vlan: "", ip: "dhcp", gateway: "", ssh_key: "", password: "", unprivileged: true });
  const set = (p: Partial<typeof o>) => setO((x) => ({ ...x, ...p }));
  useEffect(() => { if (s) set({ hostname: s.slug.replace(/[^a-z0-9-]/g, "").slice(0, 63), unprivileged: s.defaults.unprivileged ?? true }); }, [s?.slug]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = useMutation({
    mutationFn: () => {
      const num = (v: string) => (v ? Number(v) : undefined);
      const el = document.getElementById("cs-term-size");
      const cols = el ? Math.max(80, Math.floor((el.clientWidth - 24) / 8.45)) : 120;
      return post<{ mode: string; job: NodeJob; session?: string }>(`api/community/scripts/${slug}/install`, {
        node_id: c.nodeId, sha256: s!.sha256, mode, cols, rows: 34,
        options: {
          hostname: o.hostname || undefined, vmid: num(o.vmid), cpu: num(o.cpu), ram: num(o.ram), disk: num(o.disk),
          storage: o.storage || undefined, bridge: o.bridge || undefined, vlan: num(o.vlan), ip: o.ip || undefined,
          gateway: o.ip !== "dhcp" && o.gateway ? o.gateway : undefined, ssh_key: o.ssh_key || undefined,
          password: o.password || undefined, unprivileged: s!.kind === "ct" ? o.unprivileged : undefined,
        },
      });
    },
    onSuccess: (r) => {
      if (r.session) onDone({ job: r.job, session: { session: r.session, kind: "pty", mode: "script", label: slug } });
      else onDone({ job: r.job });
    },
  });
  const d = s?.defaults;
  const bridges = c.res.data?.bridges ?? [];
  const storages = storagesFor(c.res.data, s?.kind === "vm" ? "images" : "rootdir");
  return (
    <div className="space-y-4" id="cs-term-size">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="ghost" onClick={onBack}>← {t("cs.all_scripts")}</Button>
        <span className="text-lg font-bold">{s?.name ?? slug}</span>
        {s && <Badge tone={s.kind === "ct" ? "neutral" : "info"}>{s.kind.toUpperCase()}</Badge>}
        <div className="flex-1" />
        {s && <a href={s.page_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-sm font-semibold text-accent-text hover:underline">{t("cs.website")} <ExternalLink className="size-3.5" /></a>}
      </div>
      {info.isLoading ? <Spinner /> : info.isError ? <Alert tone="offline">{errorMessage(info.error, t)}</Alert> : s && (
        <div className="grid gap-5 lg:grid-cols-5">
          <div className="space-y-3 lg:col-span-3">
            <Alert tone="warning" title={t("cs.root_title")}>{t("cs.root_text")}</Alert>
            <div className="flex items-center gap-2">
              <Segmented value={view} onChange={setView} options={[{ value: "script", label: s.path }, ...(s.install_script ? [{ value: "install" as const, label: t("cs.install_script") }] : [])]} />
              <a href={view === "script" ? s.script_url : s.install_url ?? s.script_url} target="_blank" rel="noreferrer noopener" className="text-xs text-muted hover:underline">raw</a>
            </div>
            <pre className="max-h-[46vh] overflow-auto rounded-md bg-nacht p-3 text-[11.5px] leading-[1.5] text-[#e8edf7]">{view === "script" ? s.script : s.install_script}</pre>
            <p className="mono break-all text-[11px] text-muted">sha256 {s.sha256}</p>
            <p className="text-xs text-muted">{t("cs.engine_hint")}</p>
          </div>
          <div className="space-y-3 lg:col-span-2">
            <NodePicker c={c} />
            <Field label={t("cs.mode")}>
              <Segmented value={mode} onChange={setMode} options={[{ value: "default", label: t("cs.mode_default") }, { value: "advanced", label: t("cs.mode_advanced") }]} />
            </Field>
            <p className="text-xs text-muted">{mode === "default" ? t("cs.mode_default_hint") : t("cs.mode_advanced_hint")}</p>
            {mode === "default" && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Field label={t("gx.hostname")}><Input value={o.hostname} onChange={(e) => set({ hostname: e.target.value.replace(/[^A-Za-z0-9-]/g, "") })} /></Field>
                  <Field label="VMID"><Input type="number" value={o.vmid} onChange={(e) => set({ vmid: e.target.value })} placeholder={c.res.data?.nextid ? String(c.res.data.nextid) : t("gx.auto")} /></Field>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <Field label="CPU"><Input type="number" min={1} value={o.cpu} onChange={(e) => set({ cpu: e.target.value })} placeholder={String(d?.cpu ?? "")} /></Field>
                  <Field label="RAM (MB)"><Input type="number" min={128} value={o.ram} onChange={(e) => set({ ram: e.target.value })} placeholder={String(d?.ram ?? "")} /></Field>
                  <Field label={t("gx.disk_gb")}><Input type="number" min={1} value={o.disk} onChange={(e) => set({ disk: e.target.value })} placeholder={String(d?.disk ?? "")} /></Field>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Field label={t("gx.storage")}>
                    <Select value={o.storage} onChange={(e) => set({ storage: e.target.value })}>
                      <option value="">{t("cs.script_default")}</option>
                      {storages.map((x) => <option key={x.storage} value={x.storage}>{x.storage}</option>)}
                    </Select>
                  </Field>
                  <Field label={t("gx.bridge")}>
                    <Select value={o.bridge} onChange={(e) => set({ bridge: e.target.value })}>
                      <option value="">{t("cs.script_default")}</option>
                      {bridges.map((b) => <option key={b} value={b}>{b}</option>)}
                    </Select>
                  </Field>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  <Field label={t("gx.ip")}><Input className="mono" value={o.ip} onChange={(e) => set({ ip: e.target.value.trim() })} /></Field>
                  <Field label={t("gx.gateway")}><Input className="mono" value={o.gateway} onChange={(e) => set({ gateway: e.target.value.trim() })} disabled={o.ip === "dhcp"} /></Field>
                  <Field label="VLAN"><Input type="number" value={o.vlan} onChange={(e) => set({ vlan: e.target.value })} /></Field>
                </div>
                {s.kind === "ct" && (
                  <>
                    <Field label={t("gx.root_password")} hint={t("cs.password_hint")}><Input type="password" autoComplete="new-password" value={o.password} onChange={(e) => set({ password: e.target.value })} /></Field>
                    <Field label={t("gx.ssh_keys")}><Textarea rows={2} className="mono text-[12px]" value={o.ssh_key} onChange={(e) => set({ ssh_key: e.target.value })} placeholder="ssh-ed25519 AAAA…" /></Field>
                    <Checkbox checked={o.unprivileged} onCheckedChange={(v) => set({ unprivileged: v })} label={t("gx.unprivileged")} />
                  </>
                )}
              </>
            )}
            <Checkbox checked={reviewed} onCheckedChange={setReviewed} label={t("cs.reviewed")} description={t("cs.reviewed_hint")} />
            {go.isError && <Alert tone="offline">{errorMessage(go.error, t)}</Alert>}
            <Button variant="primary" className="w-full" disabled={!reviewed || !c.nodeId} loading={go.isPending} onClick={() => go.mutate()}>
              <Sparkles /> {mode === "default" ? t("cs.install") : t("cs.install_advanced")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ VM from ISO / cloud image */
function IsoTab({ envId, onDone }: { envId?: string; onDone: (d: Done) => void }) {
  const { t } = useTranslation();
  const c = useNodeChoice(envId);
  const [f, setF] = useState<Basics>(basicsDefaults(4096, 32));
  const set = (p: Partial<Basics>) => setF((o) => ({ ...o, ...p }));
  const [src, setSrc] = useState<"existing" | "url">("existing");
  const [iso, setIso] = useState("");
  const [isoUrl, setIsoUrl] = useState("");
  const [isoStorage, setIsoStorage] = useState("local");
  const [ostype, setOstype] = useState("l26");
  const [bios, setBios] = useState("ovmf");
  const isos = c.res.data?.isos ?? [];
  useEffect(() => { if (isos.length && !isos.some((i) => i.volid === iso)) setIso(isos[0].volid); if (!isos.length && c.res.data) setSrc("url"); }, [isos.map((i) => i.volid).join(","), c.res.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = useMutation({
    mutationFn: () => post<NodeJob>("api/guests/create/vm", {
      ...basicsBody(c, f), source: "iso", ostype, bios,
      iso: src === "existing" ? iso : undefined, iso_url: src === "url" ? isoUrl : undefined, iso_storage: isoStorage,
    }),
    onSuccess: (job) => onDone({ job }),
  });
  return (
    <div className="space-y-4">
      <Field label={t("gx.iso")}>
        <Segmented value={src} onChange={setSrc} options={[{ value: "existing", label: t("gx.iso_existing") }, { value: "url", label: t("gx.iso_download") }]} />
      </Field>
      {src === "existing" ? (
        <Select value={iso} onChange={(e) => setIso(e.target.value)}>
          {!isos.length && <option value="">{t("gx.no_isos")}</option>}
          {isos.map((i) => <option key={i.volid} value={i.volid}>{i.volid} ({formatBytes(i.size)})</option>)}
        </Select>
      ) : (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t("gx.iso_url")} className="sm:col-span-2"><Input className="mono" value={isoUrl} onChange={(e) => setIsoUrl(e.target.value.trim())} placeholder="https://cdimage.debian.org/…/debian-13.0.0-amd64-netinst.iso" /></Field>
          <StorageSelect res={c.res.data} content="iso" value={isoStorage} onChange={setIsoStorage} label={t("gx.iso_storage")} />
        </div>
      )}
      <BasicsFields c={c} f={f} set={set} content="images" showIp={false} nameLabel={t("px.name")} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("gx.ostype")}>
          <Select value={ostype} onChange={(e) => setOstype(e.target.value)}>
            <option value="l26">Linux (6.x / 2.6+)</option><option value="win11">Windows 11 / 2022 / 2025</option><option value="win10">Windows 10 / 2016 / 2019</option><option value="other">{t("gx.other")}</option>
          </Select>
        </Field>
        <Field label="BIOS"><Select value={bios} onChange={(e) => setBios(e.target.value)}><option value="ovmf">UEFI (OVMF)</option><option value="seabios">SeaBIOS</option></Select></Field>
      </div>
      {ostype.startsWith("win") && <Alert tone="info">{t("gx.windows_hint")}</Alert>}
      {go.isError && <Alert tone="offline">{errorMessage(go.error, t)}</Alert>}
      <Footer>
        <Button variant="primary" loading={go.isPending} disabled={!c.nodeId || !f.name || (src === "existing" ? !iso : !/^https?:\/\//.test(isoUrl))} onClick={() => go.mutate()}><Disc /> {t("gx.create_vm")}</Button>
      </Footer>
    </div>
  );
}

function CloudTab({ envId, onDone }: { envId?: string; onDone: (d: Done) => void }) {
  const { t } = useTranslation();
  const c = useNodeChoice(envId);
  const images = useQuery({ queryKey: ["cloud-images"], queryFn: () => get<{ key: string; name: string; url: string }[]>("api/guests/cloud-images"), staleTime: Infinity });
  const [f, setF] = useState<Basics>(basicsDefaults(2048, 20));
  const set = (p: Partial<Basics>) => setF((o) => ({ ...o, ...p }));
  const [image, setImage] = useState("debian-13");
  const [url, setUrl] = useState("");
  const [user, setUser] = useState("admin");
  const [password, setPassword] = useState("");
  const [keys, setKeys] = useState("");
  const go = useMutation({
    mutationFn: () => post<NodeJob>("api/guests/create/vm", {
      ...basicsBody(c, f), source: "cloud", ostype: "l26", bios: "ovmf",
      cloud_image: image === "custom" ? undefined : image, cloud_url: image === "custom" ? url : undefined,
      ci_user: user, ci_password: password || undefined, ssh_keys: keys || undefined,
    }),
    onSuccess: (job) => onDone({ job }),
  });
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={t("gx.cloud_image")}>
          <Select value={image} onChange={(e) => setImage(e.target.value)}>
            {images.data?.map((i) => <option key={i.key} value={i.key}>{i.name}</option>)}
            <option value="custom">{t("gx.cloud_custom")}</option>
          </Select>
        </Field>
        {image === "custom" && <Field label={t("gx.cloud_url")} className="sm:col-span-2"><Input className="mono" value={url} onChange={(e) => setUrl(e.target.value.trim())} placeholder="https://…/image.qcow2" /></Field>}
      </div>
      <p className="text-xs text-muted">{t("gx.cloud_hint")}</p>
      <BasicsFields c={c} f={f} set={set} content="images" nameLabel={t("gx.hostname")} />
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={t("gx.ci_user")}><Input className="mono" value={user} onChange={(e) => setUser(e.target.value.toLowerCase())} /></Field>
      </div>
      <Secrets password={password} setPassword={setPassword} keys={keys} setKeys={setKeys} passwordLabel={t("gx.ci_password")} />
      {go.isError && <Alert tone="offline">{errorMessage(go.error, t)}</Alert>}
      <Footer>
        <Button variant="primary" loading={go.isPending} disabled={!c.nodeId || !f.name || (!password && !keys) || (image === "custom" && !url)} onClick={() => go.mutate()}><Cloud /> {t("gx.create_vm")}</Button>
      </Footer>
    </div>
  );
}

/* ------------------------------------------------------------------ result */
function Result({ done, onClose }: { done: Done; onClose: () => void }) {
  const { t } = useTranslation();
  const [job, setJob] = useState<NodeJob | null>(null);
  const vmid = (job?.result as { vmid?: number } | undefined)?.vmid;
  return (
    <div className="space-y-4">
      {done.session ? (
        <>
          <Alert tone="info">{t("cs.advanced_running")}</Alert>
          <ConsoleView initial={done.session} className="h-[60vh]" />
          {done.job && <p className="text-xs text-muted">{t("cs.advanced_log")}</p>}
        </>
      ) : done.job && <JobView jobId={done.job.id} onDone={setJob} />}
      <div className="flex flex-wrap justify-end gap-2">
        {job?.status === "success" && vmid && done.job && (
          <Link to={`/proxmox/guests/${done.job.node_id}/${vmid}`} onClick={onClose}><Button variant="primary">{t("gx.open_guest")}</Button></Link>
        )}
        <Button onClick={onClose}>{t("common.done")}</Button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ dialog */
export function CreateGuestDialog({ open, onOpenChange, environmentId }: { open: boolean; onOpenChange: (v: boolean) => void; environmentId?: string }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState("template");
  const [done, setDone] = useState<Done | null>(null);
  useEffect(() => { if (open) setDone(null); }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange} xl title={done ? t("gx.creating") : t("gx.create")} description={done ? undefined : t("gx.create_desc")}>
      {done ? <Result done={done} onClose={() => onOpenChange(false)} /> : (
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
            <Tabs.Trigger value="template" className={tabCls}><Box /> {t("gx.tab_template")}</Tabs.Trigger>
            <Tabs.Trigger value="community" className={tabCls}><Store /> {t("gx.tab_community")}</Tabs.Trigger>
            <Tabs.Trigger value="iso" className={tabCls}><Disc /> {t("gx.tab_iso")}</Tabs.Trigger>
            <Tabs.Trigger value="cloud" className={tabCls}><Cloud /> {t("gx.tab_cloud")}</Tabs.Trigger>
            <Tabs.Trigger value="github" className={tabCls} disabled title={t("nav.soon_hint")}><GitBranch /> GitHub <span className="rounded-full bg-line px-1.5 text-[10px] uppercase">{t("gx.phase4")}</span></Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="template"><TemplateTab envId={environmentId} onDone={setDone} /></Tabs.Content>
          <Tabs.Content value="community"><CommunityTab envId={environmentId} onDone={setDone} /></Tabs.Content>
          <Tabs.Content value="iso"><IsoTab envId={environmentId} onDone={setDone} /></Tabs.Content>
          <Tabs.Content value="cloud"><CloudTab envId={environmentId} onDone={setDone} /></Tabs.Content>
        </Tabs.Root>
      )}
    </Dialog>
  );
}
