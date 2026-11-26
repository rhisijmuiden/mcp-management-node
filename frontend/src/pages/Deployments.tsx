import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, GitBranch, Pencil, Play, Plus, Rocket, Trash2, Undo2, Wand2, Webhook, XCircle } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { JobView } from "@/components/jobs";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { DeployPreset, Deployment, DeploymentRun, GithubAccount, GithubRepo, GithubStatus, Guest, NodeInfo } from "@/lib/types";
import { cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ shared form */
export interface DeployForm {
  name: string; repo: string; channel: "prod" | "test"; branch_prod: string; branch_test: string;
  target: "container" | "server"; node_id: string; vmid: number; path: string; preset: DeployPreset; setup_cmd: string; deploy_cmd: string;
  service: string; env: string | null; auto_deploy: "off" | "webhook" | "poll"; snapshot: boolean; rollback: boolean; mail_to: string[];
}

export const deployDefaults = (d?: Deployment | null): DeployForm => ({
  name: d?.name ?? "", repo: d?.repo ?? "", channel: d?.channel ?? "prod", branch_prod: d?.branch_prod ?? "main", branch_test: d?.branch_test ?? "test",
  target: d?.target ?? "container", node_id: d?.node_id ?? "", vmid: d?.vmid ?? 0, path: d?.path ?? "", preset: d?.preset ?? "script", setup_cmd: d?.setup_cmd ?? "", deploy_cmd: d?.deploy_cmd ?? "bash ./deploy/install.sh",
  service: d?.service ?? "", env: d ? null : "", auto_deploy: d?.auto_deploy ?? "off", snapshot: d?.snapshot ?? true, rollback: d?.rollback ?? true, mail_to: d?.mail_to ?? [],
});

export function deployBody(f: DeployForm) {
  return { ...f, path: f.path || null, service: f.service || null, env: f.env };
}

export const useGithub = () => useQuery({ queryKey: ["github", "status"], queryFn: () => get<GithubStatus>("api/github") });

/** Repo, branches, preset, commands, env and auto-deploy. Used by the deployment dialog and the Create dialog's GitHub tab. */
export function DeployFields({ f, set, editing }: { f: DeployForm; set: (p: Partial<DeployForm>) => void; editing?: boolean }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const repos = useQuery({ queryKey: ["gh-repos", q], queryFn: () => get<GithubRepo[]>(`api/github/repos${q ? `?q=${encodeURIComponent(q)}` : ""}`), staleTime: 60_000 });
  const branches = useQuery({ queryKey: ["gh-branches", f.repo], queryFn: () => get<string[]>(`api/github/branches?repo=${encodeURIComponent(f.repo)}`), enabled: /.+\/.+/.test(f.repo), staleTime: 60_000, retry: false });
  const detect = useMutation({
    mutationFn: () => get<{ preset: DeployPreset; setup_cmd: string; deploy_cmd: string; files: string[] }>(`api/github/detect?repo=${encodeURIComponent(f.repo)}&branch=${encodeURIComponent(f.channel === "prod" ? f.branch_prod : f.branch_test)}`),
    onSuccess: (r) => { set({ preset: r.preset, setup_cmd: r.setup_cmd, deploy_cmd: r.deploy_cmd }); toast.success(t("dep.detected", { preset: t(`dep.preset_${r.preset}`) })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const presets = useQuery({ queryKey: ["deploy-presets"], queryFn: () => get<Record<DeployPreset, { setup: string; deploy: string }>>("api/deployments/presets"), staleTime: Infinity });
  const pickRepo = (full: string) => {
    const r = repos.data?.find((x) => x.full_name === full);
    const short = full.split("/")[1]?.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+/, "").slice(0, 40) ?? "";
    set({ repo: full, name: f.name || short, path: f.path || `/opt/${short}`, branch_prod: r?.default_branch ?? f.branch_prod });
  };
  const brList = branches.data ?? [];
  const branchSel = (key: "branch_prod" | "branch_test", label: string) => (
    <Field label={label}>
      {brList.length ? (
        <Select value={f[key]} onChange={(e) => set({ [key]: e.target.value } as Partial<DeployForm>)}>
          {!brList.includes(f[key]) && <option value={f[key]}>{f[key]} ({t("dep.missing_branch")})</option>}
          {brList.map((b) => <option key={b}>{b}</option>)}
        </Select>
      ) : <Input value={f[key]} onChange={(e) => set({ [key]: e.target.value } as Partial<DeployForm>)} className="mono" />}
    </Field>
  );
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("dep.repo")} hint={repos.isError ? errorMessage(repos.error, t) : undefined}>
          <div className="space-y-2">
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("dep.search_repos")} />
            <Select value={f.repo} onChange={(e) => pickRepo(e.target.value)}>
              <option value="">…</option>
              {f.repo && !repos.data?.some((r) => r.full_name === f.repo) && <option value={f.repo}>{f.repo}</option>}
              {(repos.data ?? []).filter((r) => !r.archived).map((r) => <option key={r.full_name} value={r.full_name}>{r.full_name}{r.private ? " 🔒" : ""}</option>)}
            </Select>
          </div>
        </Field>
        <Field label={t("dep.name")} hint={t("dep.name_hint")}><Input value={f.name} onChange={(e) => set({ name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 40) })} className="mono" disabled={editing} /></Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        {branchSel("branch_prod", t("dep.branch_prod"))}
        {branchSel("branch_test", t("dep.branch_test"))}
        <Field label={t("dep.channel")}>
          <Segmented value={f.channel} onChange={(v) => set({ channel: v })} options={[{ value: "prod", label: t("dep.prod") }, { value: "test", label: t("dep.test") }]} />
        </Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={t("dep.path")} hint={t("dep.path_hint")}><Input value={f.path} onChange={(e) => set({ path: e.target.value.trim() })} className="mono" placeholder="/opt/app" /></Field>
        <Field label={t("dep.preset")}>
          <Select value={f.preset} onChange={(e) => { const p = e.target.value as DeployPreset; const d = presets.data?.[p]; set({ preset: p, ...(d ? { setup_cmd: d.setup, deploy_cmd: d.deploy } : {}) }); }}>
            {(["script", "python", "node", "static", "custom"] as DeployPreset[]).map((p) => <option key={p} value={p}>{t(`dep.preset_${p}`)}</option>)}
          </Select>
        </Field>
        <div className="flex items-end"><Button onClick={() => detect.mutate()} loading={detect.isPending} disabled={!f.repo}><Wand2 /> {t("dep.detect")}</Button></div>
      </div>
      <Field label={t("dep.setup_cmd")} hint={t("dep.setup_cmd_hint")}><Textarea rows={2} value={f.setup_cmd} onChange={(e) => set({ setup_cmd: e.target.value })} className="mono text-[12px]" /></Field>
      <Field label={t("dep.deploy_cmd")} hint={t("dep.deploy_cmd_hint")}><Textarea rows={3} value={f.deploy_cmd} onChange={(e) => set({ deploy_cmd: e.target.value })} className="mono text-[12px]" /></Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("dep.service")} hint={t("dep.service_hint")}><Input value={f.service} onChange={(e) => set({ service: e.target.value.trim() })} className="mono" placeholder="myapp.service" /></Field>
        <Field label={t("dep.mail_to")}><Input value={f.mail_to.join(", ")} onChange={(e) => set({ mail_to: e.target.value.split(/[,;\s]+/).filter(Boolean) })} /></Field>
      </div>
      <Field label={t("dep.env")} hint={editing && f.env === null ? t("dep.env_keep") : t("dep.env_hint")}>
        {editing && f.env === null
          ? <Button size="sm" onClick={() => set({ env: "" })}>{t("dep.env_replace")}</Button>
          : <Textarea rows={3} value={f.env ?? ""} onChange={(e) => set({ env: e.target.value })} className="mono text-[12px]" placeholder={"DATABASE_URL=postgresql://…\nPORT=8080"} />}
      </Field>
      <Field label={t("dep.auto")}>
        <Segmented value={f.auto_deploy} onChange={(v) => set({ auto_deploy: v })} options={[{ value: "off", label: t("dep.auto_off") }, { value: "webhook", label: t("dep.auto_webhook") }, { value: "poll", label: t("dep.auto_poll") }]} />
      </Field>
      <p className="-mt-2 text-xs text-muted">{t(`dep.auto_${f.auto_deploy}_hint`)}</p>
      {f.target === "server" ? <p className="text-xs text-muted">{t("dep.server_no_snapshot")}</p> : <div className="flex flex-wrap gap-6">
        <Checkbox checked={f.snapshot} onCheckedChange={(v) => set({ snapshot: v })} label={t("dep.snapshot")} description={t("dep.snapshot_hint")} />
        <Checkbox checked={f.rollback} onCheckedChange={(v) => set({ rollback: v })} label={t("dep.rollback")} description={t("dep.rollback_hint")} />
      </div>}
    </div>
  );
}

/** The GitHub tokens (v0.27.0: several, e.g. your own account and an organisation). `compact` only shows
 * itself when nothing is connected or a token needs attention. */
export function GithubConnect({ compact, guide = true }: { compact?: boolean; guide?: boolean }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["github", "accounts"], queryFn: () => get<GithubAccount[]>("api/github/accounts") });
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [adding, setAdding] = useState(false);
  const refresh = () => { for (const k of ["github", "gh-repos", "gh-remote", "integrations"]) qc.invalidateQueries({ queryKey: [k] }); };
  const add = useMutation({
    mutationFn: () => post<GithubAccount>("api/github/accounts", { name: name || null, token }),
    onSuccess: (a) => { setToken(""); setName(""); setAdding(false); refresh(); toast.success(t("gh.added", { login: a.login ?? "?" })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const check = useMutation({
    mutationFn: (a: GithubAccount) => post<GithubAccount>(`api/github/accounts/${a.id}/check`, {}),
    onSuccess: (a) => { refresh(); if (a.status === "ok") toast.success(t("gh.check_ok")); else toast.error(a.last_error ?? t("gh.check_bad")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: (a: GithubAccount) => del(`api/github/accounts/${a.id}`),
    onSuccess: () => { refresh(); toast.success(t("gh.removed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (accounts.isLoading) return <Spinner />;
  const list = accounts.data ?? [];
  const attention = list.some((a) => a.status === "error" || a.expiring);
  if (compact && list.length && !attention) return null;
  const showForm = adding || !list.length;
  return (
    <Card>
      <CardHeader icon={<GitBranch />} title={t("gh.title")} description={list.length ? t("gh.desc", { count: list.length }) : t("dep.gh_hint")}
        action={<div className="flex items-center gap-3">
          {guide && <Link to="/integrations?setup=github" className="text-sm font-semibold text-accent-text hover:underline">{t("intg.github.how")}</Link>}
          {list.length > 0 && !adding && <Button size="sm" onClick={() => setAdding(true)}><Plus /> {t("gh.add")}</Button>}
        </div>} />
      {list.length > 0 && (
        <CardBody className="space-y-0 py-1">
          {list.map((a) => (
            <div key={a.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2.5 last:border-0">
              <span className={cn("size-2 shrink-0 rounded-full", a.status === "error" ? "bg-offline" : a.expiring ? "bg-warning" : "bg-online")} />
              <div className="min-w-0 flex-1">
                <div className="font-semibold">{a.name}{a.login && a.login !== a.name && <span className="ml-1.5 font-normal text-muted">@{a.login}</span>}</div>
                <div className="text-xs text-muted">
                  {t(`gh.kind_${a.token_kind}`)}
                  {a.expires_at ? ` · ${a.expires_in_days !== null && a.expires_in_days < 0 ? t("gh.expired") : t("gh.expires", { date: formatDate(a.expires_at, i18n.language) })}` : ` · ${t("gh.no_expiry")}`}
                  {a.checked_at ? ` · ${t("gh.checked", { ago: timeAgo(a.checked_at, i18n.language) })}` : ""}
                </div>
                {a.status === "error" && <div className="text-xs text-offline">{a.last_error}</div>}
                {a.status === "ok" && a.expiring && <div className="text-xs text-warning">{t("gh.expiring", { count: Math.max(0, a.expires_in_days ?? 0) })}</div>}
              </div>
              <Button size="sm" variant="ghost" loading={check.isPending && check.variables?.id === a.id} onClick={() => check.mutate(a)}>{t("gh.check")}</Button>
              <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => remove.mutate(a)}><Trash2 /></Button>
            </div>
          ))}
        </CardBody>
      )}
      {showForm && (
        <CardBody className="flex flex-wrap items-end gap-3 border-t border-line">
          <Field label={t("gh.name")} hint={t("gh.name_hint")} className="w-56">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("gh.name_ph")} />
          </Field>
          <Field label={t("dep.gh_token")} hint={t("dep.gh_token_hint")} className="min-w-72 flex-1">
            <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" placeholder="github_pat_… / ghp_…" />
          </Field>
          {list.length > 0 && <Button onClick={() => setAdding(false)}>{t("common.cancel")}</Button>}
          <Button variant="primary" disabled={!token} loading={add.isPending} onClick={() => add.mutate()}>{t("gh.connect")}</Button>
        </CardBody>
      )}
    </Card>
  );
}

export function RunStatus({ status, rolledBack }: { status: string | null; rolledBack?: boolean }) {
  const { t } = useTranslation();
  if (!status) return <Badge>{t("dep.never")}</Badge>;
  if (status === "success") return <Badge tone="online"><CheckCircle2 className="size-3.5" /> {t("common.success")}</Badge>;
  if (status === "running") return <Badge tone="warning" dot>{t("common.running")}</Badge>;
  return <Badge tone="offline"><XCircle className="size-3.5" /> {t("common.failed")}{rolledBack ? ` · ${t("dep.rolled_back")}` : ""}</Badge>;
}

/* ------------------------------------------------------------------ list */
export function DeploymentsPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["deployments"], queryFn: () => get<Deployment[]>("api/deployments"), refetchInterval: 30_000 });
  const gh = useGithub();
  const [edit, setEdit] = useState<Deployment | "new" | null>(null);
  const runNow = useMutation({
    mutationFn: (d: Deployment) => post<DeploymentRun>(`api/deployments/${d.id}/run`, {}),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["deployments"] }); toast.success(t("dep.started")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div>
      <PageHeader title={t("dep.title")} description={t("dep.desc")}
        actions={admin && <Button variant="primary" onClick={() => setEdit("new")} disabled={!gh.data?.configured}><Plus /> {t("dep.new")}</Button>} />
      <div className="space-y-6">
        {admin && <GithubConnect compact />}
        <Card>
          {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : !q.data?.length ? (
            <EmptyState icon={<Rocket />} title={t("dep.empty")}>{t("dep.empty_hint")}</EmptyState>
          ) : (
            <Table>
              <thead><tr><Th>{t("dep.name")}</Th><Th>{t("dep.repo")}</Th><Th>{t("dep.target")}</Th><Th>{t("dep.auto")}</Th><Th>{t("dep.last_run")}</Th><Th /></tr></thead>
              <tbody>
                {q.data.map((d) => (
                  <Tr key={d.id}>
                    <Td><Link to={`/deployments/${d.id}`} className="font-semibold hover:underline">{d.name}</Link></Td>
                    <Td>
                      <div className="mono text-sm">{d.repo}</div>
                      <div className="flex items-center gap-1.5 text-xs text-muted"><GitBranch className="size-3" /> {d.branch} <Badge tone={d.channel === "prod" ? "accent" : "info"}>{d.channel === "prod" ? t("dep.prod") : t("dep.test")}</Badge></div>
                    </Td>
                    <Td className="text-sm">{d.target === "server" ? <TargetName dep={d} /> : `CT ${d.vmid}`}<div className="mono text-xs text-muted">{d.path}</div></Td>
                    <Td className="text-sm">{t(`dep.auto_${d.auto_deploy}`)}</Td>
                    <Td><RunStatus status={d.last_status} /><div className="text-xs text-muted">{d.last_run_at ? timeAgo(d.last_run_at, i18n.language) : ""}{d.last_sha ? ` · ${d.last_sha.slice(0, 7)}` : ""}</div></Td>
                    <Td className="whitespace-nowrap text-right">
                      {admin && <>
                        <Button size="sm" onClick={() => runNow.mutate(d)} disabled={d.last_status === "running"}><Play /> {t("dep.deploy_now")}</Button>
                        <Button variant="ghost" size="icon" aria-label={t("common.edit")} onClick={() => setEdit(d)}><Pencil /></Button>
                      </>}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
      {edit && <DeploymentDialog dep={edit === "new" ? null : edit} onClose={() => setEdit(null)} />}
    </div>
  );
}

function DeploymentDialog({ dep, onClose }: { dep: Deployment | null; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [f, setF] = useState<DeployForm>(() => deployDefaults(dep));
  const set = (p: Partial<DeployForm>) => setF((o) => ({ ...o, ...p }));
  const cts = useQuery({ queryKey: ["guests", "lxc-pick"], queryFn: () => get<Guest[]>("api/guests"), staleTime: 30_000 });
  const lxcs = (cts.data ?? []).filter((g) => g.type === "lxc").sort((a, b) => a.vmid - b.vmid);
  const srv = useQuery({ queryKey: ["nodes", "all"], queryFn: () => get<NodeInfo[]>("api/nodes"), staleTime: 30_000 });
  const servers = (srv.data ?? []).filter((n) => n.kind === "linux" && n.role !== "workstation").sort((a, b) => a.name.localeCompare(b.name));
  const ready = f.target === "server" ? !!f.node_id : !!(f.node_id && f.vmid);
  const save = useMutation({
    mutationFn: () => (dep ? put<Deployment>(`api/deployments/${dep.id}`, deployBody(f)) : post<Deployment>("api/deployments", deployBody(f))),
    onSuccess: (d) => { qc.invalidateQueries({ queryKey: ["deployments"] }); qc.invalidateQueries({ queryKey: ["deployment", d.id] }); toast.success(t("dep.saved")); onClose(); if (!dep) nav(`/deployments/${d.id}`); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} xl title={dep ? t("dep.edit") : t("dep.new")} description={t("dep.dialog_hint")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!f.name || !f.repo || !ready} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("dep.target_kind")}>
          <Segmented value={f.target} onChange={(v) => set({ target: v as DeployForm["target"], node_id: "", vmid: 0 })}
            options={[{ value: "container", label: t("dep.target_container") }, { value: "server", label: t("dep.target_server") }]} />
        </Field>
        {f.target === "container" ? (
          <Field label={t("dep.container")} hint={t("dep.container_hint")}>
            <Select value={f.node_id && f.vmid ? `${f.node_id}|${f.vmid}` : ""} onChange={(e) => { const [n, v] = e.target.value.split("|"); set({ node_id: n ?? "", vmid: Number(v) || 0 }); }}>
              <option value="">…</option>
              {lxcs.map((g) => <option key={`${g.node_id}-${g.vmid}`} value={`${g.node_id}|${g.vmid}`}>{g.vmid} · {g.name} ({g.node}){g.environment ? ` · ${g.environment}` : ""}{g.status !== "running" ? ` · ${g.status}` : ""}</option>)}
            </Select>
          </Field>
        ) : (
          <Field label={t("dep.server")} hint={t("dep.server_hint")}>
            <Select value={f.node_id} onChange={(e) => set({ node_id: e.target.value, vmid: 0, snapshot: false, rollback: false })}>
              <option value="">…</option>
              {servers.map((n) => <option key={n.id} value={n.id}>{n.name}{n.environment_name ? ` · ${n.environment_name}` : ""}</option>)}
            </Select>
          </Field>
        )}
        <Alert tone="info">{t("dep.rights_hint")} <Link to="/repos" className="font-semibold underline">{t("nav.repos")}</Link></Alert>
        <DeployFields f={f} set={set} editing={!!dep} />
      </div>
    </Dialog>
  );
}

function TargetName({ dep }: { dep: Deployment }) {
  const n = useQuery({ queryKey: ["nodes", "all"], queryFn: () => get<NodeInfo[]>("api/nodes"), staleTime: 60_000 });
  const name = n.data?.find((x) => x.id === dep.node_id)?.name ?? "server";
  return dep.node_id ? <Link to={`/servers/${dep.node_id}`} className="hover:underline">{name}</Link> : <>{name}</>;
}

/* ------------------------------------------------------------------ detail */
export function DeploymentDetailPage() {
  const { t, i18n } = useTranslation();
  const { id } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const q = useQuery({ queryKey: ["deployment", id], queryFn: () => get<Deployment>(`api/deployments/${id}`), refetchInterval: (s) => (s.state.data?.last_status === "running" ? 3000 : 30_000) });
  const hook = useQuery({ queryKey: ["deployment-hook", id], queryFn: () => get<{ url: string; secret: string; content_type: string; events: string[] }>(`api/deployments/${id}/webhook`), enabled: admin && q.data?.auto_deploy === "webhook" });
  const [edit, setEdit] = useState(false);
  const [runSel, setRunSel] = useState<string | null>(null);
  const [setup, setSetup] = useState(false);
  const d = q.data;
  useEffect(() => { if (!runSel && d?.runs?.[0]?.job_id) setRunSel(d.runs[0].id); }, [d, runSel]);
  const run = useMutation({
    mutationFn: () => post<DeploymentRun>(`api/deployments/${id}/run`, { run_setup: setup }),
    onSuccess: (r) => { setRunSel(r.id); qc.invalidateQueries({ queryKey: ["deployment", id] }); toast.success(t("dep.started")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/deployments/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["deployments"] }); nav("/deployments"); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading) return <Spinner />;
  if (q.isError || !d) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const sel = d.runs?.find((r) => r.id === runSel);
  return (
    <div>
      <PageHeader title={d.name} description={<span className="mono">{d.repo} · {d.branch}</span>}
        actions={<>
          <Link to="/deployments"><Button><ArrowLeft /> {t("dep.all")}</Button></Link>
          {admin && <Button onClick={() => setEdit(true)}><Pencil /> {t("common.edit")}</Button>}
          {admin && <Button variant="primary" onClick={() => run.mutate()} loading={run.isPending} disabled={d.last_status === "running"}><Rocket /> {t("dep.deploy_now")}</Button>}
        </>} />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="space-y-6">
          <Card>
            <CardHeader title={t("dep.overview")} action={<RunStatus status={d.last_status} />} />
            <CardBody className="space-y-2 text-sm">
              <Row k={t("dep.channel")} v={<Badge tone={d.channel === "prod" ? "accent" : "info"}>{d.channel === "prod" ? t("dep.prod") : t("dep.test")}</Badge>} />
              <Row k={t("dep.branch_prod")} v={<span className="mono">{d.branch_prod}</span>} />
              <Row k={t("dep.branch_test")} v={<span className="mono">{d.branch_test}</span>} />
              <Row k={t("dep.target")} v={d.target === "server" ? <TargetName dep={d} /> : d.node_id ? <Link className="hover:underline" to={`/proxmox/guests/${d.node_id}/${d.vmid}`}>CT {d.vmid}</Link> : `CT ${d.vmid}`} />
              <Row k={t("dep.path")} v={<span className="mono">{d.path}</span>} />
              <Row k={t("dep.preset")} v={t(`dep.preset_${d.preset}`)} />
              {d.service && <Row k={t("dep.service")} v={<span className="mono">{d.service}</span>} />}
              <Row k={t("dep.env")} v={d.has_env ? t("dep.env_set") : "-"} />
              <Row k={t("dep.auto")} v={t(`dep.auto_${d.auto_deploy}`)} />
              <Row k={t("dep.safety")} v={[d.snapshot && t("dep.snapshot"), d.rollback && t("dep.rollback")].filter(Boolean).join(" · ") || "-"} />
              {d.last_sha && <Row k={t("dep.current_commit")} v={<span className="mono">{d.last_sha.slice(0, 10)}</span>} />}
            </CardBody>
          </Card>
          {admin && (
            <Card>
              <CardHeader title={t("dep.run_options")} />
              <CardBody className="space-y-3">
                <Switch checked={setup} onCheckedChange={setSetup} label={t("dep.run_setup")} description={t("dep.run_setup_hint")} />
                <Button variant="danger" size="sm" onClick={() => remove.mutate()} loading={remove.isPending}><Trash2 /> {t("dep.delete")}</Button>
                <p className="text-xs text-muted">{t("dep.delete_hint")}</p>
              </CardBody>
            </Card>
          )}
          {hook.data && (
            <Card>
              <CardHeader icon={<Webhook />} title={t("dep.webhook")} description={t("dep.webhook_hint")} />
              <CardBody className="space-y-3 text-sm">
                <Field label="Payload URL"><div className="flex gap-2"><Input readOnly value={hook.data.url} className="mono text-[12px]" /><CopyButton value={hook.data.url} /></div></Field>
                <Field label="Secret"><div className="flex gap-2"><Input readOnly type="password" value={hook.data.secret} className="mono text-[12px]" /><CopyButton value={hook.data.secret} /></div></Field>
                <p className="text-xs text-muted">{t("dep.webhook_steps")}</p>
              </CardBody>
            </Card>
          )}
        </div>
        <div className="space-y-6">
          <Card>
            <CardHeader title={t("dep.runs")} />
            {!d.runs?.length ? <EmptyState title={t("dep.no_runs")} /> : (
              <Table>
                <thead><tr><Th>{t("dep.started_at")}</Th><Th>{t("dep.trigger")}</Th><Th>Commit</Th><Th>{t("dep.result")}</Th></tr></thead>
                <tbody>
                  {d.runs.map((r) => (
                    <Tr key={r.id} className={cn("cursor-pointer", r.id === runSel && "bg-sunken")} onClick={() => setRunSel(r.id)}>
                      <Td className="text-sm">{formatDate(r.started_at, i18n.language)}<div className="text-xs text-muted">{r.requested_by ?? ""}</div></Td>
                      <Td className="text-sm">{t(`dep.trigger_${r.trigger}`, { defaultValue: r.trigger })}<div className="mono text-xs text-muted">{r.branch}</div></Td>
                      <Td className="mono text-xs">{r.sha?.slice(0, 7) ?? "-"}{r.previous_sha && r.previous_sha !== r.sha ? <span className="text-muted"> ← {r.previous_sha.slice(0, 7)}</span> : ""}</Td>
                      <Td><RunStatus status={r.status} rolledBack={r.rolled_back} />{r.rolled_back && <Undo2 className="ml-1 inline size-3.5 text-muted" />}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
          {sel && (
            <Card>
              <CardHeader title={t("dep.run_log")} description={sel.error ?? undefined} />
              <CardBody>{sel.job_id ? <JobView key={sel.job_id} jobId={sel.job_id} onDone={() => qc.invalidateQueries({ queryKey: ["deployment", id] })} /> : <p className="text-sm text-muted">{sel.error ?? "-"}</p>}</CardBody>
            </Card>
          )}
        </div>
      </div>
      {edit && <DeploymentDialog dep={d} onClose={() => setEdit(false)} />}
    </div>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="flex justify-between gap-4 border-b border-line pb-2 last:border-0"><span className="text-muted">{k}</span><span className="text-right">{v}</span></div>;
}
