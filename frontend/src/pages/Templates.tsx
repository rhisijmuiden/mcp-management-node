import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CloudDownload, Layers, Link2, Pencil, Plus, RefreshCw, Search, Send, Settings2, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { apiUrl, del, errorMessage, get, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { CatalogItem, CtTemplate, TemplateLibrary, TemplateLocation } from "@/lib/types";
import { cn, formatBytes, timeAgo } from "@/lib/utils";

const statusTone = { current: "online", outdated: "warning", missing: "offline", unknown: "neutral" } as const;

function LocationCell({ loc }: { loc?: TemplateLocation }) {
  const { t } = useTranslation();
  if (!loc) return <span className="text-muted">–</span>;
  return (
    <span title={[loc.storage, loc.volid, ...(loc.present ?? [])].filter(Boolean).join("\n")}>
      <Badge tone={statusTone[loc.status]} dot className={cn(!loc.online && "opacity-60")}>{t(`tpl.status_${loc.status}`)}</Badge>
    </span>
  );
}

/** Upload with progress (fetch has no upload progress). */
function uploadFile(file: File, q: Record<string, string>, onProgress: (p: number) => void): Promise<CtTemplate> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", apiUrl(`api/templates/upload?${new URLSearchParams(q)}`));
    const m = document.cookie.match(/(?:^|;\s*)mmn_csrf=([^;]+)/);
    xhr.setRequestHeader("X-CSRF-Token", m ? decodeURIComponent(m[1]) : "");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data: unknown = xhr.responseText;
      try { data = JSON.parse(xhr.responseText); } catch { /* text */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as CtTemplate);
      else reject(new Error(typeof data === "object" && data && "detail" in data ? String((data as { detail: unknown }).detail) : `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.send(file);
  });
}

function AddOfficialDialog({ open, onOpenChange, existing }: { open: boolean; onOpenChange: (v: boolean) => void; existing: Set<string> }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const cat = useQuery({ queryKey: ["template-catalog"], queryFn: () => get<{ checked_at: string | null; items: CatalogItem[] }>("api/templates/catalog"), enabled: open });
  const refresh = useMutation({ mutationFn: () => post("api/templates/catalog/refresh"), onSuccess: () => cat.refetch(), onError: (e) => toast.error(errorMessage(e, t)) });
  const add = useMutation({
    mutationFn: (filename: string) => post<CtTemplate>("api/templates/official", { filename, sync: true }),
    onSuccess: (x) => { toast.success(t("tpl.added", { name: x.name })); qc.invalidateQueries({ queryKey: ["templates"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const items = (cat.data?.items ?? []).filter((i) => !q || i.filename.toLowerCase().includes(q.toLowerCase()) || (i.headline ?? "").toLowerCase().includes(q.toLowerCase()));
  const sections = [...new Set(items.map((i) => i.section ?? "system"))].sort((a, b) => (a === "system" ? -1 : b === "system" ? 1 : a.localeCompare(b)));
  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide title={t("tpl.add_official")} description={t("tpl.add_official_desc")}>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="debian, ubuntu, alpine…" className="h-9 pl-9" autoFocus />
          </div>
          <Button size="sm" onClick={() => refresh.mutate()} loading={refresh.isPending}>{!refresh.isPending && <RefreshCw />} {t("tpl.catalog_refresh")}</Button>
        </div>
        <p className="text-xs text-muted">{cat.data?.checked_at ? t("tpl.catalog_checked", { when: timeAgo(cat.data.checked_at, i18n.language) }) : t("tpl.catalog_never")}</p>
        {cat.isLoading ? <Spinner /> : !items.length ? <p className="py-6 text-center text-sm text-muted">{t("tpl.catalog_empty")}</p> : (
          <div className="max-h-[55vh] space-y-4 overflow-y-auto">
            {sections.map((s) => (
              <div key={s}>
                <div className="pb-1 text-[11px] font-bold uppercase tracking-wider text-muted">{s}</div>
                <div className="divide-y divide-line rounded-md border border-line">
                  {items.filter((i) => (i.section ?? "system") === s).map((i) => (
                    <div key={i.filename} className="flex items-center gap-3 px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="mono truncate text-[12.5px] font-semibold">{i.filename}</div>
                        {i.headline && <div className="truncate text-xs text-muted">{i.headline}</div>}
                      </div>
                      {existing.has(i.family) ? <Badge tone="online"><Check className="size-3" /> {t("tpl.in_library")}</Badge>
                        : <Button size="sm" onClick={() => add.mutate(i.filename)} loading={add.isPending && add.variables === i.filename}><Plus /> {t("tpl.add")}</Button>}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Dialog>
  );
}

function CustomDialog({ open, onOpenChange, replace }: { open: boolean; onOpenChange: (v: boolean) => void; replace?: CtTemplate | null }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [src, setSrc] = useState<"file" | "url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [name, setName] = useState(replace?.name ?? "");
  const [desc, setDesc] = useState(replace?.description ?? "");
  const [progress, setProgress] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const go = useMutation({
    mutationFn: async () => {
      if (src === "file" && file) {
        setProgress(0);
        const q: Record<string, string> = { filename: file.name, name: name || file.name };
        if (desc) q.description = desc;
        if (replace) q.replace_id = replace.id;
        return uploadFile(file, q, setProgress);
      }
      return post("api/templates/from-url", { url, name: name || url.split("/").pop(), description: desc || undefined, replace_id: replace?.id });
    },
    onSuccess: () => {
      toast.success(src === "file" ? t("tpl.uploaded") : t("tpl.download_started"));
      qc.invalidateQueries({ queryKey: ["templates"] });
      onOpenChange(false);
    },
    onError: (e) => toast.error(errorMessage(e, t)),
    onSettled: () => setProgress(null),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={replace ? t("tpl.replace_title", { name: replace.name }) : t("tpl.add_custom")} description={t("tpl.custom_desc")}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={go.isPending} disabled={src === "file" ? !file : !/^https?:\/\//.test(url)} onClick={() => go.mutate()}>{src === "file" ? <Upload /> : <CloudDownload />} {src === "file" ? t("tpl.upload") : t("tpl.download")}</Button></>}>
      <div className="space-y-4">
        <div className="inline-flex rounded-md border border-line bg-sunken p-0.5">
          {(["file", "url"] as const).map((v) => <button key={v} type="button" onClick={() => setSrc(v)} className={cn("cursor-pointer rounded-[8px] px-3 py-1.5 text-[13px] font-semibold", src === v ? "bg-raised shadow-sm" : "text-muted")}>{t(`tpl.src_${v}`)}</button>)}
        </div>
        {src === "file" ? (
          <div onClick={() => inputRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) { setFile(f); if (!name) setName(f.name.replace(/\.tar\.(gz|xz|zst)$/, "")); } }}
            className="cursor-pointer rounded-md border-2 border-dashed border-line px-4 py-8 text-center text-sm hover:bg-sunken">
            <Upload className="mx-auto mb-2 size-6 text-muted" />
            {file ? <><span className="mono font-semibold">{file.name}</span> <span className="text-muted">({formatBytes(file.size)})</span></> : t("tpl.drop")}
            <input ref={inputRef} type="file" accept=".tar.gz,.tar.xz,.tar.zst,.gz,.xz,.zst" className="hidden" onChange={(e) => { const f = e.target.files?.[0] ?? null; setFile(f); if (f && !name) setName(f.name.replace(/\.tar\.(gz|xz|zst)$/, "")); }} />
          </div>
        ) : (
          <Field label="URL" hint={t("tpl.url_hint")}><Input className="mono" value={url} onChange={(e) => setUrl(e.target.value.trim())} placeholder="https://…/my-template.tar.zst" /></Field>
        )}
        {progress !== null && (
          <div className="h-2 overflow-hidden rounded-full bg-sunken"><div className="h-full bg-online transition-all" style={{ width: `${Math.round(progress * 100)}%` }} /></div>
        )}
        <Field label={t("px.name")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label={t("gx.description")}><Textarea rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
      </div>
    </Dialog>
  );
}

function EditDialog({ tpl, onClose }: { tpl: CtTemplate; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [f, setF] = useState({ name: tpl.name, description: tpl.description ?? "", sync: tpl.sync, storage: tpl.storage ?? "", follow_latest: tpl.follow_latest });
  const save = useMutation({
    mutationFn: () => patch(`api/templates/${tpl.id}`, f),
    onSuccess: () => { toast.success(t("common.saved")); qc.invalidateQueries({ queryKey: ["templates"] }); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={tpl.name} description={<span className="mono">{tpl.filename}</span>}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("px.name")}><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label={t("gx.description")}><Textarea rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <Field label={t("tpl.storage")} hint={t("tpl.storage_override_hint")}><Input className="mono" value={f.storage} onChange={(e) => setF({ ...f, storage: e.target.value.trim() })} placeholder={t("tpl.storage_default")} /></Field>
        <Switch checked={f.sync} onCheckedChange={(v) => setF({ ...f, sync: v })} label={t("tpl.sync_on")} description={t("tpl.sync_on_hint")} />
        {tpl.kind === "official" && <Switch checked={f.follow_latest} onCheckedChange={(v) => setF({ ...f, follow_latest: v })} label={t("tpl.follow_latest")} description={t("tpl.follow_latest_hint")} />}
      </div>
    </Dialog>
  );
}

export function TemplatesPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const qc = useQueryClient();
  const navigate = useNavigate();
  const lib = useQuery({ queryKey: ["templates"], queryFn: () => get<TemplateLibrary>("api/templates"), refetchInterval: 30_000 });
  const [official, setOfficial] = useState(false);
  const [custom, setCustom] = useState(false);
  const [replace, setReplace] = useState<CtTemplate | null>(null);
  const [edit, setEdit] = useState<CtTemplate | null>(null);
  const [remove, setRemove] = useState<CtTemplate | null>(null);
  const [settings, setSettings] = useState(false);
  const [storage, setStorage] = useState("");
  const toRun = (r: { run_id: string }) => { toast.success(t("tpl.sync_started")); navigate(`/tasks/runs/${r.run_id}`); };
  const sync = useMutation({ mutationFn: () => post<{ run_id: string }>("api/templates/sync"), onSuccess: toRun, onError: (e) => toast.error(errorMessage(e, t)) });
  const push = useMutation({ mutationFn: (id: string) => post<{ run_id: string }>(`api/templates/${id}/push`, {}), onSuccess: toRun, onError: (e) => toast.error(errorMessage(e, t)) });
  const scan = useMutation({ mutationFn: () => post("api/templates/scan"), onSuccess: () => qc.invalidateQueries({ queryKey: ["templates"] }) });
  const rm = useMutation({ mutationFn: (id: string) => del(`api/templates/${id}`), onSuccess: () => { setRemove(null); qc.invalidateQueries({ queryKey: ["templates"] }); }, onError: (e) => toast.error(errorMessage(e, t)) });
  const saveSettings = useMutation({
    mutationFn: () => put("api/templates-settings", { storage }),
    onSuccess: () => { setSettings(false); toast.success(t("common.saved")); qc.invalidateQueries({ queryKey: ["templates"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  useEffect(() => {
    const h = (e: Event) => toast.error(t("tpl.download_failed"), { description: String((e as CustomEvent).detail) });
    window.addEventListener("mmn:template-error", h);
    return () => window.removeEventListener("mmn:template-error", h);
  }, [t]);
  const data = lib.data;
  const families = useMemo(() => new Set((data?.templates ?? []).map((x) => x.family)), [data]);
  const totals = useMemo(() => {
    const s = { current: 0, outdated: 0, missing: 0, unknown: 0 };
    for (const x of data?.templates ?? []) for (const k of Object.keys(s) as (keyof typeof s)[]) s[k] += x.summary?.[k] ?? 0;
    return s;
  }, [data]);
  return (
    <>
      <PageHeader title={t("tpl.title")} description={t("tpl.desc")}
        actions={admin && <>
          <Button onClick={() => { setStorage(data?.settings.storage ?? "local"); setSettings(true); }}><Settings2 /> {t("tpl.settings")}</Button>
          <Button onClick={() => scan.mutate()} loading={scan.isPending}>{!scan.isPending && <RefreshCw />} {t("tpl.rescan")}</Button>
          <Button variant="dark" onClick={() => sync.mutate()} loading={sync.isPending}>{!sync.isPending && <Send />} {t("tpl.sync_all")}</Button>
        </>} />
      <div className="mb-5 grid gap-3 sm:grid-cols-4">
        {(["current", "outdated", "missing", "unknown"] as const).map((k) => (
          <div key={k} className="rounded-lg border border-line bg-raised p-4">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t(`tpl.status_${k}`)}</div>
            <div className={cn("display mt-1 text-2xl", k === "missing" && totals[k] ? "text-offline" : k === "outdated" && totals[k] ? "text-warning" : "")}>{totals[k]}</div>
          </div>
        ))}
      </div>
      <Card>
        <CardHeader icon={<Layers />} title={t("tpl.library")} description={t("tpl.library_desc", { storage: data?.settings.storage ?? "local" })}
          action={admin && <>
            <Button size="sm" onClick={() => setCustom(true)}><Upload /> {t("tpl.add_custom")}</Button>
            <Button size="sm" variant="primary" onClick={() => setOfficial(true)}><Plus /> {t("tpl.add_official")}</Button>
          </>} />
        {lib.isLoading ? <CardBody><Spinner /></CardBody> : !data?.templates.length ? (
          <EmptyState icon={<Layers />} title={t("tpl.empty")} action={admin && <Button variant="primary" onClick={() => setOfficial(true)}><Plus /> {t("tpl.add_official")}</Button>}>{t("tpl.empty_hint")}</EmptyState>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("tpl.template")}</Th>
                {data.nodes.map((n) => <Th key={n.node_id} className="whitespace-nowrap" title={n.checked_at ? `${t("tpl.scanned")} ${timeAgo(n.checked_at, i18n.language)}` : undefined}>{n.node}{!n.online && " ·"}</Th>)}
                {admin && <Th />}
              </tr>
            </thead>
            <tbody>
              {data.templates.map((x) => (
                <Tr key={x.id}>
                  <Td>
                    <div className="flex items-center gap-2 font-semibold">{x.name} <Badge tone={x.kind === "official" ? "info" : "accent"}>{t(`tpl.kind_${x.kind}`)}</Badge>{!x.sync && <Badge>{t("tpl.no_sync")}</Badge>}</div>
                    <div className="mono text-[11.5px] text-muted">{x.filename}{x.size ? ` · ${formatBytes(x.size)}` : ""}</div>
                    {x.description && <div className="text-xs text-muted">{x.description}</div>}
                  </Td>
                  {data.nodes.map((n) => <Td key={n.node_id}><LocationCell loc={x.locations?.find((l) => l.node_id === n.node_id)} /></Td>)}
                  {admin && (
                    <Td className="whitespace-nowrap text-right">
                      <Button size="sm" variant="ghost" title={t("tpl.push")} loading={push.isPending && push.variables === x.id} onClick={() => push.mutate(x.id)}>{!(push.isPending && push.variables === x.id) && <Send />}</Button>
                      {x.kind === "custom" && <Button size="sm" variant="ghost" title={t("tpl.replace")} onClick={() => setReplace(x)}><Upload /></Button>}
                      <Button size="sm" variant="ghost" title={t("common.edit")} onClick={() => setEdit(x)}><Pencil /></Button>
                      <Button size="sm" variant="ghost" title={t("common.delete")} onClick={() => setRemove(x)}><Trash2 /></Button>
                    </Td>
                  )}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <p className="mt-3 flex items-center gap-1.5 text-xs text-muted"><Link2 className="size-3.5" /> {t("tpl.nightly_hint")}</p>
      {admin && <AddOfficialDialog open={official} onOpenChange={setOfficial} existing={families} />}
      {admin && custom && <CustomDialog open={custom} onOpenChange={setCustom} />}
      {admin && replace && <CustomDialog open onOpenChange={(v) => !v && setReplace(null)} replace={replace} />}
      {edit && <EditDialog tpl={edit} onClose={() => setEdit(null)} />}
      <Dialog open={!!remove} onOpenChange={(v) => !v && setRemove(null)} title={t("tpl.remove_title", { name: remove?.name })}
        footer={<><Button onClick={() => setRemove(null)}>{t("common.cancel")}</Button><Button variant="danger" loading={rm.isPending} onClick={() => remove && rm.mutate(remove.id)}>{t("common.delete")}</Button></>}>
        <Alert tone="info">{t("tpl.remove_text")}</Alert>
      </Dialog>
      <Dialog open={settings} onOpenChange={setSettings} title={t("tpl.settings")}
        footer={<><Button onClick={() => setSettings(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={saveSettings.isPending} onClick={() => saveSettings.mutate()}>{t("common.save")}</Button></>}>
        <div className="space-y-3">
          <Field label={t("tpl.storage")} hint={t("tpl.storage_hint")}><Input className="mono" value={storage} onChange={(e) => setStorage(e.target.value.trim())} /></Field>
          {data && <p className="text-xs text-muted">{data.nodes.map((n) => `${n.node}: ${n.storages.join(", ") || "–"}`).join(" · ")}</p>}
        </div>
      </Dialog>
    </>
  );
}
