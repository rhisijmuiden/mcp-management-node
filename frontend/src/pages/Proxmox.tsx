import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpCircle, Bell, Boxes, Building2, Home, Mail, Package, Pencil, Plus, Search, Server, ShieldAlert, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { EnvDot, EnvironmentDialog, NodeCard, PairDialog, StatusDot, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Environment, NodeInfo, NotifyConfig, Overview } from "@/lib/types";
import { cn, timeAgo } from "@/lib/utils";

const useOverview = (envId?: string) =>
  useQuery({
    queryKey: ["overview", envId ?? "all"],
    queryFn: () => get<Overview>(`api/proxmox/overview${envId ? `?environment_id=${envId}` : ""}`),
    refetchInterval: 30_000,
  });

function EnvChips({ envs, value, onChange }: { envs: Environment[]; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  const chip = (active: boolean) =>
    cn("inline-flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-[13px] font-semibold transition-colors",
      active ? "border-nacht bg-nacht text-white dark:border-lucht dark:bg-lucht dark:text-nacht" : "border-line bg-raised hover:bg-sunken");
  return (
    <div className="mb-5 flex flex-wrap gap-2">
      <button className={chip(value === "")} onClick={() => onChange("")}>{t("px.all_envs")}</button>
      {envs.map((e) => (
        <button key={e.id} className={chip(value === e.id)} onClick={() => onChange(e.id)}>
          <EnvDot color={e.color} /> {e.name}
          <span className="text-[11px] opacity-70">{e.nodes_online}/{e.node_count}</span>
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ overview */
export function ProxmoxOverviewPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [env, setEnv] = useState(() => localStorage.getItem("mmn.px.env") ?? "");
  const ov = useOverview(env || undefined);
  const [pair, setPair] = useState(false);
  useEffect(() => {
    try {
      localStorage.setItem("mmn.px.env", env);
    } catch {
      /* ignore */
    }
  }, [env]);
  const tot = ov.data?.totals;
  const clusters = useMemo(() => {
    const groups = new Map<string, NodeInfo[]>();
    for (const n of ov.data?.nodes ?? []) {
      const key = `${n.environment_name ?? ""}${n.cluster_name ? ` · ${n.cluster_name}` : ""}`;
      groups.set(key, [...(groups.get(key) ?? []), n]);
    }
    return [...groups.entries()];
  }, [ov.data]);

  return (
    <>
      <PageHeader
        title={t("px.overview_title")}
        description={t("px.overview_subtitle")}
        actions={admin && <Button variant="primary" onClick={() => setPair(true)}><Plus /> {t("px.add_node")}</Button>}
      />
      {ov.data && ov.data.environments.length > 1 && <EnvChips envs={ov.data.environments} value={env} onChange={setEnv} />}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("px.k_nodes")} value={tot ? `${tot.online} / ${tot.nodes}` : "…"} hint={tot && tot.online < tot.nodes ? t("px.k_nodes_offline", { count: tot.nodes - tot.online }) : t("px.k_nodes_ok")} tone={tot && tot.online < tot.nodes ? "offline" : "online"} icon={<Server />} />
        <Stat label={t("px.k_vms")} value={tot ? `${tot.vms_running} / ${tot.vms_total}` : "…"} hint={t("px.running")} tone="info" icon={<Boxes />} />
        <Stat label={t("px.k_cts")} value={tot ? `${tot.cts_running} / ${tot.cts_total}` : "…"} hint={t("px.running")} tone="info" icon={<Package />} />
        <Link to="/proxmox/updates">
          <Stat label={t("px.k_updates")} value={tot ? tot.updates : "…"} hint={tot ? t("px.k_updates_hint", { nodes: tot.nodes_with_updates, reboot: tot.reboot_required }) : ""} tone={tot?.updates ? "accent" : "online"} icon={<ArrowUpCircle />} />
        </Link>
      </div>

      {ov.isLoading ? <div className="mt-8"><Spinner /></div> : !ov.data?.nodes.length ? (
        <Card className="mt-6">
          <EmptyState icon={<Server />} title={t("px.empty_title")} action={admin && <Button variant="primary" onClick={() => setPair(true)}><Plus /> {t("px.add_node")}</Button>}>
            {t("px.empty_text")}
          </EmptyState>
        </Card>
      ) : (
        clusters.map(([title, list]) => (
          <section key={title} className="mt-8">
            <h2 className="mb-3 text-[13px] font-bold uppercase tracking-[0.08em] text-muted">{title}</h2>
            <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
              {list.map((n) => <NodeCard key={n.id} n={n} admin={admin} />)}
            </div>
          </section>
        ))
      )}
      {tot && tot.agents_outdated > 0 && <Alert tone="info" className="mt-6">{t("px.agents_outdated_hint", { count: tot.agents_outdated, version: ov.data?.agent_version })}</Alert>}
      <PairDialog open={pair} onOpenChange={setPair} environmentId={env || undefined} />
    </>
  );
}

/* ------------------------------------------------------------------ environments */
export function EnvironmentsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const envs = useEnvironments();
  const [edit, setEdit] = useState<Environment | null | "new">(null);
  return (
    <>
      <PageHeader title={t("px.envs_title")} description={t("px.envs_subtitle")} actions={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("px.env_add")}</Button>} />
      {envs.isLoading ? <Spinner /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {envs.data?.map((e) => (
            <Link key={e.id} to={`/proxmox/environments/${e.id}`}>
              <Card className="h-full transition-shadow hover:shadow-lg">
                <CardHeader
                  icon={e.kind === "home" ? <Home /> : <Building2 />}
                  title={<span className="flex items-center gap-2"><EnvDot color={e.color} /> {e.name}</span>}
                  description={e.kind === "home" ? t("px.kind_home") : t("px.kind_customer")}
                />
                <CardBody className="grid grid-cols-3 gap-3 text-sm">
                  <div><div className="text-xs text-muted">{t("px.nodes")}</div><div className="font-bold">{e.nodes_online}/{e.node_count}</div></div>
                  <div><div className="text-xs text-muted">{t("px.updates")}</div><div className="font-bold">{e.updates}</div></div>
                  <div><div className="text-xs text-muted">{t("px.mail")}</div><div className="font-bold">{e.notify_enabled ? t("common.on") : t("common.off")}</div></div>
                  {e.description && <p className="col-span-3 text-muted">{e.description}</p>}
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      )}
      <EnvironmentDialog open={edit !== null} onOpenChange={(v) => !v && setEdit(null)} env={edit === "new" ? null : edit} />
    </>
  );
}

export function NodesTable({ nodes, showEnv = true }: { nodes: NodeInfo[]; showEnv?: boolean }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  return (
    <Table>
      <thead>
        <tr>
          <Th>{t("px.node")}</Th>
          {showEnv && <Th>{t("px.environment")}</Th>}
          <Th>{t("px.version")}</Th>
          <Th>CPU</Th>
          <Th>{t("px.memory")}</Th>
          <Th>{t("px.updates")}</Th>
          <Th>{t("px.agent")}</Th>
          <Th>{t("px.last_seen")}</Th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((n) => (
          <Tr key={n.id} className="cursor-pointer" onClick={() => navigate(`/proxmox/nodes/${n.id}`)}>
            <Td><span className="flex items-center gap-2 font-semibold"><StatusDot online={n.online} /> {n.name}</span><span className="text-xs text-muted">{n.cluster_name}</span></Td>
            {showEnv && <Td>{n.environment_name}</Td>}
            <Td className="mono text-[12.5px]">{n.info?.pve_version ? `PVE ${n.info.pve_version}` : n.info?.os_pretty_name ?? "–"}</Td>
            <Td>{n.metrics?.cpu_percent != null ? `${n.metrics.cpu_percent.toFixed(0)}%` : "–"}</Td>
            <Td>{n.metrics?.mem_total ? `${(((n.metrics.mem_used ?? 0) / n.metrics.mem_total) * 100).toFixed(0)}%` : "–"}</Td>
            <Td>{n.updates.count ? <Badge tone="accent">{n.updates.count}</Badge> : <span className="text-muted">0</span>} {n.updates.reboot_required && <Badge tone="warning">{t("px.reboot")}</Badge>}</Td>
            <Td className="mono text-[12.5px]">{n.agent_version ?? "–"} {n.agent_update_available && <Badge tone="info">↑</Badge>}</Td>
            <Td className="text-muted">{n.online ? t("px.online") : timeAgo(n.last_seen_at, i18n.language)}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

export function NodesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [env, setEnv] = useState("");
  const ov = useOverview(env || undefined);
  const [q, setQ] = useState("");
  const [pair, setPair] = useState(false);
  const list = (ov.data?.nodes ?? []).filter((n) => !q || `${n.name} ${n.hostname} ${n.cluster_name}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <PageHeader title={t("px.nodes_title")} description={t("px.nodes_subtitle")} actions={admin && <Button variant="primary" onClick={() => setPair(true)}><Plus /> {t("px.add_node")}</Button>} />
      {ov.data && ov.data.environments.length > 1 && <EnvChips envs={ov.data.environments} value={env} onChange={setEnv} />}
      <Card>
        <div className="border-b border-line p-3">
          <div className="relative max-w-sm">
            <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("px.search_nodes")} />
          </div>
        </div>
        {ov.isLoading ? <CardBody><Spinner /></CardBody> : list.length ? <NodesTable nodes={list} /> : <EmptyState icon={<Server />} title={t("px.empty_title")}>{t("px.empty_text")}</EmptyState>}
      </Card>
      <PairDialog open={pair} onOpenChange={setPair} environmentId={env || undefined} />
    </>
  );
}

/* ------------------------------------------------------------------ environment detail */
const TYPE_KEYS = ["vzdump", "replication", "fencing", "package-updates", "system-mail"];
const SEV_KEYS = ["info", "notice", "warning", "error", "unknown"];

function NotifyCard({ env }: { env: Environment }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["env-notify", env.id], queryFn: () => get<NotifyConfig>(`api/environments/${env.id}/notifications`) });
  const [f, setF] = useState<NotifyConfig | null>(null);
  const [pw, setPw] = useState("");
  const [rcpt, setRcpt] = useState("");
  useEffect(() => {
    if (q.data) {
      setF(q.data);
      setRcpt(q.data.mailto.join(", "));
    }
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => put<{ push: { node_id: string; ok: boolean; error?: string }[] }>(`api/environments/${env.id}/notifications`, {
      ...f, mailto: rcpt.split(/[,;\s]+/).filter(Boolean), password: pw ? pw : undefined,
      types_available: undefined, severities_available: undefined, password_set: undefined,
    }),
    onSuccess: (r) => {
      const ok = r.push.filter((x) => x.ok).length;
      const bad = r.push.length - ok;
      toast.success(t("common.saved"), { description: r.push.length ? t("px.notify_pushed", { ok, bad }) : undefined });
      setPw("");
      qc.invalidateQueries({ queryKey: ["env-notify", env.id] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
  const push = useMutation({
    mutationFn: () => post<{ ok: boolean; error?: string }[]>(`api/environments/${env.id}/notifications/push`),
    onSuccess: (r) => toast.success(t("px.notify_pushed", { ok: r.filter((x) => x.ok).length, bad: r.filter((x) => !x.ok).length })),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!f) return <Card><CardBody><Spinner /></CardBody></Card>;
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <Card>
      <CardHeader icon={<Bell />} title={t("px.notify_title")} description={t("px.notify_desc")} action={<Switch checked={f.enabled} onCheckedChange={(v) => setF({ ...f, enabled: v })} />} />
      <CardBody className="space-y-5">
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
        <Field label={t("px.notify_rcpt")} hint={t("px.notify_rcpt_hint")}>
          <Input value={rcpt} onChange={(e) => setRcpt(e.target.value)} placeholder="ops@example.com, beheer@klant.nl" />
        </Field>
        <div>
          <div className="mb-2 text-[13px] font-semibold">{t("px.notify_types")}</div>
          <div className="grid gap-3 sm:grid-cols-2">
            {TYPE_KEYS.map((k) => <Checkbox key={k} checked={f.types.includes(k)} onCheckedChange={() => setF({ ...f, types: toggle(f.types, k) })} label={t(`px.type_${k.replace("-", "_")}`)} description={t(`px.type_${k.replace("-", "_")}_hint`)} />)}
          </div>
        </div>
        <div>
          <div className="mb-2 text-[13px] font-semibold">{t("px.notify_sev")}</div>
          <div className="flex flex-wrap gap-4">
            {SEV_KEYS.map((k) => <Checkbox key={k} checked={f.severities.includes(k)} onCheckedChange={() => setF({ ...f, severities: toggle(f.severities, k) })} label={t(`px.sev_${k}`)} />)}
          </div>
        </div>
        <div className="rounded-md border border-line p-4">
          <Switch checked={f.use_app_smtp} onCheckedChange={(v) => setF({ ...f, use_app_smtp: v })} label={t("px.notify_app_smtp")} description={t("px.notify_app_smtp_hint")} />
          {!f.use_app_smtp && (
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <Field label={t("mail.host")}><Input value={f.server} onChange={(e) => setF({ ...f, server: e.target.value })} placeholder="smtp.example.com" /></Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label={t("px.smtp_port")}><Input type="number" value={f.port} onChange={(e) => setF({ ...f, port: Number(e.target.value) })} /></Field>
                <Field label={t("mail.security")}>
                  <Select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value as NotifyConfig["mode"] })}>
                    <option value="starttls">STARTTLS</option><option value="tls">TLS (SMTPS)</option><option value="insecure">{t("px.mode_insecure")}</option>
                  </Select>
                </Field>
              </div>
              <Field label={t("px.smtp_username")}><Input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoComplete="off" /></Field>
              <Field label={t("px.smtp_password")} hint={f.password_set ? t("common.secret_keep") : undefined}><Input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" /></Field>
              <Field label={t("mail.from_address")}><Input value={f.from_address} onChange={(e) => setF({ ...f, from_address: e.target.value })} placeholder="pve@example.com" /></Field>
              <Field label={t("px.notify_author")}><Input value={f.author} onChange={(e) => setF({ ...f, author: e.target.value })} placeholder={`Proxmox VE · ${env.name}`} /></Field>
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}><Mail /> {t("px.notify_save")}</Button>
          <Button loading={push.isPending} onClick={() => push.mutate()}>{t("px.notify_push")}</Button>
        </div>
        <p className="text-xs text-muted">{t("px.notify_note")}</p>
      </CardBody>
    </Card>
  );
}

export function EnvironmentDetailPage() {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const env = useQuery({ queryKey: ["environment", id], queryFn: () => get<Environment>(`api/environments/${id}`) });
  const ov = useOverview(id);
  const [edit, setEdit] = useState(false);
  const [pair, setPair] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const remove = useMutation({
    mutationFn: () => del(`api/environments/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["environments"] });
      navigate("/proxmox/environments");
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (env.isLoading) return <Spinner />;
  if (!env.data) return <Alert tone="offline">{t("px.not_found")}</Alert>;
  const e = env.data;
  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3"><EnvDot color={e.color} className="size-3.5" /> {e.name}</span>}
        description={e.description || (e.kind === "home" ? t("px.kind_home") : t("px.kind_customer"))}
        actions={admin && (
          <>
            <Button onClick={() => setEdit(true)}><Pencil /> {t("common.edit")}</Button>
            <Button onClick={() => setConfirmDel(true)} disabled={e.node_count > 0} title={e.node_count ? t("px.env_delete_hint") : undefined}><Trash2 /> {t("common.delete")}</Button>
            <Button variant="primary" onClick={() => setPair(true)}><Plus /> {t("px.add_node")}</Button>
          </>
        )}
      />
      <div className="grid gap-6 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader icon={<Server />} title={t("px.nodes")} description={e.clusters.length ? `${t("px.clusters")}: ${e.clusters.join(", ")}` : undefined} />
          {ov.data?.nodes.length ? <NodesTable nodes={ov.data.nodes} showEnv={false} /> : <EmptyState icon={<Server />} title={t("px.empty_title")}>{t("px.empty_env_text")}</EmptyState>}
        </Card>
        <div className="xl:col-span-2">{admin && <NotifyCard env={e} />}</div>
      </div>
      <EnvironmentDialog open={edit} onOpenChange={setEdit} env={e} />
      <PairDialog open={pair} onOpenChange={setPair} environmentId={e.id} />
      <Dialog open={confirmDel} onOpenChange={setConfirmDel} title={t("px.env_delete_title", { name: e.name })}
        footer={<><Button onClick={() => setConfirmDel(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("px.env_delete_text")}</p>
      </Dialog>
    </>
  );
}

/* ------------------------------------------------------------------ updates across nodes */
interface PkgRow { node_id: string; node: string; environment: string; online: boolean; error?: string; packages?: { name: string; version: string; update_available?: string }[] }

export function UpdatesPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const qc = useQueryClient();
  const [env, setEnv] = useState("");
  const ov = useOverview(env || undefined);
  const [confirm, setConfirm] = useState<NodeInfo | null>(null);
  const [typed, setTyped] = useState("");
  const [pattern, setPattern] = useState("");
  const search = useMutation({ mutationFn: (p: string) => get<PkgRow[]>(`api/packages/search?pattern=${encodeURIComponent(p)}${env ? `&environment_id=${env}` : ""}`) });
  const apply = useMutation({
    mutationFn: (n: NodeInfo) => post(`api/nodes/${n.id}/updates/apply`),
    onSuccess: (_, n) => {
      toast.success(t("px.updates_started", { node: n.name }));
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const refresh = useMutation({
    mutationFn: (n: NodeInfo) => post(`api/nodes/${n.id}/updates/refresh`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["overview"] }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const nodes = [...(ov.data?.nodes ?? [])].sort((a, b) => b.updates.count - a.updates.count);
  return (
    <>
      <PageHeader title={t("px.updates_title")} description={t("px.updates_subtitle")} />
      {ov.data && ov.data.environments.length > 1 && <EnvChips envs={ov.data.environments} value={env} onChange={setEnv} />}
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader icon={<ArrowUpCircle />} title={t("px.updates_per_node")} />
          <Table>
            <thead><tr><Th>{t("px.node")}</Th><Th>{t("px.updates")}</Th><Th>{t("px.checked")}</Th><Th className="text-right">{t("common.actions")}</Th></tr></thead>
            <tbody>
              {nodes.map((n) => (
                <Tr key={n.id}>
                  <Td><Link to={`/proxmox/nodes/${n.id}?tab=updates`} className="flex items-center gap-2 font-semibold hover:underline"><StatusDot online={n.online} /> {n.name}</Link><span className="text-xs text-muted">{n.environment_name}</span></Td>
                  <Td>{n.updates.count ? <Badge tone="accent">{t("px.updates_count", { count: n.updates.count })}</Badge> : <Badge tone="online" dot>{t("px.up_to_date")}</Badge>} {n.updates.reboot_required && <Badge tone="warning">{t("px.reboot_required")}</Badge>}{n.updates.error && <div className="mt-1 text-xs text-offline">{n.updates.error}</div>}</Td>
                  <Td className="text-muted">{timeAgo(n.updates.checked_at, i18n.language)}</Td>
                  <Td className="text-right">
                    {admin && (
                      <div className="flex justify-end gap-2">
                        <Button size="sm" disabled={!n.online} loading={refresh.isPending && refresh.variables?.id === n.id} onClick={() => refresh.mutate(n)}>{t("px.check")}</Button>
                        <Button size="sm" variant="dark" disabled={!n.online || !n.updates.count} onClick={() => { setTyped(""); setConfirm(n); }}>{t("px.install")}</Button>
                      </div>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Card>
        {admin && (
          <Card>
            <CardHeader icon={<ShieldAlert />} title={t("px.pkg_search_title")} description={t("px.pkg_search_desc")} />
            <CardBody className="space-y-3">
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (pattern.trim()) search.mutate(pattern.trim()); }}>
                <Input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="nginx*, openssl, libssh*" />
                <Button type="submit" loading={search.isPending}><Search /></Button>
              </form>
              {search.isError && <Alert tone="offline">{errorMessage(search.error, t)}</Alert>}
              {search.data && (
                <div className="divide-y divide-line text-sm">
                  {search.data.map((r) => (
                    <div key={r.node_id} className="py-2">
                      <div className="font-semibold">{r.node} <span className="text-xs font-normal text-muted">{r.environment}</span></div>
                      {!r.online ? <div className="text-xs text-muted">{t("px.offline")}</div> : r.error ? <div className="text-xs text-offline">{r.error}</div> : r.packages?.length ? r.packages.map((p) => (
                        <div key={p.name} className="mono flex justify-between gap-2 text-[12px]"><span>{p.name}</span><span>{p.version}{p.update_available && <span className="text-accent-text"> → {p.update_available}</span>}</span></div>
                      )) : <div className="text-xs text-muted">{t("px.pkg_not_installed")}</div>}
                    </div>
                  ))}
                </div>
              )}
            </CardBody>
          </Card>
        )}
      </div>
      <Dialog open={!!confirm} onOpenChange={(v) => !v && setConfirm(null)} title={t("px.install_title", { node: confirm?.name })}
        footer={<><Button onClick={() => setConfirm(null)}>{t("common.cancel")}</Button><Button variant="primary" disabled={typed !== confirm?.name} loading={apply.isPending} onClick={() => confirm && apply.mutate(confirm)}>{t("px.install")}</Button></>}>
        <div className="space-y-3 text-sm">
          <p>{t("px.install_text", { count: confirm?.updates.count })}</p>
          <Field label={t("px.type_name", { name: confirm?.name })}><Input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus /></Field>
        </div>
      </Dialog>
    </>
  );
}
