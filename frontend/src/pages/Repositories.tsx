import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Download, ExternalLink, FolderGit2, Lock, Pencil, Plus, Rocket, Search, ShieldCheck, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { useCustomers } from "@/components/customers";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { CustomerSummary, GithubAccount, GithubRepo, LibraryRepo, RepoAccess, RepoKind } from "@/lib/types";
import { cn } from "@/lib/utils";
import { GithubConnect } from "./Deployments";

const KIND_TONE: Record<RepoKind, "accent" | "info" | "neutral"> = { product: "accent", customer: "info", tooling: "neutral" };
type AccessRow = { customer_id: string; can_deploy: boolean; ai_read: boolean };

const useLibrary = () => useQuery({ queryKey: ["repos"], queryFn: () => get<LibraryRepo[]>("api/repos"), refetchInterval: 60_000 });

function useRefresh() {
  const qc = useQueryClient();
  return () => { for (const k of ["repos", "gh-remote", "customer"]) qc.invalidateQueries({ queryKey: [k] }); };
}

export function KindBadge({ kind }: { kind: RepoKind }) {
  const { t } = useTranslation();
  return <Badge tone={KIND_TONE[kind]}>{t(`repo.kind_${kind}`)}</Badge>;
}

/* ------------------------------------------------------------------ page */
export function RepositoriesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const lib = useLibrary();
  const customers = useCustomers();
  const accounts = useQuery({ queryKey: ["github", "accounts"], queryFn: () => get<GithubAccount[]>("api/github/accounts"), enabled: admin });
  const [view, setView] = useState<"list" | "matrix">("list");
  const [kind, setKind] = useState<"all" | RepoKind>("all");
  const [q, setQ] = useState("");
  const [dialog, setDialog] = useState<null | "import" | "create" | { access: LibraryRepo } | { edit: LibraryRepo }>(null);
  const refresh = useRefresh();
  const remove = useMutation({
    mutationFn: (r: LibraryRepo) => del(`api/repos/${r.id}`),
    onSuccess: () => { refresh(); toast.success(t("repo.removed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rows = useMemo(() => (lib.data ?? []).filter((r) => (kind === "all" || r.kind === kind)
    && (!q || r.full_name.toLowerCase().includes(q.toLowerCase()) || r.description.toLowerCase().includes(q.toLowerCase()))), [lib.data, kind, q]);
  const connected = (accounts.data?.length ?? 0) > 0;
  return (
    <div>
      <PageHeader title={t("repo.title")} description={t("repo.desc")}
        actions={admin && <>
          <Button onClick={() => setDialog("import")} disabled={!connected}><Download /> {t("repo.import")}</Button>
          <Button variant="primary" onClick={() => setDialog("create")} disabled={!connected}><Plus /> {t("repo.create")}</Button>
        </>} />
      <div className="space-y-5">
        {admin && <GithubConnect compact />}
        <div className="grid gap-3 sm:grid-cols-3">
          {(["product", "customer", "tooling"] as RepoKind[]).map((k) => (
            <div key={k} className="rounded-lg border border-line bg-raised p-3.5 shadow-card">
              <div className="flex items-center gap-2"><KindBadge kind={k} /><span className="ml-auto text-lg font-bold tabular-nums">{(lib.data ?? []).filter((r) => r.kind === k).length}</span></div>
              <p className="mt-1.5 text-xs text-muted">{t(`repo.kind_${k}_hint`)}</p>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Segmented value={view} onChange={setView} options={[{ value: "list", label: t("repo.view_list") }, { value: "matrix", label: t("repo.view_matrix") }]} />
          <Segmented value={kind} onChange={setKind} options={[{ value: "all", label: t("repo.all") }, { value: "product", label: t("repo.kind_product") }, { value: "customer", label: t("repo.kind_customer") }, { value: "tooling", label: t("repo.kind_tooling") }]} />
          <div className="relative ml-auto w-full sm:w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("repo.search")} className="pl-8" />
          </div>
        </div>
        <Card>
          {lib.isLoading ? <CardBody><Spinner /></CardBody> : lib.isError ? <CardBody><Alert tone="offline">{errorMessage(lib.error, t)}</Alert></CardBody> : !rows.length ? (
            <EmptyState icon={<FolderGit2 />} title={lib.data?.length ? t("repo.none_match") : t("repo.empty")}>{!lib.data?.length && t("repo.empty_hint")}</EmptyState>
          ) : view === "list" ? (
            <Table>
              <thead><tr><Th>{t("repo.repository")}</Th><Th>{t("repo.kind")}</Th><Th>{t("repo.customers")}</Th><Th>{t("repo.deployments")}</Th><Th /></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <Tr key={r.id}>
                    <Td>
                      <div className="flex items-center gap-1.5">
                        <a href={r.html_url} target="_blank" rel="noreferrer" className="mono text-sm font-semibold hover:underline">{r.full_name}</a>
                        {r.private && <Lock className="size-3.5 text-muted" aria-label={t("repo.private")} />}
                        <ExternalLink className="size-3 text-muted" />
                      </div>
                      {r.description && <div className="max-w-md truncate text-xs text-muted">{r.description}</div>}
                    </Td>
                    <Td><KindBadge kind={r.kind} />{r.kind === "customer" && r.owner_customer && <div className="mt-1 text-xs text-muted">{r.owner_customer}</div>}</Td>
                    <Td>
                      <div className="flex max-w-sm flex-wrap gap-1">
                        {r.access.length ? r.access.map((a) => <AccessChip key={a.customer_id} a={a} />) : <span className="text-xs text-warning">{t("repo.no_customers")}</span>}
                      </div>
                    </Td>
                    <Td className="text-sm">{r.deployments ? <Link to="/deployments" className="inline-flex items-center gap-1 hover:underline"><Rocket className="size-3.5" /> {r.deployments}</Link> : <span className="text-muted">–</span>}</Td>
                    <Td className="whitespace-nowrap text-right">
                      {admin && <>
                        <Button size="sm" onClick={() => setDialog({ access: r })}><ShieldCheck /> {t("repo.rights")}</Button>
                        <Button size="icon" variant="ghost" aria-label={t("common.edit")} onClick={() => setDialog({ edit: r })}><Pencil /></Button>
                        <Button size="icon" variant="ghost" aria-label={t("repo.remove")} onClick={() => { if (window.confirm(t("repo.remove_confirm", { name: r.full_name }))) remove.mutate(r); }}><Trash2 /></Button>
                      </>}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : <Matrix repos={rows} customers={customers.data ?? []} admin={admin} />}
        </Card>
        <p className="text-xs text-muted">{t("repo.footnote")}</p>
      </div>
      {dialog === "import" && <ImportDialog accounts={accounts.data ?? []} onClose={() => setDialog(null)} />}
      {dialog === "create" && <CreateDialog accounts={accounts.data ?? []} onClose={() => setDialog(null)} />}
      {dialog && typeof dialog === "object" && "access" in dialog && <AccessDialog repo={dialog.access} onClose={() => setDialog(null)} />}
      {dialog && typeof dialog === "object" && "edit" in dialog && <EditDialog repo={dialog.edit} onClose={() => setDialog(null)} />}
    </div>
  );
}

function AccessChip({ a }: { a: RepoAccess }) {
  const { t } = useTranslation();
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs", a.can_deploy ? "border-line bg-sunken" : "border-dashed border-line text-muted")}
      title={[a.can_deploy ? t("repo.can_deploy") : t("repo.no_deploy"), a.ai_read && t("repo.ai_read")].filter(Boolean).join(" · ")}>
      {a.customer ?? a.customer_id}
      {a.can_deploy && <Rocket className="size-3 text-online" />}
      {a.ai_read && <Bot className="size-3 text-accent-text" />}
    </span>
  );
}

/* ------------------------------------------------------------------ matrix */
function Matrix({ repos, customers, admin }: { repos: LibraryRepo[]; customers: CustomerSummary[]; admin: boolean }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const save = useMutation({
    mutationFn: ({ r, access }: { r: LibraryRepo; access: AccessRow[] }) => put<LibraryRepo>(`api/repos/${r.id}/access`, { customers: access }),
    onSuccess: refresh,
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  // One click cycles: nothing → may deploy → may deploy + AI reads → nothing.
  const cycle = (r: LibraryRepo, cid: string) => {
    const cur = r.access.find((a) => a.customer_id === cid);
    const rest: AccessRow[] = r.access.filter((a) => a.customer_id !== cid).map(({ customer_id, can_deploy, ai_read }) => ({ customer_id, can_deploy, ai_read }));
    const next: AccessRow | null = !cur ? { customer_id: cid, can_deploy: true, ai_read: false }
      : cur.can_deploy && !cur.ai_read ? { customer_id: cid, can_deploy: true, ai_read: true } : null;
    save.mutate({ r, access: next ? [...rest, next] : rest });
  };
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-line">
            <th className="sticky left-0 bg-raised px-4 py-2.5 text-left text-[11px] font-bold uppercase tracking-[0.07em] text-muted">{t("repo.repository")}</th>
            {customers.map((c) => <th key={c.id} className="min-w-24 px-2 py-2.5 text-center text-xs font-semibold">{c.name}</th>)}
          </tr>
        </thead>
        <tbody>
          {repos.map((r) => (
            <tr key={r.id} className="border-b border-line last:border-0">
              <td className="sticky left-0 bg-raised px-4 py-2"><div className="mono font-semibold">{r.full_name}</div><KindBadge kind={r.kind} /></td>
              {customers.map((c) => {
                const a = r.access.find((x) => x.customer_id === c.id);
                const locked = r.kind === "customer" && r.owner_customer_id !== c.id;
                const owner = r.kind === "customer" && r.owner_customer_id === c.id;
                return (
                  <td key={c.id} className="px-2 py-2 text-center">
                    <button type="button" disabled={!admin || locked || owner || save.isPending} onClick={() => cycle(r, c.id)}
                      title={locked ? t("repo.locked") : owner ? t("repo.owner") : t("repo.cycle")}
                      className={cn("inline-flex h-8 min-w-14 items-center justify-center gap-1 rounded-md border px-2 transition-colors",
                        locked ? "cursor-not-allowed border-transparent text-line" : a ? "border-online/40 bg-online-soft text-online" : "border-dashed border-line text-muted hover:border-ink/30",
                        admin && !locked && !owner && "cursor-pointer")}>
                      {locked ? "·" : a ? <>{owner ? <span className="text-[10px] font-black uppercase">{t("repo.own")}</span> : a.can_deploy ? <Rocket className="size-3.5" /> : !a.ai_read ? <span className="text-[10px] font-bold uppercase">{t("repo.linked")}</span> : null}{a.ai_read && <Bot className="size-3.5 text-accent-text" />}</> : "–"}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex flex-wrap gap-4 border-t border-line px-4 py-2.5 text-xs text-muted">
        <span className="inline-flex items-center gap-1"><Rocket className="size-3.5 text-online" /> {t("repo.can_deploy")}</span>
        <span className="inline-flex items-center gap-1"><Bot className="size-3.5 text-accent-text" /> {t("repo.ai_read")}</span>
        <span>{t("repo.matrix_hint")}</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ shared: customers with rights */
function CustomerRights({ value, onChange, only }: { value: AccessRow[]; onChange: (v: AccessRow[]) => void; only?: string | null }) {
  const { t } = useTranslation();
  const customers = (useCustomers().data ?? []).filter((c) => !only || c.id === only);
  const get_ = (id: string) => value.find((a) => a.customer_id === id);
  const upd = (id: string, p: Partial<AccessRow> | null) => {
    const rest = value.filter((a) => a.customer_id !== id);
    onChange(p === null ? rest : [...rest, { customer_id: id, can_deploy: true, ai_read: false, ...get_(id), ...p }]);
  };
  return (
    <div className="max-h-72 divide-y divide-line overflow-y-auto rounded-md border border-line">
      {customers.map((c) => {
        const a = get_(c.id);
        return (
          <div key={c.id} className="flex flex-wrap items-center gap-x-5 gap-y-2 px-3 py-2">
            <Checkbox checked={!!a} disabled={!!only} onCheckedChange={(v) => upd(c.id, v ? {} : null)} label={<span className="font-semibold">{c.name}</span>} />
            {a && <div className="ml-auto flex gap-4">
              <Switch checked={a.can_deploy} onCheckedChange={(v) => upd(c.id, { can_deploy: v })} label={t("repo.can_deploy")} />
              <Switch checked={a.ai_read} onCheckedChange={(v) => upd(c.id, { ai_read: v })} label={t("repo.ai_read")} />
            </div>}
          </div>
        );
      })}
    </div>
  );
}

function KindFields({ kind, owner, set }: { kind: RepoKind; owner: string | null; set: (p: { kind?: RepoKind; owner_customer_id?: string | null }) => void }) {
  const { t } = useTranslation();
  const customers = useCustomers().data ?? [];
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label={t("repo.kind")} hint={t(`repo.kind_${kind}_hint`)}>
        <Select value={kind} onChange={(e) => set({ kind: e.target.value as RepoKind, owner_customer_id: e.target.value === "customer" ? owner : null })}>
          {(["product", "customer", "tooling"] as RepoKind[]).map((k) => <option key={k} value={k}>{t(`repo.kind_${k}`)}</option>)}
        </Select>
      </Field>
      {kind === "customer" && (
        <Field label={t("repo.owner_customer")}>
          <Select value={owner ?? ""} onChange={(e) => set({ owner_customer_id: e.target.value || null })}>
            <option value="">…</option>
            {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </Field>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ dialogs */
function AccessDialog({ repo, onClose }: { repo: LibraryRepo; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const [v, setV] = useState<AccessRow[]>(repo.access.map(({ customer_id, can_deploy, ai_read }) => ({ customer_id, can_deploy, ai_read })));
  const save = useMutation({
    mutationFn: () => put<LibraryRepo>(`api/repos/${repo.id}/access`, { customers: v }),
    onSuccess: () => { refresh(); toast.success(t("repo.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={t("repo.rights_title", { name: repo.full_name })} description={repo.kind === "customer" ? t("repo.rights_customer") : t("repo.rights_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <CustomerRights value={v} onChange={setV} only={repo.kind === "customer" ? repo.owner_customer_id : null} />
    </Dialog>
  );
}

function EditDialog({ repo, onClose }: { repo: LibraryRepo; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const [f, setF] = useState({ kind: repo.kind, owner_customer_id: repo.owner_customer_id, notes: repo.notes });
  const save = useMutation({
    mutationFn: () => put<LibraryRepo>(`api/repos/${repo.id}`, f),
    onSuccess: () => { refresh(); toast.success(t("repo.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const narrowing = f.kind === "customer" && repo.kind !== "customer" && repo.access.some((a) => a.customer_id !== f.owner_customer_id);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={repo.full_name}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={f.kind === "customer" && !f.owner_customer_id} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <KindFields kind={f.kind} owner={f.owner_customer_id} set={(p) => setF((o) => ({ ...o, ...p }))} />
        {narrowing && <Alert tone="warning">{t("repo.narrowing")}</Alert>}
        <Field label={t("repo.notes")} hint={t("repo.notes_hint")}><Textarea rows={3} value={f.notes} onChange={(e) => setF((o) => ({ ...o, notes: e.target.value }))} /></Field>
      </div>
    </Dialog>
  );
}

function ImportDialog({ accounts, onClose }: { accounts: GithubAccount[]; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const [q, setQ] = useState("");
  const [pick, setPick] = useState<string[]>([]);
  const [f, setF] = useState<{ kind: RepoKind; owner_customer_id: string | null }>({ kind: "product", owner_customer_id: null });
  const [access, setAccess] = useState<AccessRow[]>([]);
  const remote = useQuery({ queryKey: ["gh-remote"], queryFn: () => get<GithubRepo[]>("api/github/remote"), staleTime: 60_000 });
  const list = (remote.data ?? []).filter((r) => !r.archived && (!q || r.full_name.toLowerCase().includes(q.toLowerCase())));
  const save = useMutation({
    mutationFn: () => post<LibraryRepo[]>("api/repos/import", { full_names: pick, ...f, customers: f.kind === "customer" ? [] : access }),
    onSuccess: (r) => { refresh(); toast.success(t("repo.imported", { count: r.length })); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const toggle = (fn: string) => setPick((p) => (p.includes(fn) ? p.filter((x) => x !== fn) : [...p, fn]));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={t("repo.import_title")} description={t("repo.import_desc", { count: accounts.length })}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!pick.length || (f.kind === "customer" && !f.owner_customer_id)} onClick={() => save.mutate()}>{t("repo.import_n", { count: pick.length })}</Button></>}>
      <div className="space-y-4">
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("repo.search")} />
        <div className="max-h-64 divide-y divide-line overflow-y-auto rounded-md border border-line">
          {remote.isLoading ? <div className="p-4"><Spinner /></div> : remote.isError ? <Alert tone="offline" className="m-3">{errorMessage(remote.error, t)}</Alert> : list.map((r) => (
            <div key={r.full_name} className="flex items-center gap-3 px-3 py-2">
              <Checkbox checked={pick.includes(r.full_name) || !!r.in_library} disabled={!!r.in_library} onCheckedChange={() => toggle(r.full_name)}
                label={<span className="mono text-sm">{r.full_name}{r.private && <Lock className="ml-1 inline size-3 text-muted" />}</span>}
                description={r.in_library ? t("repo.in_library") : r.description || undefined} />
            </div>
          ))}
        </div>
        <KindFields kind={f.kind} owner={f.owner_customer_id} set={(p) => setF((o) => ({ ...o, ...p }))} />
        {f.kind !== "customer" && <Field label={t("repo.link_to")}><CustomerRights value={access} onChange={setAccess} /></Field>}
      </div>
    </Dialog>
  );
}

const GITIGNORE = ["", "Python", "Node", "Go", "Java", "VisualStudio"];

function CreateDialog({ accounts, onClose }: { accounts: GithubAccount[]; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const [f, setF] = useState({
    account_id: accounts[0]?.id ?? "", owner: "", name: "", description: "", private: true, gitignore: "", test_branch: true,
    kind: "product" as RepoKind, owner_customer_id: null as string | null,
  });
  const [access, setAccess] = useState<AccessRow[]>([]);
  const set = (p: Partial<typeof f>) => setF((o) => ({ ...o, ...p }));
  const owners = useQuery({ queryKey: ["gh-owners", f.account_id], queryFn: () => get<{ login: string; type: string }[]>(`api/github/accounts/${f.account_id}/owners`), enabled: !!f.account_id });
  const save = useMutation({
    mutationFn: () => post<LibraryRepo>("api/repos/create", { ...f, owner: f.owner || null, gitignore: f.gitignore || null, customers: f.kind === "customer" ? [] : access }),
    onSuccess: (r) => { refresh(); toast.success(t("repo.created", { name: r.full_name })); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const valid = /^[A-Za-z0-9_.-]{1,100}$/.test(f.name) && (f.kind !== "customer" || !!f.owner_customer_id);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={t("repo.create_title")} description={t("repo.create_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}><Plus /> {t("repo.create")}</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-3">
          {accounts.length > 1 && (
            <Field label={t("repo.account")}>
              <Select value={f.account_id} onChange={(e) => set({ account_id: e.target.value, owner: "" })}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </Select>
            </Field>
          )}
          <Field label={t("repo.owner_gh")} hint={t("repo.owner_gh_hint")}>
            <Select value={f.owner} onChange={(e) => set({ owner: e.target.value })}>
              {(owners.data ?? []).map((o) => <option key={o.login} value={o.type === "user" ? "" : o.login}>{o.login}{o.type === "org" ? ` (${t("repo.org")})` : ""}</option>)}
            </Select>
          </Field>
          <Field label={t("repo.name")} hint={t("repo.name_hint")}>
            <Input value={f.name} onChange={(e) => set({ name: e.target.value.replace(/\s+/g, "-") })} placeholder="klant-portaal" />
          </Field>
        </div>
        <Field label={t("repo.description")}><Input value={f.description} onChange={(e) => set({ description: e.target.value })} maxLength={350} /></Field>
        <div className="flex flex-wrap items-end gap-6">
          <Switch checked={f.private} onCheckedChange={(v) => set({ private: v })} label={t("repo.private")} />
          <Checkbox checked={f.test_branch} onCheckedChange={(v) => set({ test_branch: v })} label={t("repo.test_branch")} description={t("repo.test_branch_hint")} />
          <Field label={t("repo.gitignore")} className="w-44">
            <Select value={f.gitignore} onChange={(e) => set({ gitignore: e.target.value })}>
              {GITIGNORE.map((g) => <option key={g} value={g}>{g || t("repo.gitignore_none")}</option>)}
            </Select>
          </Field>
        </div>
        <KindFields kind={f.kind} owner={f.owner_customer_id} set={set} />
        {f.kind !== "customer" && <Field label={t("repo.link_to")}><CustomerRights value={access} onChange={setAccess} /></Field>}
      </div>
    </Dialog>
  );
}
