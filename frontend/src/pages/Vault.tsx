import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Bot, Check, Copy, Eye, EyeOff, History, KeyRound, Lock, LockOpen, Pencil, Plus, RefreshCw, Search,
  ShieldCheck, Trash2, LockKeyhole as VaultIcon, Wand2, X,
} from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError, del, errorMessage, get, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";
import { MachinePicker } from "./Docs";

/* ------------------------------------------------------------------ types */
const KINDS = ["login", "server", "network", "database", "api_key", "wifi", "certificate", "license", "note", "other"] as const;
type Kind = (typeof KINDS)[number];
interface Extra { name: string; value: string | null; secret: boolean }
interface Item {
  id: string; customer_id: string; customer_name: string | null; name: string; kind: Kind; url: string | null; tags: string[];
  machines: { id: string; name: string | null }[]; ai_access: "approval" | "never"; rotate_days: number | null; rotation_due: boolean;
  created_at: string; updated_at: string; secret_changed_at: string; last_used_at: string | null; created_by: string | null;
  updated_by: string | null; username: string; has_password: boolean; has_notes: boolean; fields: Extra[];
  password?: string; notes?: string; history?: { id: string; created_at: string; created_by: string | null }[]; error?: string;
}
interface Status {
  unlocked: boolean; unlocked_until: string | null; require_unlock: boolean; can_reveal: boolean; unencrypted_backup_targets: number;
  grants: number; key: { active: string; keys: number; rotated_at: string | null; created_at: string | null; algorithm: string } | null;
}
interface Access {
  id: string; at: string; item_id: string | null; item_name: string | null; customer_id: string | null; action: string;
  actor_kind: "user" | "ai" | "system"; username: string | null; token_name: string | null; approval_id: string | null;
  reason: string | null; ip: string | null; success: boolean; detail: Record<string, unknown>;
}
interface Grant { id: string; item_name: string | null; approved_by: string | null; expires_at: string; uses_left: number; approval_id: string | null }
interface VaultSettings { unlock_minutes: number; require_unlock: boolean; grant_minutes: number; grant_uses: number; history_keep: number; notify_ai_use: boolean }

function qs(path: string, params: Record<string, string | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) u.set(k, v);
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = value;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function useVaultStatus() {
  return useQuery({ queryKey: ["vault-status"], queryFn: () => get<Status>("api/vault/status"), refetchInterval: 30_000 });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => { for (const k of ["vault-items", "vault-item", "vault-access", "vault-status", "vault-grants", "documents"]) qc.invalidateQueries({ queryKey: [k] }); };
}

/** Run something that needs the unlocked vault; asks for the password first when it is locked. */
function useUnlockGuard() {
  const status = useVaultStatus();
  const [pending, setPending] = useState<(() => void) | null>(null);
  const run = (fn: () => void) => {
    if (status.data?.unlocked) fn();
    else setPending(() => fn);
  };
  const dialog = pending ? <UnlockDialog onClose={() => setPending(null)} onUnlocked={() => { const f = pending; setPending(null); f?.(); }} /> : null;
  return { run, dialog };
}

function UnlockDialog({ onClose, onUnlocked }: { onClose: () => void; onUnlocked: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [pw, setPw] = useState("");
  const unlock = useMutation({
    mutationFn: () => post("api/vault/unlock", { password: pw }),
    onSuccess: async () => { await qc.invalidateQueries({ queryKey: ["vault-status"] }); onUnlocked(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("vault.unlock_title")} description={t("vault.unlock_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={unlock.isPending} disabled={!pw} onClick={() => unlock.mutate()}><LockOpen /> {t("vault.unlock")}</Button></>}>
      <form onSubmit={(e) => { e.preventDefault(); if (pw) unlock.mutate(); }}>
        <Field label={t("vault.your_password")}>
          <Input type="password" autoFocus autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} />
        </Field>
      </form>
    </Dialog>
  );
}

function LockChip() {
  const { t, i18n } = useTranslation();
  const status = useVaultStatus();
  const qc = useQueryClient();
  const [unlocking, setUnlocking] = useState(false);
  const lock = useMutation({ mutationFn: () => post("api/vault/lock"), onSuccess: () => qc.invalidateQueries({ queryKey: ["vault-status"] }) });
  const s = status.data;
  if (!s?.can_reveal) return null;
  if (!s.require_unlock) return <Badge tone="warning">{t("vault.no_unlock_needed")}</Badge>;
  return (
    <>
      {s.unlocked ? (
        <Button size="sm" onClick={() => lock.mutate()} title={s.unlocked_until ? t("vault.unlocked_until", { time: formatDate(s.unlocked_until, i18n.language) }) : undefined}>
          <LockOpen className="text-online" /> {t("vault.lock")}
        </Button>
      ) : (
        <Button size="sm" onClick={() => setUnlocking(true)}><Lock /> {t("vault.unlock")}</Button>
      )}
      {unlocking && <UnlockDialog onClose={() => setUnlocking(false)} onUnlocked={() => setUnlocking(false)} />}
    </>
  );
}

function KindBadge({ k }: { k: Kind }) {
  const { t } = useTranslation();
  return <Badge tone="neutral">{t(`vault.kind.${k}`)}</Badge>;
}

function AiBadge({ a }: { a: Item["ai_access"] }) {
  const { t } = useTranslation();
  return a === "approval"
    ? <Badge tone="info"><Bot className="mr-1 inline size-3" />{t("vault.ai.approval_short")}</Badge>
    : <Badge tone="neutral"><X className="mr-1 inline size-3" />{t("vault.ai.never_short")}</Badge>;
}

/* ------------------------------------------------------------------ page */
export function VaultPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [tab, setTab] = useState("items");
  const status = useVaultStatus();
  const s = status.data;
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><VaultIcon className="hidden size-6 shrink-0 text-koraal sm:block" /><span>{t("vault.title")}</span></span>}
        description={t("vault.desc")}
        actions={<LockChip />} />
      {admin && !!s?.unencrypted_backup_targets && (
        <Alert tone="warning" className="mb-4" title={t("vault.backup_warn_title")}>
          {t("vault.backup_warn", { count: s.unencrypted_backup_targets })} <Link to="/admin/backups" className="underline">{t("vault.backup_warn_link")}</Link>
        </Alert>
      )}
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          <Tabs.Trigger value="items" className={tabCls}>{t("vault.tab_items")}</Tabs.Trigger>
          {admin && <Tabs.Trigger value="access" className={tabCls}>{t("vault.tab_access")}</Tabs.Trigger>}
          {admin && <Tabs.Trigger value="settings" className={tabCls}>{t("vault.tab_settings")}</Tabs.Trigger>}
        </Tabs.List>
        <Tabs.Content value="items"><ItemsTab admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="access"><AccessTab /></Tabs.Content>}
        {admin && <Tabs.Content value="settings"><SettingsTab /></Tabs.Content>}
      </Tabs.Root>
    </div>
  );
}

/* ------------------------------------------------------------------ items */
function ItemsTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const status = useVaultStatus();
  const invalidate = useInvalidate();
  const [filterCustomer, setFilterCustomer] = useState("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [editing, setEditing] = useState<Item | "new" | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Item | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const guard = useUnlockGuard();
  useEffect(() => { const h = setTimeout(() => setQuery(q), 250); return () => clearTimeout(h); }, [q]);
  const cid = customerId || filterCustomer;
  const list = useQuery({
    queryKey: ["vault-items", cid, query, kind],
    queryFn: () => get<{ items: Item[]; counts: { credentials: number; rotation_due: number; ai_never: number } }>(qs("api/vault/items", { customer_id: cid, q: query, kind })),
  });
  const copy = async (it: Item, field = "password") => {
    try {
      const r = await post<Item>(`api/vault/items/${it.id}/reveal`, { action: "copy", field });
      await copyText(field === "username" ? r.username : r.password ?? "");
      setCopied(it.id + field);
      toast.success(t("vault.copied"));
      setTimeout(() => setCopied(null), 1500);
      invalidate();
    } catch (e) {
      if (e instanceof ApiError && e.detail === "vault_locked") status.refetch();
      toast.error(errorMessage(e, t));
    }
  };
  const remove = useMutation({
    mutationFn: (id: string) => del(`api/vault/items/${id}`),
    onSuccess: () => { invalidate(); setDeleting(null); toast.success(t("vault.deleted")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const items = list.data?.items ?? [];
  const c = list.data?.counts;
  return (
    <div>
      <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("vault.k_items")} value={c?.credentials ?? "–"} hint={t("vault.k_items_hint")} tone="info" icon={<KeyRound />} />
        <Stat label={t("vault.k_rotate")} value={c?.rotation_due ?? "–"} hint={t("vault.k_rotate_hint")} tone={c?.rotation_due ? "warning" : "neutral"} icon={<RefreshCw />} />
        <Stat label={t("vault.k_ai_never")} value={c?.ai_never ?? "–"} hint={t("vault.k_ai_never_hint")} tone="neutral" icon={<ShieldCheck />} />
        <Stat label={t("vault.k_grants")} value={status.data?.grants ?? "–"} hint={t("vault.k_grants_hint")} tone={status.data?.grants ? "accent" : "neutral"} icon={<Bot />} />
      </div>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("vault.search")} className="pl-9" />
        </div>
        {!customerId && (
          <Select value={filterCustomer} onChange={(e) => setFilterCustomer(e.target.value)} className="sm:w-56">
            <option value="">{t("doc.all_customers")}</option>
            {customers.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </Select>
        )}
        <Select value={kind} onChange={(e) => setKind(e.target.value)} className="sm:w-44">
          <option value="">{t("vault.all_kinds")}</option>
          {KINDS.map((k) => <option key={k} value={k}>{t(`vault.kind.${k}`)}</option>)}
        </Select>
        {admin && <Button variant="primary" onClick={() => guard.run(() => setEditing("new"))}><Plus /> {t("vault.new")}</Button>}
      </div>
      {list.isLoading ? <Spinner /> : !items.length ? (
        <EmptyState icon={<KeyRound />} title={query || kind ? t("vault.none_found") : t("vault.empty")}>{!query && !kind && t("vault.empty_hint")}</EmptyState>
      ) : (
        <Card className="overflow-hidden">
          <Table>
            <thead>
              <Tr><Th>{t("common.name")}</Th>{!customerId && <Th className="hidden md:table-cell">{t("doc.customer")}</Th>}<Th>{t("vault.username")}</Th>
                <Th className="hidden lg:table-cell">{t("vault.ai_access")}</Th><Th className="hidden lg:table-cell">{t("vault.last_used")}</Th><Th className="text-right">{t("common.actions")}</Th></Tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <Tr key={it.id}>
                  <Td>
                    <button type="button" onClick={() => setViewing(it.id)} className="cursor-pointer text-left">
                      <div className="flex items-center gap-2 font-semibold hover:underline">{it.name}{it.rotation_due && <span title={t("vault.rotation_due")}><AlertTriangle className="size-4 text-warning" /></span>}</div>
                      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">{it.kind && <KindBadge k={it.kind} />}{it.url && <span className="max-w-56 truncate">{it.url}</span>}</div>
                    </button>
                  </Td>
                  {!customerId && <Td className="hidden md:table-cell text-sm">{it.customer_name}</Td>}
                  <Td className="mono text-[13px]">
                    {it.username ? (
                      <span className="inline-flex items-center gap-1">{it.username}
                        <button type="button" className="cursor-pointer rounded p-0.5 text-muted hover:text-ink" title={t("vault.copy_username")} onClick={() => copyText(it.username).then(() => toast.success(t("vault.copied")))}><Copy className="size-3.5" /></button>
                      </span>
                    ) : "–"}
                  </Td>
                  <Td className="hidden lg:table-cell"><AiBadge a={it.ai_access} /></Td>
                  <Td className="hidden lg:table-cell text-sm text-muted">{it.last_used_at ? timeAgo(it.last_used_at, i18n.language) : "–"}</Td>
                  <Td className="text-right">
                    <div className="inline-flex gap-1">
                      {admin && it.has_password && (
                        <Button size="sm" title={t("vault.copy_password")} onClick={() => guard.run(() => copy(it))}>
                          {copied === it.id + "password" ? <Check /> : <Copy />}<span className="hidden sm:inline">{t("vault.password")}</span>
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" title={t("vault.show")} onClick={() => setViewing(it.id)}><Eye /></Button>
                      {admin && <Button size="sm" variant="ghost" title={t("common.edit")} onClick={() => guard.run(() => setEditing(it))}><Pencil /></Button>}
                      {admin && <Button size="sm" variant="ghost" title={t("common.delete")} onClick={() => guard.run(() => setDeleting(it))}><Trash2 /></Button>}
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
      {editing && <ItemDialog item={editing === "new" ? null : editing} defaultCustomer={cid} onClose={() => setEditing(null)} />}
      {viewing && <ItemView id={viewing} admin={admin} onClose={() => setViewing(null)} onEdit={(it) => { setViewing(null); guard.run(() => setEditing(it)); }} />}
      <Dialog open={!!deleting} onOpenChange={(v) => !v && setDeleting(null)} title={t("vault.delete_title")} description={deleting?.name}
        footer={<><Button onClick={() => setDeleting(null)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => deleting && remove.mutate(deleting.id)}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("vault.delete_desc")}</p>
      </Dialog>
      {guard.dialog}
    </div>
  );
}

function Secret({ value, mono = true }: { value: string; mono?: boolean }) {
  const { t } = useTranslation();
  const [show, setShow] = useState(false);
  const [done, setDone] = useState(false);
  if (!value) return <span className="text-muted">–</span>;
  return (
    <span className="inline-flex max-w-full items-center gap-1.5">
      <span className={cn("min-w-0 break-all", mono && "mono text-[13px]")}>{show ? value : "••••••••••••"}</span>
      <button type="button" className="cursor-pointer rounded p-1 text-muted hover:bg-sunken hover:text-ink" title={show ? t("vault.hide") : t("vault.show")} onClick={() => setShow(!show)}>{show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}</button>
      <button type="button" className="cursor-pointer rounded p-1 text-muted hover:bg-sunken hover:text-ink" title={t("common.copy")}
        onClick={async () => { await copyText(value); setDone(true); setTimeout(() => setDone(false), 1500); }}>{done ? <Check className="size-4 text-online" /> : <Copy className="size-4" />}</button>
    </span>
  );
}

function ItemView({ id, admin, onClose, onEdit }: { id: string; admin: boolean; onClose: () => void; onEdit: (it: Item) => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const item = useQuery({ queryKey: ["vault-item", id], queryFn: () => get<Item>(`api/vault/items/${id}`) });
  const access = useQuery({ queryKey: ["vault-access", "item", id], queryFn: () => get<Access[]>(`api/vault/access?item_id=${id}&limit=50`), enabled: admin });
  const [revealed, setRevealed] = useState<Item | null>(null);
  const [old, setOld] = useState<Record<string, { username: string; password: string }>>({});
  const guard = useUnlockGuard();
  const reveal = useMutation({
    mutationFn: () => post<Item>(`api/vault/items/${id}/reveal`, { action: "reveal" }),
    onSuccess: (r) => { setRevealed(r); access.refetch(); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const revealOld = async (hid: string) => {
    try {
      const r = await post<{ username: string; password: string }>(`api/vault/items/${id}/history/${hid}/reveal`);
      setOld({ ...old, [hid]: r });
      access.refetch();
    } catch (e) {
      toast.error(errorMessage(e, t));
    }
  };
  const revoke = useMutation({ mutationFn: () => del(`api/vault/items/${id}/grants`), onSuccess: () => { invalidate(); toast.success(t("vault.grants_revoked")); } });
  const it = revealed ?? item.data;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={it?.name ?? "…"} description={it ? `${it.customer_name ?? ""} · ${t(`vault.kind.${it.kind}`)}` : undefined}
      footer={<>
        {admin && <Button onClick={() => revoke.mutate()} title={t("vault.revoke_hint")}><X /> {t("vault.revoke")}</Button>}
        {admin && item.data && <Button onClick={() => onEdit(item.data)}><Pencil /> {t("common.edit")}</Button>}
        {admin && !revealed && <Button variant="primary" loading={reveal.isPending} onClick={() => guard.run(() => reveal.mutate())}><Eye /> {t("vault.reveal")}</Button>}
      </>}>
      {!it ? <Spinner /> : (
        <div className="space-y-5">
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[140px_minmax(0,1fr)]">
            <dt className="font-semibold text-muted">{t("vault.username")}</dt><dd className="mono text-[13px]">{it.username || "–"}</dd>
            <dt className="font-semibold text-muted">{t("vault.password")}</dt>
            <dd>{revealed ? <Secret value={revealed.password ?? ""} /> : it.has_password ? <span className="text-muted">•••••••• <span className="text-xs">({t("vault.hidden_until_reveal")})</span></span> : "–"}</dd>
            {it.url && <><dt className="font-semibold text-muted">{t("vault.url")}</dt><dd className="break-all">{/^https?:\/\//.test(it.url) ? <a href={it.url} target="_blank" rel="noopener noreferrer" className="text-accent-text underline">{it.url}</a> : it.url}</dd></>}
            {it.fields.map((f) => (
              <Fragment key={f.name}><dt className="font-semibold text-muted">{f.name}</dt>
                <dd>{f.secret ? (revealed ? <Secret value={revealed.fields.find((x) => x.name === f.name)?.value ?? ""} /> : <span className="text-muted">••••••••</span>) : <span className="break-all">{f.value}</span>}</dd></Fragment>
            ))}
            {(it.has_notes || revealed?.notes) && <><dt className="font-semibold text-muted">{t("vault.notes")}</dt><dd>{revealed ? <pre className="mono whitespace-pre-wrap text-[13px]">{revealed.notes}</pre> : <span className="text-muted">{t("vault.hidden_until_reveal")}</span>}</dd></>}
            <dt className="font-semibold text-muted">{t("doc.machines")}</dt><dd>{it.machines.length ? <span className="flex flex-wrap gap-1.5">{it.machines.map((m) => <Badge key={m.id} tone="neutral">{m.name ?? "?"}</Badge>)}</span> : "–"}</dd>
            <dt className="font-semibold text-muted">{t("vault.ai_access")}</dt><dd><AiBadge a={it.ai_access} /> <span className="text-xs text-muted">{t(`vault.ai.${it.ai_access}_hint`)}</span></dd>
            <dt className="font-semibold text-muted">{t("vault.changed")}</dt>
            <dd>{formatDate(it.secret_changed_at, i18n.language)}{it.rotate_days ? <span className={cn("ml-2 text-xs", it.rotation_due ? "font-semibold text-warning" : "text-muted")}>{it.rotation_due ? t("vault.rotation_due") : t("vault.rotate_every", { days: it.rotate_days })}</span> : null}</dd>
          </dl>
          {!!item.data?.history?.length && admin && (
            <div>
              <h3 className="mb-2 flex items-center gap-2 text-sm font-bold"><History className="size-4" />{t("vault.history")}</h3>
              <div className="space-y-1.5">
                {item.data.history.map((h) => (
                  <div key={h.id} className="flex flex-wrap items-center gap-2 rounded-md border border-line px-3 py-1.5 text-sm">
                    <span className="text-muted">{formatDate(h.created_at, i18n.language)} · {h.created_by ?? "–"}</span>
                    <span className="ml-auto">{old[h.id] ? <Secret value={old[h.id].password} /> : <Button size="sm" variant="ghost" onClick={() => guard.run(() => revealOld(h.id))}><Eye /> {t("vault.reveal")}</Button>}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {admin && (
            <div>
              <h3 className="mb-2 text-sm font-bold">{t("vault.access_log")}</h3>
              <AccessTable rows={access.data ?? []} compact />
            </div>
          )}
        </div>
      )}
      {guard.dialog}
    </Dialog>
  );
}

function ItemDialog({ item, defaultCustomer, onClose }: { item: Item | null; defaultCustomer: string; onClose: () => void }) {
  const { t } = useTranslation();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const [f, setF] = useState({
    customer_id: item?.customer_id ?? defaultCustomer, name: item?.name ?? "", kind: (item?.kind ?? "login") as Kind, url: item?.url ?? "",
    username: item?.username ?? "", password: "", notes: "", fields: (item?.fields ?? []).map((x) => ({ ...x, value: x.secret ? "" : x.value ?? "" })),
    tags: (item?.tags ?? []).join(", "), node_ids: (item?.machines ?? []).map((m) => m.id), ai_access: item?.ai_access ?? "approval",
    rotate_days: item?.rotate_days ? String(item.rotate_days) : "",
  });
  const [showPw, setShowPw] = useState(false);
  const set = (p: Partial<typeof f>) => setF({ ...f, ...p });
  const gen = useMutation({
    mutationFn: (words: number) => post<{ password: string }>("api/vault/generate", { length: 24, words }),
    onSuccess: (r) => { set({ password: r.password }); setShowPw(true); },
  });
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: f.name, kind: f.kind, url: f.url || null, username: f.username, tags: f.tags.split(/[,\s]+/).filter(Boolean),
        node_ids: f.node_ids, ai_access: f.ai_access, rotate_days: f.rotate_days ? Number(f.rotate_days) : item ? 0 : null,
        fields: f.fields.filter((x) => x.name.trim()).map((x) => ({ name: x.name.trim(), value: x.value ?? "", secret: x.secret })),
      };
      if (!item || f.password) body.password = f.password;
      if (!item || f.notes) body.notes = f.notes;
      return item ? patch(`api/vault/items/${item.id}`, body) : post("api/vault/items", { ...body, customer_id: f.customer_id });
    },
    onSuccess: () => { invalidate(); toast.success(t("common.saved")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={item ? t("vault.edit_title") : t("vault.new")} description={t("vault.edit_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!f.name.trim() || !f.customer_id} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label={t("doc.customer")}>
          <Select value={f.customer_id} disabled={!!item} onChange={(e) => set({ customer_id: e.target.value, node_ids: [] })}>
            <option value="">–</option>
            {customers.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </Select>
        </Field>
        <Field label={t("vault.kind_label")}>
          <Select value={f.kind} onChange={(e) => set({ kind: e.target.value as Kind })}>
            {KINDS.map((k) => <option key={k} value={k}>{t(`vault.kind.${k}`)}</option>)}
          </Select>
        </Field>
        <Field label={t("common.name")} className="sm:col-span-2"><Input value={f.name} maxLength={200} onChange={(e) => set({ name: e.target.value })} placeholder={t("vault.name_placeholder")} /></Field>
        <Field label={t("vault.username")}><Input value={f.username} autoComplete="off" onChange={(e) => set({ username: e.target.value })} /></Field>
        <Field label={t("vault.password")} hint={item?.has_password ? t("common.secret_keep") : undefined}>
          <div className="flex gap-1.5">
            <Input type={showPw ? "text" : "password"} value={f.password} autoComplete="new-password" className="mono" onChange={(e) => set({ password: e.target.value })} />
            <Button type="button" size="icon" variant="ghost" title={showPw ? t("vault.hide") : t("vault.show")} onClick={() => setShowPw(!showPw)}>{showPw ? <EyeOff /> : <Eye />}</Button>
            <Button type="button" size="icon" title={t("vault.generate")} onClick={() => gen.mutate(0)}><Wand2 /></Button>
          </div>
          <button type="button" className="mt-1 cursor-pointer text-xs text-accent-text hover:underline" onClick={() => gen.mutate(5)}>{t("vault.generate_phrase")}</button>
        </Field>
        <Field label={t("vault.url")} className="sm:col-span-2"><Input value={f.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://10.0.0.1 / ssh://srv01" /></Field>
        <Field label={t("vault.notes")} hint={item?.has_notes ? t("common.secret_keep") : t("vault.notes_hint")} className="sm:col-span-2">
          <Textarea value={f.notes} onChange={(e) => set({ notes: e.target.value })} className="min-h-20" />
        </Field>
        <div className="sm:col-span-2">
          <div className="mb-1.5 text-[13px] font-semibold">{t("vault.fields")}</div>
          <div className="space-y-2">
            {f.fields.map((x, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
                <Input value={x.name} placeholder={t("vault.field_name")} className="sm:w-44" onChange={(e) => set({ fields: f.fields.map((y, j) => j === i ? { ...y, name: e.target.value } : y) })} />
                <Input value={x.value ?? ""} type={x.secret ? "password" : "text"} placeholder={x.secret && item ? t("vault.keep_placeholder") : t("vault.field_value")} autoComplete="off"
                  onChange={(e) => set({ fields: f.fields.map((y, j) => j === i ? { ...y, value: e.target.value } : y) })} />
                <Switch checked={x.secret} onCheckedChange={(v) => set({ fields: f.fields.map((y, j) => j === i ? { ...y, secret: v } : y) })} label={<span className="text-xs">{t("vault.secret")}</span>} />
                <Button size="icon" variant="ghost" onClick={() => set({ fields: f.fields.filter((_, j) => j !== i) })}><Trash2 /></Button>
              </div>
            ))}
            <Button size="sm" onClick={() => set({ fields: [...f.fields, { name: "", value: "", secret: true }] })}><Plus /> {t("vault.add_field")}</Button>
          </div>
        </div>
        <Field label={t("doc.machines")} className="sm:col-span-2"><MachinePicker customerId={f.customer_id} value={f.node_ids} onChange={(v) => set({ node_ids: v })} /></Field>
        <Field label={t("vault.ai_access")} className="sm:col-span-2" hint={t(`vault.ai.${f.ai_access}_hint`)}>
          <Segmented value={f.ai_access} onChange={(v) => set({ ai_access: v as "approval" | "never" })}
            options={[{ value: "approval", label: t("vault.ai.approval") }, { value: "never", label: t("vault.ai.never") }]} />
        </Field>
        <Field label={t("vault.rotate_days")} hint={t("vault.rotate_days_hint")}><Input type="number" min={0} max={3650} value={f.rotate_days} onChange={(e) => set({ rotate_days: e.target.value })} /></Field>
        <Field label={t("doc.tags")}><Input value={f.tags} onChange={(e) => set({ tags: e.target.value })} /></Field>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ access log */
function ActionBadge({ a }: { a: Access }) {
  const { t } = useTranslation();
  const tone = !a.success ? "offline" : a.action.startsWith("ai_") ? "accent" : ["reveal", "copy"].includes(a.action) ? "warning" : "neutral";
  return <Badge tone={tone}>{t(`vault.action.${a.action}`, { defaultValue: a.action })}</Badge>;
}

function AccessTable({ rows, compact }: { rows: Access[]; compact?: boolean }) {
  const { t, i18n } = useTranslation();
  if (!rows.length) return <p className="text-sm text-muted">{t("vault.no_access")}</p>;
  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <Table>
        <thead><Tr><Th>{t("vault.when")}</Th>{!compact && <Th>{t("vault.item")}</Th>}<Th>{t("vault.action_label")}</Th><Th>{t("vault.who")}</Th><Th className="hidden md:table-cell">{t("vault.reason")}</Th></Tr></thead>
        <tbody>
          {rows.map((a) => (
            <Tr key={a.id}>
              <Td className="whitespace-nowrap text-[13px]">{formatDate(a.at, i18n.language)}</Td>
              {!compact && <Td className="text-sm">{a.item_name ?? "–"}</Td>}
              <Td><ActionBadge a={a} /></Td>
              <Td className="text-sm">
                {a.actor_kind === "ai" ? <span className="inline-flex items-center gap-1"><Bot className="size-3.5 text-koraal" />{a.token_name ?? "AI"}</span> : a.username ?? "–"}
                {typeof a.detail?.approved_by === "string" && <div className="text-xs text-muted">{t("vault.approved_by", { who: a.detail.approved_by })}</div>}
                {typeof a.detail?.machine === "string" && <div className="text-xs text-muted">{t("vault.on_machine", { machine: a.detail.machine })}</div>}
              </Td>
              <Td className="hidden max-w-xs md:table-cell text-[13px] text-muted">
                {a.reason ?? (typeof a.detail?.field === "string" ? t("vault.field_x", { field: t(`vault.f.${a.detail.field}`, { defaultValue: a.detail.field }) }) : "–")}
                {a.approval_id && <div><Link to={`/ai/approvals?id=${a.approval_id}`} className="text-xs underline">{t("vault.approval")}</Link></div>}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

function AccessTab() {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const [actor, setActor] = useState("");
  const rows = useQuery({ queryKey: ["vault-access", customerId, actor], queryFn: () => get<Access[]>(qs("api/vault/access", { customer_id: customerId, actor, limit: "300" })) });
  const grants = useQuery({ queryKey: ["vault-grants"], queryFn: () => get<Grant[]>("api/vault/grants"), refetchInterval: 30_000 });
  const revokeAll = useMutation({ mutationFn: () => del("api/vault/grants"), onSuccess: () => { invalidate(); toast.success(t("vault.grants_revoked")); } });
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Bot />} title={t("vault.grants_title")} description={t("vault.grants_desc")}
          action={!!grants.data?.length && <Button size="sm" variant="danger" onClick={() => revokeAll.mutate()}>{t("vault.revoke_all")}</Button>} />
        <CardBody>
          {!grants.data?.length ? <p className="text-sm text-muted">{t("vault.no_grants")}</p> : (
            <div className="space-y-1.5">{grants.data.map((g) => (
              <div key={g.id} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-semibold">{g.item_name}</span>
                <span className="text-muted">{t("vault.grant_line", { who: g.approved_by ?? "–", uses: g.uses_left, until: formatDate(g.expires_at, i18n.language) })}</span>
              </div>
            ))}</div>
          )}
        </CardBody>
      </Card>
      <div className="flex items-center gap-2">
        <Segmented value={actor} onChange={setActor} options={[{ value: "", label: t("vault.all") }, { value: "ai", label: t("vault.only_ai") }, { value: "user", label: t("vault.only_people") }]} />
      </div>
      {rows.isLoading ? <Spinner /> : <AccessTable rows={rows.data ?? []} />}
    </div>
  );
}

/* ------------------------------------------------------------------ settings */
function SettingsTab() {
  const { t, i18n } = useTranslation();
  const status = useVaultStatus();
  const invalidate = useInvalidate();
  const cfg = useQuery({ queryKey: ["vault-settings"], queryFn: () => get<VaultSettings>("api/vault/settings") });
  const [f, setF] = useState<VaultSettings | null>(null);
  const [rotating, setRotating] = useState(false);
  const guard = useUnlockGuard();
  useEffect(() => { if (cfg.data && !f) setF(cfg.data); }, [cfg.data, f]);
  const save = useMutation({
    mutationFn: () => put("api/vault/settings", f),
    onSuccess: () => { invalidate(); toast.success(t("common.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rotate = useMutation({
    mutationFn: () => post<{ items: number; attachments: number }>("api/vault/rotate-key"),
    onSuccess: (r) => { invalidate(); setRotating(false); toast.success(t("vault.rotated", { items: r.items, files: r.attachments })); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!f) return <Spinner />;
  const set = (p: Partial<VaultSettings>) => setF({ ...f, ...p });
  const key = status.data?.key;
  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
      <Card>
        <CardHeader icon={<Lock />} title={t("vault.set_title")} description={t("vault.set_desc")} />
        <CardBody className="space-y-4">
          <Switch checked={f.require_unlock} onCheckedChange={(v) => set({ require_unlock: v })} label={t("vault.set_require")} description={t("vault.set_require_hint")} />
          <Field label={t("vault.set_unlock_minutes")}><Input type="number" min={1} max={480} value={f.unlock_minutes} onChange={(e) => set({ unlock_minutes: Number(e.target.value) })} /></Field>
          <Field label={t("vault.set_grant_minutes")} hint={t("vault.set_grant_hint")}><Input type="number" min={1} max={1440} value={f.grant_minutes} onChange={(e) => set({ grant_minutes: Number(e.target.value) })} /></Field>
          <Field label={t("vault.set_grant_uses")}><Input type="number" min={1} max={20} value={f.grant_uses} onChange={(e) => set({ grant_uses: Number(e.target.value) })} /></Field>
          <Field label={t("vault.set_history")}><Input type="number" min={0} max={100} value={f.history_keep} onChange={(e) => set({ history_keep: Number(e.target.value) })} /></Field>
          <Switch checked={f.notify_ai_use} onCheckedChange={(v) => set({ notify_ai_use: v })} label={t("vault.set_notify")} description={t("vault.set_notify_hint")} />
          <div className="flex justify-end"><Button variant="primary" loading={save.isPending} onClick={() => guard.run(() => save.mutate())}>{t("common.save")}</Button></div>
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<ShieldCheck />} title={t("vault.key_title")} description={t("vault.key_desc")} />
        <CardBody className="space-y-3 text-sm">
          <dl className="grid grid-cols-[140px_minmax(0,1fr)] gap-y-2">
            <dt className="text-muted">{t("vault.key_algorithm")}</dt><dd>{key?.algorithm ?? "–"}</dd>
            <dt className="text-muted">{t("vault.key_id")}</dt><dd className="mono">{key?.active ?? "–"}</dd>
            <dt className="text-muted">{t("vault.key_created")}</dt><dd>{formatDate(key?.created_at, i18n.language)}</dd>
            <dt className="text-muted">{t("vault.key_rotated")}</dt><dd>{key?.rotated_at ? formatDate(key.rotated_at, i18n.language) : t("vault.never")}</dd>
          </dl>
          <Alert tone="info">{t("vault.key_backup")}</Alert>
          <div className="flex justify-end"><Button onClick={() => guard.run(() => setRotating(true))}><RefreshCw /> {t("vault.rotate")}</Button></div>
        </CardBody>
      </Card>
      <Dialog open={rotating} onOpenChange={setRotating} title={t("vault.rotate")} description={t("vault.rotate_desc")}
        footer={<><Button onClick={() => setRotating(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={rotate.isPending} onClick={() => rotate.mutate()}>{t("vault.rotate")}</Button></>}>
        <p className="text-sm">{t("vault.rotate_hint")}</p>
      </Dialog>
      {guard.dialog}
    </div>
  );
}
