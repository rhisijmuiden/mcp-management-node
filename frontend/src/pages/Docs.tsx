import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookText, Bot, Download, EyeOff, FileText, History, Laptop, Paperclip, Pencil, Pin, Plus, RotateCcw, Search, Server,
  Trash2, Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { api, apiUrl, ApiError, del, errorMessage, get, patch, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatBytes, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
export const DOC_CATEGORIES = ["network", "procedure", "notes", "contacts", "howto", "hardware", "software", "security", "other"] as const;
type Category = (typeof DOC_CATEGORIES)[number];
interface MachineRef { id: string; name: string | null }
interface Attachment { id: string; filename: string; content_type: string; size: number; created_at: string; created_by: string | null }
interface Revision { id: string; version: number; title: string; created_at: string; created_by: string | null; body?: string }
export interface Doc {
  id: string; customer_id: string; customer_name: string | null; title: string; category: Category; tags: string[]; pinned: boolean;
  ai_visible: boolean; machines: MachineRef[]; version: number; created_at: string; updated_at: string; created_by: string | null;
  updated_by: string | null; excerpt: string; body?: string; snippets?: string[]; attachments?: Attachment[]; revisions?: Revision[];
}
interface Counts { documents: number; credentials: number; rotation_due: number; ai_never: number }
interface Template { key: string; category: Category; title: string; body: string }
export interface Machine { id: string; name: string; type: "server" | "workstation"; online: boolean; kind: string }

function qs(path: string, params: Record<string, string | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) u.set(k, v);
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

const CAT_TONE: Record<Category, "info" | "accent" | "neutral" | "warning" | "online" | "offline"> = {
  network: "info", procedure: "accent", notes: "neutral", contacts: "online", howto: "accent", hardware: "neutral", software: "neutral",
  security: "warning", other: "neutral",
};

export function CategoryBadge({ c }: { c: Category }) {
  const { t } = useTranslation();
  return <Badge tone={CAT_TONE[c] ?? "neutral"}>{t(`doc.cat.${c}`)}</Badge>;
}

export function useMachines(customerId: string | undefined) {
  return useQuery({
    queryKey: ["customer-machines", customerId],
    queryFn: () => get<Machine[]>(`api/tickets/machines?customer_id=${customerId}`),
    enabled: !!customerId,
  });
}

/** Machines of a customer as checkboxes (used by documents and vault items). */
export function MachinePicker({ customerId, value, onChange }: { customerId: string; value: string[]; onChange: (v: string[]) => void }) {
  const { t } = useTranslation();
  const machines = useMachines(customerId);
  if (!customerId) return <p className="text-sm text-muted">{t("doc.pick_customer_first")}</p>;
  if (machines.isLoading) return <Spinner />;
  if (!machines.data?.length) return <p className="text-sm text-muted">{t("doc.no_machines")}</p>;
  return (
    <div className="grid max-h-44 grid-cols-1 gap-1.5 overflow-y-auto rounded-md border border-line p-2 sm:grid-cols-2">
      {machines.data.map((m) => (
        <Checkbox key={m.id} checked={value.includes(m.id)} label={<span className="inline-flex items-center gap-1.5">{m.type === "workstation" ? <Laptop className="size-3.5 text-muted" /> : <Server className="size-3.5 text-muted" />}{m.name}</span>}
          onCheckedChange={(on) => onChange(on ? [...value, m.id] : value.filter((x) => x !== m.id))} />
      ))}
    </div>
  );
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => { for (const k of ["documents", "document"]) qc.invalidateQueries({ queryKey: [k] }); };
}

/* ------------------------------------------------------------------ list */
export function DocsPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const navigate = useNavigate();
  const [filterCustomer, setFilterCustomer] = useState("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [cat, setCat] = useState<string>("");
  const [newOpen, setNewOpen] = useState(false);
  useEffect(() => { const h = setTimeout(() => setQuery(q), 250); return () => clearTimeout(h); }, [q]);
  const cid = customerId || filterCustomer;
  const list = useQuery({
    queryKey: ["documents", cid, query, cat],
    queryFn: () => get<{ documents: Doc[]; counts: Counts }>(qs("api/documents", { customer_id: cid, q: query, category: cat })),
  });
  const docs = list.data?.documents ?? [];
  const byCat = useMemo(() => {
    const m: Record<string, number> = {};
    for (const d of docs) m[d.category] = (m[d.category] ?? 0) + 1;
    return m;
  }, [docs]);
  const c = list.data?.counts;
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><BookText className="hidden size-6 shrink-0 text-koraal sm:block" /><span>{t("doc.title")}</span></span>}
        description={t("doc.desc")}
        actions={admin && <Button variant="primary" onClick={() => setNewOpen(true)}><Plus /> {t("doc.new")}</Button>} />
      <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("doc.k_pages")} value={c?.documents ?? "–"} hint={t("doc.k_pages_hint")} tone="info" icon={<FileText />} />
        <Stat label={t("doc.k_credentials")} value={c?.credentials ?? "–"} hint={<Link to="/vault" className="underline">{t("doc.k_credentials_hint")}</Link>} tone="neutral" icon={<BookText />} />
        <Stat label={t("doc.k_network")} value={byCat.network ?? 0} hint={t("doc.k_network_hint")} tone="neutral" icon={<Server />} />
        <Stat label={t("doc.k_procedures")} value={byCat.procedure ?? 0} hint={t("doc.k_procedures_hint")} tone="neutral" icon={<FileText />} />
      </div>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("doc.search")} className="pl-9" />
        </div>
        {!customerId && (
          <Select value={filterCustomer} onChange={(e) => setFilterCustomer(e.target.value)} className="sm:w-56">
            <option value="">{t("doc.all_customers")}</option>
            {customers.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </Select>
        )}
        <Select value={cat} onChange={(e) => setCat(e.target.value)} className="sm:w-48">
          <option value="">{t("doc.all_categories")}</option>
          {DOC_CATEGORIES.map((k) => <option key={k} value={k}>{t(`doc.cat.${k}`)}</option>)}
        </Select>
      </div>
      {list.isLoading ? <Spinner /> : !docs.length ? (
        <EmptyState icon={<BookText />} title={query || cat ? t("doc.none_found") : t("doc.empty")}
          action={admin && !query && <Button variant="primary" onClick={() => setNewOpen(true)}><Plus /> {t("doc.new")}</Button>}>
          {!query && !cat && t("doc.empty_hint")}
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {docs.map((d) => (
            <Link key={d.id} to={`/docs/${d.id}`} className="group block">
              <Card className="h-full transition-shadow group-hover:shadow-md">
                <CardBody className="space-y-2">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 font-bold">
                        {d.pinned && <Pin className="size-3.5 shrink-0 text-koraal" />}
                        <span className="truncate">{d.title}</span>
                      </div>
                      {!customerId && <div className="truncate text-xs text-muted">{d.customer_name}</div>}
                    </div>
                    <CategoryBadge c={d.category} />
                  </div>
                  {d.snippets?.length ? (
                    <div className="space-y-1 text-[13px] text-muted">{d.snippets.map((s, i) => <p key={i} className="line-clamp-2">{s}</p>)}</div>
                  ) : <p className="line-clamp-3 text-[13px] text-muted">{d.excerpt || t("doc.no_text")}</p>}
                  <div className="flex flex-wrap items-center gap-1.5 pt-1 text-xs text-muted">
                    {d.machines.slice(0, 3).map((m) => <Badge key={m.id} tone="neutral">{m.name ?? "?"}</Badge>)}
                    {d.machines.length > 3 && <span>+{d.machines.length - 3}</span>}
                    {d.tags.slice(0, 3).map((tg) => <span key={tg}>#{tg}</span>)}
                    {!d.ai_visible && <span className="inline-flex items-center gap-1" title={t("doc.ai_hidden")}><EyeOff className="size-3.5" /></span>}
                    <span className="ml-auto">{timeAgo(d.updated_at, i18n.language)}</span>
                  </div>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      )}
      {newOpen && <NewDocDialog onClose={() => setNewOpen(false)} customerId={cid} onPick={(tpl, cust) => {
        setNewOpen(false);
        navigate(qs("/docs/new", { customer: cust, template: tpl }));
      }} />}
    </div>
  );
}

function NewDocDialog({ onClose, onPick, customerId }: { onClose: () => void; onPick: (tpl: string, customer: string) => void; customerId: string }) {
  const { t, i18n } = useTranslation();
  const customers = useCustomers();
  const [cust, setCust] = useState(customerId);
  const tpls = useQuery({ queryKey: ["doc-templates", i18n.language], queryFn: () => get<Template[]>(`api/documents/templates?lang=${i18n.language}`) });
  const options = [{ key: "", title: t("doc.blank"), category: "notes" as Category }, ...(tpls.data ?? [])];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("doc.new")} description={t("doc.new_desc")}>
      <div className="space-y-4">
        <Field label={t("doc.customer")}>
          <Select value={cust} onChange={(e) => setCust(e.target.value)}>
            <option value="">–</option>
            {customers.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </Select>
        </Field>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {options.map((o) => (
            <button key={o.key || "blank"} type="button" disabled={!cust} onClick={() => onPick(o.key, cust)}
              className="flex cursor-pointer items-center gap-3 rounded-md border border-line p-3 text-left hover:border-koraal hover:bg-sunken disabled:cursor-not-allowed disabled:opacity-50">
              <FileText className="size-5 shrink-0 text-koraal" />
              <span className="min-w-0">
                <span className="block truncate text-sm font-semibold">{o.title.replace(/:\s*$/, "")}</span>
                <span className="block text-xs text-muted">{t(`doc.cat.${o.category}`)}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ detail */
export function DocDetailPage() {
  const { id = "" } = useParams();
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const doc = useQuery({ queryKey: ["document", id], queryFn: () => get<Doc>(`api/documents/${id}`) });
  const [revision, setRevision] = useState<Revision | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: async (files: FileList) => {
      for (const f of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", f, f.name);
        await api(`api/documents/${id}/attachments`, { method: "POST", body: fd });
      }
    },
    onSuccess: () => { invalidate(); toast.success(t("doc.uploaded")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const removeAtt = useMutation({
    mutationFn: (aid: string) => del(`api/documents/${id}/attachments/${aid}`),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/documents/${id}`),
    onSuccess: () => { invalidate(); toast.success(t("doc.deleted")); navigate("/docs"); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const pin = useMutation({
    mutationFn: (v: boolean) => patch(`api/documents/${id}`, { pinned: v }),
    onSuccess: () => invalidate(),
  });
  if (doc.isLoading) return <Spinner />;
  if (!doc.data) return <Alert tone="offline">{t("doc.not_found")}</Alert>;
  const d = doc.data;
  return (
    <div>
      <div className="mb-2 text-sm"><Link to="/docs" className="text-muted hover:text-ink">← {t("doc.title")}</Link></div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2">{d.pinned && <Pin className="size-5 shrink-0 text-koraal" />}<span className="truncate">{d.title}</span></span>}
        description={<span className="flex flex-wrap items-center gap-2"><CategoryBadge c={d.category} /><span>{d.customer_name}</span><span>·</span>
          <span>{t("doc.version_by", { version: d.version, who: d.updated_by ?? "–", when: timeAgo(d.updated_at, i18n.language) })}</span></span>}
        actions={admin && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => pin.mutate(!d.pinned)}><Pin /> {d.pinned ? t("doc.unpin") : t("doc.pin")}</Button>
            <Button size="sm" onClick={() => setConfirmDelete(true)}><Trash2 /> {t("common.delete")}</Button>
            <Button size="sm" variant="primary" onClick={() => navigate(`/docs/${d.id}/edit`)}><Pencil /> {t("common.edit")}</Button>
          </div>
        )} />
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
        <Card>
          <CardBody className="px-5 py-5 sm:px-7">
            <Markdown empty={t("doc.no_text")}>{d.body ?? ""}</Markdown>
          </CardBody>
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader title={t("doc.details")} />
            <CardBody className="space-y-3 text-sm">
              <div>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{t("doc.machines")}</div>
                {d.machines.length ? <div className="flex flex-wrap gap-1.5">{d.machines.map((m) => <Badge key={m.id} tone="neutral">{m.name ?? "?"}</Badge>)}</div> : <span className="text-muted">–</span>}
              </div>
              <div>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{t("doc.tags")}</div>
                {d.tags.length ? <div className="flex flex-wrap gap-1.5">{d.tags.map((x) => <Badge key={x} tone="accent">#{x}</Badge>)}</div> : <span className="text-muted">–</span>}
              </div>
              <div className="flex items-center gap-2">
                {d.ai_visible ? <><Bot className="size-4 text-online" /><span>{t("doc.ai_visible_yes")}</span></> : <><EyeOff className="size-4 text-muted" /><span>{t("doc.ai_hidden")}</span></>}
              </div>
              <div className="text-xs text-muted">{t("doc.created_by", { who: d.created_by ?? "–", when: formatDate(d.created_at, i18n.language) })}</div>
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={<span className="flex items-center gap-2"><Paperclip className="size-4" />{t("doc.attachments")}</span>}
              action={admin && <>
                <input ref={fileRef} type="file" multiple className="hidden" onChange={(e) => { if (e.target.files?.length) upload.mutate(e.target.files); e.target.value = ""; }} />
                <Button size="sm" loading={upload.isPending} onClick={() => fileRef.current?.click()}><Upload /> {t("doc.upload")}</Button>
              </>} />
            <CardBody className="space-y-2">
              {!d.attachments?.length && <p className="text-sm text-muted">{t("doc.no_attachments")}</p>}
              {d.attachments?.map((a) => {
                const viewable = a.content_type === "application/pdf" || (a.content_type.startsWith("image/") && a.content_type !== "image/svg+xml");
                return (
                  <div key={a.id} className="flex items-center gap-2 rounded-md border border-line px-2.5 py-2 text-sm">
                    <Paperclip className="size-4 shrink-0 text-muted" />
                    <div className="min-w-0 flex-1">
                      <a className="block truncate font-medium hover:underline" target="_blank" rel="noopener noreferrer"
                        href={apiUrl(`api/documents/${d.id}/attachments/${a.id}${viewable ? "?inline=true" : ""}`)}>{a.filename}</a>
                      <div className="text-xs text-muted">{formatBytes(a.size)} · {timeAgo(a.created_at, i18n.language)}</div>
                    </div>
                    <a href={apiUrl(`api/documents/${d.id}/attachments/${a.id}`)} className="rounded p-1 text-muted hover:bg-sunken hover:text-ink" title={t("doc.download")}><Download className="size-4" /></a>
                    {admin && <button type="button" className="cursor-pointer rounded p-1 text-muted hover:bg-sunken hover:text-offline" title={t("common.delete")}
                      onClick={() => removeAtt.mutate(a.id)}><Trash2 className="size-4" /></button>}
                  </div>
                );
              })}
              <p className="text-xs text-muted">{t("doc.attachments_hint")}</p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={<span className="flex items-center gap-2"><History className="size-4" />{t("doc.history")}</span>} />
            <CardBody className="space-y-1.5">
              <div className="flex items-center justify-between text-sm"><span className="font-semibold">v{d.version} · {t("doc.current")}</span><span className="text-xs text-muted">{timeAgo(d.updated_at, i18n.language)}</span></div>
              {!d.revisions?.length && <p className="text-sm text-muted">{t("doc.no_history")}</p>}
              {d.revisions?.slice(0, 15).map((r) => (
                <button key={r.id} type="button" onClick={() => setRevision(r)}
                  className="flex w-full cursor-pointer items-center justify-between rounded px-1 py-1 text-left text-sm hover:bg-sunken">
                  <span>v{r.version} · <span className="text-muted">{r.created_by ?? "–"}</span></span>
                  <span className="text-xs text-muted">{formatDate(r.created_at, i18n.language)}</span>
                </button>
              ))}
            </CardBody>
          </Card>
        </div>
      </div>
      {revision && <RevisionDialog doc={d} rev={revision} admin={admin} onClose={() => setRevision(null)} />}
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete} title={t("doc.delete_title")} description={t("doc.delete_desc", { title: d.title })}
        footer={<><Button onClick={() => setConfirmDelete(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("doc.delete_hint")}</p>
      </Dialog>
    </div>
  );
}

function RevisionDialog({ doc, rev, admin, onClose }: { doc: Doc; rev: Revision; admin: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const full = useQuery({ queryKey: ["doc-revision", rev.id], queryFn: () => get<Revision>(`api/documents/${doc.id}/revisions/${rev.id}`) });
  const restore = useMutation({
    mutationFn: () => post(`api/documents/${doc.id}/revisions/${rev.id}/restore`),
    onSuccess: () => { invalidate(); toast.success(t("doc.restored", { version: rev.version })); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={`${rev.title} · v${rev.version}`}
      description={t("doc.revision_desc", { who: rev.created_by ?? "–", when: formatDate(rev.created_at, i18n.language) })}
      footer={<><Button onClick={onClose}>{t("common.close")}</Button>{admin && <Button variant="primary" loading={restore.isPending} onClick={() => restore.mutate()}><RotateCcw /> {t("doc.restore")}</Button>}</>}>
      {full.isLoading ? <Spinner /> : <Markdown empty={t("doc.no_text")}>{full.data?.body ?? ""}</Markdown>}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ editor */
export function DocEditorPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const customers = useCustomers();
  const existing = useQuery({ queryKey: ["document", id], queryFn: () => get<Doc>(`api/documents/${id}`), enabled: !!id });
  const tplKey = params.get("template") || "";
  const tpls = useQuery({ queryKey: ["doc-templates", i18n.language], queryFn: () => get<Template[]>(`api/documents/templates?lang=${i18n.language}`), enabled: !id && !!tplKey });
  const [form, setForm] = useState<{ customer_id: string; title: string; body: string; category: Category; tags: string; node_ids: string[]; ai_visible: boolean; pinned: boolean } | null>(null);
  const [view, setView] = useState<"split" | "edit" | "preview">("split");
  const [conflict, setConflict] = useState(false);
  const base = useRef<number | null>(null);

  useEffect(() => {
    if (form) return;
    if (id && existing.data) {
      const d = existing.data;
      base.current = d.version;
      setForm({ customer_id: d.customer_id, title: d.title, body: d.body ?? "", category: d.category, tags: d.tags.join(", "), node_ids: d.machines.map((m) => m.id), ai_visible: d.ai_visible, pinned: d.pinned });
    } else if (!id && (!tplKey || tpls.data)) {
      const tpl = tpls.data?.find((x) => x.key === tplKey);
      setForm({ customer_id: params.get("customer") || "", title: tpl?.title ?? "", body: tpl?.body ?? "", category: tpl?.category ?? "notes", tags: "", node_ids: [], ai_visible: true, pinned: false });
    }
  }, [id, existing.data, tpls.data, tplKey, params, form]);

  const save = useMutation({
    mutationFn: async () => {
      const f = form!;
      const payload = { title: f.title, body: f.body, category: f.category, tags: f.tags.split(/[,\s]+/).filter(Boolean), node_ids: f.node_ids, ai_visible: f.ai_visible, pinned: f.pinned };
      if (id) return patch<Doc>(`api/documents/${id}`, { ...payload, base_version: base.current });
      return post<Doc>("api/documents", { ...payload, customer_id: f.customer_id });
    },
    onSuccess: (d) => { invalidate(); toast.success(t("common.saved")); navigate(`/docs/${d.id}`); },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      else toast.error(errorMessage(e, t));
    },
  });
  if (!form) return <Spinner />;
  const set = (p: Partial<typeof form>) => setForm({ ...form, ...p });
  return (
    <div>
      <div className="mb-2 text-sm"><Link to={id ? `/docs/${id}` : "/docs"} className="text-muted hover:text-ink">← {id ? form.title : t("doc.title")}</Link></div>
      <PageHeader title={id ? t("doc.edit_title") : t("doc.new")} description={t("doc.editor_desc")}
        actions={<div className="flex gap-2"><Button onClick={() => navigate(id ? `/docs/${id}` : "/docs")}>{t("common.cancel")}</Button>
          <Button variant="primary" loading={save.isPending} disabled={!form.title.trim() || !form.customer_id} onClick={() => save.mutate()}>{t("common.save")}</Button></div>} />
      {conflict && <Alert tone="warning" className="mb-4" title={t("doc.conflict_title")}>{t("doc.conflict")}</Alert>}
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_300px]">
        <Card>
          <CardBody className="space-y-3">
            <Input value={form.title} onChange={(e) => set({ title: e.target.value })} placeholder={t("doc.title_placeholder")} className="h-11 text-base font-semibold" maxLength={200} />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="inline-flex rounded-md border border-line bg-sunken p-0.5">
                {(["edit", "split", "preview"] as const).map((v) => (
                  <button key={v} type="button" onClick={() => setView(v)} className={cn("cursor-pointer rounded-[8px] px-3 py-1 text-[13px] font-semibold", view === v ? "bg-raised text-ink shadow-sm" : "text-muted", v === "split" && "hidden lg:block")}>{t(`doc.view.${v}`)}</button>
                ))}
              </div>
              <span className="text-xs text-muted">{t("doc.markdown_hint")}</span>
            </div>
            <div className={cn("grid gap-4", view === "split" && "lg:grid-cols-2")}>
              {view !== "preview" && (
                <Textarea value={form.body} onChange={(e) => set({ body: e.target.value })} className="min-h-[60vh] resize-y" spellCheck={false} placeholder={t("doc.body_placeholder")} />
              )}
              {view !== "edit" && (
                <div className={cn("min-h-[60vh] overflow-y-auto rounded-sm border border-line px-4 py-3", view === "split" && "hidden lg:block")}>
                  <Markdown empty={t("doc.preview_empty")}>{form.body}</Markdown>
                </div>
              )}
            </div>
            <Alert tone="info">{t("doc.no_passwords")}</Alert>
          </CardBody>
        </Card>
        <Card>
          <CardBody className="space-y-4">
            <Field label={t("doc.customer")}>
              <Select value={form.customer_id} disabled={!!id} onChange={(e) => set({ customer_id: e.target.value, node_ids: [] })}>
                <option value="">–</option>
                {customers.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </Select>
            </Field>
            <Field label={t("doc.category")}>
              <Select value={form.category} onChange={(e) => set({ category: e.target.value as Category })}>
                {DOC_CATEGORIES.map((k) => <option key={k} value={k}>{t(`doc.cat.${k}`)}</option>)}
              </Select>
            </Field>
            <Field label={t("doc.tags")} hint={t("doc.tags_hint")}>
              <Input value={form.tags} onChange={(e) => set({ tags: e.target.value })} placeholder="vlan, firewall" />
            </Field>
            <Field label={t("doc.machines")}>
              <MachinePicker customerId={form.customer_id} value={form.node_ids} onChange={(v) => set({ node_ids: v })} />
            </Field>
            <Switch checked={form.ai_visible} onCheckedChange={(v) => set({ ai_visible: v })} label={t("doc.ai_visible")} description={t("doc.ai_visible_hint")} />
            <Switch checked={form.pinned} onCheckedChange={(v) => set({ pinned: v })} label={t("doc.pinned")} description={t("doc.pinned_hint")} />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
