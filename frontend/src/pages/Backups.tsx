import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, DatabaseBackup, FolderSync, History, KeyRound, Lock, Pencil, Play, Plug, Plus, RotateCcw, Trash2, XCircle } from "lucide-react";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import type { BackupRun, BackupTarget, Protocol, RemoteBackup } from "@/lib/types";
import { formatBytes, formatDate, timeAgo } from "@/lib/utils";

const PORTS: Record<Protocol, number> = { ftp: 21, ftps: 21, sftp: 22, scp: 22 };

interface TargetForm {
  name: string;
  protocol: Protocol;
  host: string;
  port: string;
  username: string;
  password: string;
  private_key: string;
  key_passphrase: string;
  encryption_passphrase: string;
  remote_path: string;
  ftps_implicit: boolean;
  verify_tls: boolean;
  passive: boolean;
  enabled: boolean;
  full_cron: string;
  incremental_cron: string;
  keep_full: number;
  auth: "password" | "key";
}

const emptyForm: TargetForm = {
  name: "", protocol: "sftp", host: "", port: "", username: "", password: "", private_key: "", key_passphrase: "",
  encryption_passphrase: "", remote_path: "mcp-management-node", ftps_implicit: false, verify_tls: true, passive: true,
  enabled: true, full_cron: "0 3 * * 0", incremental_cron: "0 3 * * 1-6", keep_full: 4, auth: "password",
};

const SCHEDULES = [
  { label: "daily", full: "0 3 * * 0", inc: "0 3 * * 1-6" },
  { label: "hourly", full: "0 3 * * *", inc: "0 * * * *" },
  { label: "weekly", full: "0 3 1 * *", inc: "0 3 * * 0" },
];

export function BackupsPage() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const targets = useQuery({ queryKey: ["backup-targets"], queryFn: () => get<BackupTarget[]>("api/backups/targets") });
  const runs = useQuery({
    queryKey: ["backup-runs"],
    queryFn: () => get<BackupRun[]>("api/backups/runs?limit=50"),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "running") ? 2000 : false),
  });
  const [editing, setEditing] = useState<BackupTarget | "new" | null>(null);
  const [form, setForm] = useState<TargetForm>(emptyForm);
  const [restoreTarget, setRestoreTarget] = useState<BackupTarget | null>(null);
  const [openLog, setOpenLog] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  const openForm = (tg: BackupTarget | "new") => {
    setEditing(tg);
    setForm(tg === "new" ? emptyForm : {
      ...emptyForm, name: tg.name, protocol: tg.protocol, host: tg.host, port: String(tg.port), username: tg.username,
      remote_path: tg.remote_path, ftps_implicit: tg.ftps_implicit, verify_tls: tg.verify_tls, passive: tg.passive,
      enabled: tg.enabled, full_cron: tg.full_cron ?? "", incremental_cron: tg.incremental_cron ?? "", keep_full: tg.keep_full,
      auth: tg.private_key_set ? "key" : "password",
    });
  };

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: form.name, protocol: form.protocol, host: form.host, port: form.port ? Number(form.port) : null,
        username: form.username, remote_path: form.remote_path, ftps_implicit: form.ftps_implicit, verify_tls: form.verify_tls,
        passive: form.passive, enabled: form.enabled, full_cron: form.full_cron || null, incremental_cron: form.incremental_cron || null,
        keep_full: form.keep_full,
      };
      if (form.password) body.password = form.password;
      if (form.auth === "key" && form.private_key) body.private_key = form.private_key;
      if (form.auth === "password" && editing !== "new" && (editing as BackupTarget).private_key_set) body.private_key = "";
      if (form.key_passphrase) body.key_passphrase = form.key_passphrase;
      if (form.encryption_passphrase) body.encryption_passphrase = form.encryption_passphrase;
      return editing === "new" ? post("api/backups/targets", body) : put(`api/backups/targets/${(editing as BackupTarget).id}`, body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backup-targets"] });
      setEditing(null);
      toast.success(t("common.saved"));
    },
  });

  const test = async (tg: BackupTarget) => {
    setTesting(tg.id);
    try {
      const r = await post<{ ok: boolean; error?: string; fingerprint?: string; tls?: boolean }>(`api/backups/targets/${tg.id}/test`);
      if (r.ok) toast.success(t("backups.test_ok"), { description: r.fingerprint ? `${t("backups.host_key")}: ${r.fingerprint}` : r.tls ? "TLS" : undefined });
      else toast.error(t("backups.test_failed"), { description: r.error });
      qc.invalidateQueries({ queryKey: ["backup-targets"] });
    } catch (e) {
      toast.error(errorMessage(e, t));
    } finally {
      setTesting(null);
    }
  };
  const run = useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: string }) => post(`api/backups/targets/${id}/run`, { kind }),
    onSuccess: () => {
      toast.success(t("backups.started"));
      setTimeout(() => qc.invalidateQueries({ queryKey: ["backup-runs"] }), 400);
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: (id: string) => del(`api/backups/targets/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backup-targets"] }),
  });

  const lastRun = (id: string) => runs.data?.find((r) => r.target_id === id && r.kind !== "restore");
  const isSsh = form.protocol === "sftp" || form.protocol === "scp";

  return (
    <>
      <PageHeader title={t("backups.title")} description={t("backups.subtitle")} actions={<Button variant="primary" onClick={() => openForm("new")}><Plus /> {t("backups.add_target")}</Button>} />

      {targets.isLoading ? <Spinner /> : !targets.data?.length ? (
        <Card><EmptyState icon={<DatabaseBackup />} title={t("backups.empty_title")} action={<Button variant="primary" onClick={() => openForm("new")}><Plus /> {t("backups.add_target")}</Button>}>{t("backups.empty_text")}</EmptyState></Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {targets.data.map((tg) => {
            const last = lastRun(tg.id);
            return (
              <Card key={tg.id}>
                <CardHeader
                  icon={<FolderSync />}
                  title={<span className="flex items-center gap-2">{tg.name} {!tg.enabled && <Badge>{t("backups.paused")}</Badge>}</span>}
                  description={<span className="mono text-xs">{tg.protocol}://{tg.username}@{tg.host}:{tg.port}/{tg.remote_path.replace(/^\//, "")}</span>}
                  action={<>
                    <Button size="icon" variant="ghost" onClick={() => openForm(tg)} aria-label={t("common.edit")}><Pencil /></Button>
                    <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => { if (window.confirm(t("backups.delete_confirm", { name: tg.name }))) remove.mutate(tg.id); }}><Trash2 className="text-offline" /></Button>
                  </>}
                />
                <CardBody className="space-y-4">
                  <div className="flex flex-wrap gap-1.5">
                    <Badge tone="info">{tg.protocol.toUpperCase()}</Badge>
                    {tg.encrypted ? <Badge tone="online"><Lock className="size-3" /> {t("backups.encrypted")}</Badge> : <Badge tone="warning">{t("backups.not_encrypted")}</Badge>}
                    {tg.host_key_fingerprint && <Badge><KeyRound className="size-3" /> {t("backups.host_key_pinned")}</Badge>}
                    <Badge>{t("backups.keep", { count: tg.keep_full })}</Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-sm">
                    <div><div className="text-xs font-semibold text-muted">{t("backups.full")}</div><div className="mono text-[13px]">{tg.full_cron || "–"}</div><div className="text-xs text-muted">{tg.next_runs?.full ? `${t("backups.next")}: ${formatDate(tg.next_runs.full, i18n.language)}` : ""}</div></div>
                    <div><div className="text-xs font-semibold text-muted">{t("backups.incremental")}</div><div className="mono text-[13px]">{tg.incremental_cron || "–"}</div><div className="text-xs text-muted">{tg.next_runs?.incremental ? `${t("backups.next")}: ${formatDate(tg.next_runs.incremental, i18n.language)}` : ""}</div></div>
                  </div>
                  {last && (
                    <div className="flex items-center gap-2 text-sm">
                      {last.status === "success" ? <CheckCircle2 className="size-4 text-online" /> : last.status === "failed" ? <XCircle className="size-4 text-offline" /> : <Spinner className="size-4" />}
                      <span>{t(`backups.kind_${last.kind}`)} · {timeAgo(last.started_at, i18n.language)}</span>
                      {last.error && <span className="truncate text-offline" title={last.error}>{last.error}</span>}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => test(tg)} loading={testing === tg.id}><Plug /> {t("backups.test")}</Button>
                    <Button size="sm" onClick={() => run.mutate({ id: tg.id, kind: "full" })}><Play /> {t("backups.run_full")}</Button>
                    <Button size="sm" onClick={() => run.mutate({ id: tg.id, kind: "incremental" })}><Play /> {t("backups.run_incremental")}</Button>
                    <Button size="sm" onClick={() => setRestoreTarget(tg)}><RotateCcw /> {t("backups.restore")}</Button>
                  </div>
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      <Card className="mt-6">
        <CardHeader icon={<History />} title={t("backups.history")} description={t("backups.history_hint")} />
        <Table>
          <thead><tr><Th>{t("backups.when")}</Th><Th>{t("backups.type")}</Th><Th>{t("backups.target")}</Th><Th>{t("backups.files")}</Th><Th>{t("backups.size")}</Th><Th>{t("backups.status")}</Th></tr></thead>
          <tbody>
            {(runs.data ?? []).map((r) => (
              <Fragment key={r.id}>
                <Tr className="cursor-pointer" onClick={() => setOpenLog(openLog === r.id ? null : r.id)}>
                  <Td className="whitespace-nowrap">{formatDate(r.started_at, i18n.language)}</Td>
                  <Td><Badge tone={r.kind === "full" ? "info" : r.kind === "restore" ? "accent" : "neutral"}>{t(`backups.kind_${r.kind}`)}</Badge> <span className="text-xs text-muted">{t(`backups.trigger_${r.trigger}`, { defaultValue: r.trigger })}</span></Td>
                  <Td>{r.target_name ?? "–"}</Td>
                  <Td className="text-muted">{r.files_changed ?? "–"}</Td>
                  <Td className="text-muted">{formatBytes(r.size_bytes)}</Td>
                  <Td>{r.status === "success" ? <Badge tone="online" dot>{t("common.success")}</Badge> : r.status === "failed" ? <Badge tone="offline" dot>{t("common.failed")}</Badge> : <Badge tone="warning" dot>{t("common.running")}</Badge>}</Td>
                </Tr>
                {openLog === r.id && (
                  <tr><td colSpan={6} className="border-b border-line bg-sunken px-4 py-3">
                    <div className="mono text-xs text-muted">{r.backup_id}</div>
                    {r.error && <Alert tone="offline" className="my-2">{r.error}</Alert>}
                    <ul className="mt-1 space-y-0.5 text-xs">{r.log.map((l, i) => <li key={i}><span className="text-muted">{new Date(l.at).toLocaleTimeString()}</span> {l.msg}</li>)}</ul>
                  </td></tr>
                )}
              </Fragment>
            ))}
            {!runs.data?.length && <tr><td colSpan={6} className="px-4 py-6 text-sm text-muted">{t("backups.no_runs")}</td></tr>}
          </tbody>
        </Table>
      </Card>

      <Dialog
        open={editing !== null}
        onOpenChange={(v) => !v && setEditing(null)}
        wide
        title={editing === "new" ? t("backups.add_target") : t("backups.edit_target")}
        footer={<><Button variant="ghost" onClick={() => setEditing(null)}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}
      >
        <div className="space-y-5">
          {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label={t("backups.name")} className="sm:col-span-2"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="NAS thuis" /></Field>
            <Field label={t("backups.protocol")}>
              <Select value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value as Protocol, port: "" })}>
                <option value="sftp">SFTP (SSH)</option>
                <option value="scp">SCP (SSH)</option>
                <option value="ftps">FTPS (FTP + TLS)</option>
                <option value="ftp">FTP</option>
              </Select>
            </Field>
            <Field label={t("backups.host")} className="sm:col-span-2"><Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="backup.example.com" /></Field>
            <Field label={t("backups.port")}><Input inputMode="numeric" value={form.port} placeholder={String(PORTS[form.protocol])} onChange={(e) => setForm({ ...form, port: e.target.value.replace(/\D/g, "") })} /></Field>
            <Field label={t("auth.username")}><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>
            <Field label={t("backups.remote_path")} className="sm:col-span-2"><Input className="mono" value={form.remote_path} onChange={(e) => setForm({ ...form, remote_path: e.target.value })} /></Field>
          </div>
          {form.protocol === "ftp" && <Alert tone="warning">{t("backups.ftp_warning")}</Alert>}

          {isSsh && (
            <Field label={t("backups.auth_method")}>
              <Select value={form.auth} onChange={(e) => setForm({ ...form, auth: e.target.value as TargetForm["auth"] })}>
                <option value="password">{t("auth.password")}</option>
                <option value="key">{t("backups.private_key")}</option>
              </Select>
            </Field>
          )}
          {(!isSsh || form.auth === "password") && (
            <Field label={t("auth.password")} hint={editing !== "new" && (editing as BackupTarget)?.password_set ? t("common.secret_keep") : undefined}>
              <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </Field>
          )}
          {isSsh && form.auth === "key" && (
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label={t("backups.private_key")} className="sm:col-span-2" hint={editing !== "new" && (editing as BackupTarget)?.private_key_set ? t("common.secret_keep") : t("backups.private_key_hint")}>
                <Textarea rows={5} value={form.private_key} onChange={(e) => setForm({ ...form, private_key: e.target.value })} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
              </Field>
              <Field label={t("backups.key_passphrase")}><Input type="password" value={form.key_passphrase} onChange={(e) => setForm({ ...form, key_passphrase: e.target.value })} /></Field>
            </div>
          )}
          {form.protocol === "ftps" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <Switch checked={form.ftps_implicit} onCheckedChange={(v) => setForm({ ...form, ftps_implicit: v })} label={t("backups.ftps_implicit")} description={t("backups.ftps_implicit_hint")} />
              <Switch checked={form.verify_tls} onCheckedChange={(v) => setForm({ ...form, verify_tls: v })} label={t("backups.verify_tls")} description={t("backups.verify_tls_hint")} />
            </div>
          )}
          {(form.protocol === "ftp" || form.protocol === "ftps") && (
            <Switch checked={form.passive} onCheckedChange={(v) => setForm({ ...form, passive: v })} label={t("backups.passive")} />
          )}

          <div className="rounded-md border border-line p-4">
            <div className="mb-1 flex items-center gap-2 font-semibold"><Lock className="size-4" /> {t("backups.encryption")}</div>
            <p className="mb-3 text-xs text-muted">{t("backups.encryption_hint")}</p>
            <Input type="password" autoComplete="new-password" value={form.encryption_passphrase} onChange={(e) => setForm({ ...form, encryption_passphrase: e.target.value })} placeholder={editing !== "new" && (editing as BackupTarget)?.encrypted ? t("common.secret_keep") : t("backups.encryption_placeholder")} />
          </div>

          <div className="rounded-md border border-line p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold">{t("backups.schedule")}</div>
              <div className="flex gap-1">{SCHEDULES.map((s) => <Button key={s.label} size="sm" variant="ghost" onClick={() => setForm({ ...form, full_cron: s.full, incremental_cron: s.inc })}>{t(`backups.preset_${s.label}`)}</Button>)}</div>
            </div>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label={t("backups.full_cron")} hint={t("backups.cron_hint")}><Input className="mono" value={form.full_cron} onChange={(e) => setForm({ ...form, full_cron: e.target.value })} /></Field>
              <Field label={t("backups.incremental_cron")}><Input className="mono" value={form.incremental_cron} onChange={(e) => setForm({ ...form, incremental_cron: e.target.value })} /></Field>
              <Field label={t("backups.keep_full")} hint={t("backups.keep_full_hint")}><Input type="number" min={1} max={100} value={form.keep_full} onChange={(e) => setForm({ ...form, keep_full: Number(e.target.value) || 1 })} /></Field>
            </div>
            <div className="mt-3"><Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} label={t("backups.enabled")} /></div>
          </div>
        </div>
      </Dialog>

      <RestoreDialog target={restoreTarget} onClose={() => setRestoreTarget(null)} />
    </>
  );
}

function RestoreDialog({ target, onClose }: { target: BackupTarget | null; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [chosen, setChosen] = useState<RemoteBackup | null>(null);
  const [password, setPassword] = useState("");
  const remote = useQuery({ queryKey: ["remote", target?.id], queryFn: () => get<RemoteBackup[]>(`api/backups/targets/${target!.id}/remote`), enabled: !!target, retry: false });
  const restore = useMutation({
    mutationFn: () => post<{ restored: string; chain: string[] }>(`api/backups/targets/${target!.id}/restore`, { backup_id: chosen!.id, password }),
    onSuccess: (r) => {
      toast.success(t("backups.restore_done"), { description: r.chain.join(" → ") });
      close();
      qc.invalidateQueries();
      setTimeout(() => window.location.reload(), 1500);
    },
  });
  const close = () => {
    setChosen(null);
    setPassword("");
    restore.reset();
    onClose();
  };
  return (
    <Dialog
      open={!!target}
      onOpenChange={(v) => !v && close()}
      wide
      title={t("backups.restore_title", { name: target?.name })}
      description={t("backups.restore_subtitle")}
      footer={chosen ? <><Button variant="ghost" onClick={() => setChosen(null)}>{t("common.back")}</Button><Button variant="danger" disabled={!password} loading={restore.isPending} onClick={() => restore.mutate()}><RotateCcw /> {t("backups.restore_now")}</Button></> : undefined}
    >
      {remote.isLoading ? <Spinner /> : remote.isError ? <Alert tone="offline">{errorMessage(remote.error, t)}</Alert> : chosen ? (
        <div className="space-y-4">
          <Alert tone="warning" title={t("backups.restore_warning_title")}>{t("backups.restore_warning", { date: formatDate(chosen.created_at, i18n.language) })}</Alert>
          {restore.isError && <Alert tone="offline">{errorMessage(restore.error, t)}</Alert>}
          <Field label={t("backups.confirm_password")}><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus /></Field>
        </div>
      ) : !remote.data?.length ? (
        <EmptyState icon={<DatabaseBackup />} title={t("backups.remote_empty")} />
      ) : (
        <Table>
          <thead><tr><Th>{t("backups.when")}</Th><Th>{t("backups.type")}</Th><Th>{t("info.version")}</Th><Th>{t("backups.size")}</Th><Th /></tr></thead>
          <tbody>
            {remote.data.map((b) => (
              <Tr key={b.id}>
                <Td className="whitespace-nowrap">{formatDate(b.created_at, i18n.language)}</Td>
                <Td><Badge tone={b.kind === "full" ? "info" : "neutral"}>{t(`backups.kind_${b.kind}`)}</Badge> {b.encrypted && <Lock className="inline size-3.5 text-muted" />}</Td>
                <Td className="mono text-xs">{b.app_version}</Td>
                <Td className="text-muted">{formatBytes(b.size)}</Td>
                <Td className="text-right">{b.restorable ? <Button size="sm" onClick={() => setChosen(b)}>{t("backups.choose")}</Button> : <Badge tone="offline">{t("backups.chain_incomplete")}</Badge>}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Dialog>
  );
}
