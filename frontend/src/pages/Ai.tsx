import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, KeyRound, Link2, Plus, ScrollText, Search, Trash2, Unplug } from "lucide-react";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { CodeBlock, CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post } from "@/lib/api";
import type { ApiToken, AuditEntry, ClientInfo, Connection } from "@/lib/types";
import { appUrl, formatDate, timeAgo } from "@/lib/utils";

export function clientLabel(c?: ClientInfo, fallback?: string): string {
  if (c?.name) return c.version ? `${c.name} ${c.version}` : c.name;
  const ua = c?.user_agent ?? "";
  if (/claude/i.test(ua)) return "Claude";
  if (/openai|chatgpt/i.test(ua)) return "ChatGPT";
  if (/copilot|vscode/i.test(ua)) return "Copilot (VS Code)";
  return fallback ?? "–";
}

function ScopeBadges({ scopes }: { scopes: string[] }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap gap-1">
      {scopes.map((s) => (
        <Badge key={s} tone={s === "restore" || s === "updates" ? "warning" : s === "read" ? "neutral" : "info"}>{t(`scopes.${s}`)}</Badge>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ connections */
export function ConnectionsPage() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const conns = useQuery({ queryKey: ["connections"], queryFn: () => get<Connection[]>("api/tokens/connections") });
  const disconnect = useMutation({
    mutationFn: (id: string) => del(`api/tokens/connections/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connections"] }),
  });
  const url = appUrl("/mcp");
  return (
    <>
      <PageHeader title={t("ai.connections_title")} description={t("ai.connections_subtitle")} actions={<Link to="/help/mcp"><Button variant="primary"><Link2 /> {t("ai.connect_new")}</Button></Link>} />
      <Card className="mb-6">
        <CardBody className="flex flex-wrap items-center justify-between gap-4 py-5">
          <div>
            <div className="text-[13px] font-semibold text-muted">{t("ai.endpoint")}</div>
            <div className="mono mt-1 text-[15px]">{url}</div>
          </div>
          <CopyButton value={url} />
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<Bot />} title={t("ai.connected_apps")} description={t("ai.connected_apps_hint")} />
        {conns.isLoading ? <CardBody><Spinner /></CardBody> : !conns.data?.length ? (
          <EmptyState icon={<Bot />} title={t("ai.no_connections")} action={<Link to="/help/mcp"><Button>{t("ai.how_to_connect")}</Button></Link>}>{t("ai.no_connections_hint")}</EmptyState>
        ) : (
          <div className="divide-y divide-line">
            {conns.data.map((c) => (
              <div key={c.client_id} className="flex flex-wrap items-center gap-4 px-5 py-4">
                <div className="grid size-10 place-items-center rounded-md bg-nacht text-koraal"><Bot className="size-5" /></div>
                <div className="min-w-0 flex-1">
                  <div className="font-bold">{c.client_name}</div>
                  <div className="text-xs text-muted">
                    {clientLabel(c.last_client, "")} · {t("ai.connected")} {timeAgo(c.connected_at, i18n.language)} · {t("ai.last_used")} {timeAgo(c.last_used_at, i18n.language)}
                  </div>
                  <div className="mt-2"><ScopeBadges scopes={c.scopes} /></div>
                </div>
                <Button size="sm" variant="secondary" loading={disconnect.isPending && disconnect.variables === c.client_id} onClick={() => { if (window.confirm(t("ai.disconnect_confirm", { name: c.client_name }))) disconnect.mutate(c.client_id); }}>
                  <Unplug /> {t("ai.disconnect")}
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

/* ------------------------------------------------------------------ tokens */
export function TokensPage() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const tokens = useQuery({ queryKey: ["tokens"], queryFn: () => get<ApiToken[]>("api/tokens") });
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: () => get<{ scope: string; description: string }[]>("api/tokens/scopes") });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", scopes: ["read"], expires_days: "90" });
  const [created, setCreated] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => post<ApiToken & { token: string }>("api/tokens", { name: form.name, scopes: form.scopes, expires_days: form.expires_days === "never" ? null : Number(form.expires_days) }),
    onSuccess: (r) => {
      setCreated(r.token);
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => del(`api/tokens/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens"] }) });
  const close = () => {
    setOpen(false);
    setCreated(null);
    create.reset();
    setForm({ name: "", scopes: ["read"], expires_days: "90" });
  };
  return (
    <>
      <PageHeader title={t("ai.tokens_title")} description={t("ai.tokens_subtitle")} actions={<Button variant="primary" onClick={() => setOpen(true)}><Plus /> {t("ai.new_token")}</Button>} />
      <Card>
        <CardHeader icon={<KeyRound />} title={t("ai.your_tokens")} />
        {!tokens.data?.length ? <EmptyState icon={<KeyRound />} title={t("ai.no_tokens")}>{t("ai.no_tokens_hint")}</EmptyState> : (
          <Table>
            <thead><tr><Th>{t("ai.token_name")}</Th><Th>{t("ai.scopes")}</Th><Th>{t("ai.used_by")}</Th><Th>{t("ai.expires")}</Th><Th /></tr></thead>
            <tbody>
              {tokens.data.map((tk) => (
                <Tr key={tk.id}>
                  <Td><div className="font-semibold">{tk.name}</div><div className="mono text-xs text-muted">{tk.prefix}…</div></Td>
                  <Td><ScopeBadges scopes={tk.scopes} /></Td>
                  <Td className="text-sm"><div>{clientLabel(tk.last_client)}</div><div className="text-xs text-muted">{timeAgo(tk.last_used_at, i18n.language)}</div></Td>
                  <Td className="text-muted">{tk.expires_at ? formatDate(tk.expires_at, i18n.language) : t("ai.never")}</Td>
                  <Td className="text-right"><Button size="icon" variant="ghost" aria-label={t("ai.revoke")} onClick={() => { if (window.confirm(t("ai.revoke_confirm", { name: tk.name }))) revoke.mutate(tk.id); }}><Trash2 className="text-offline" /></Button></Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Dialog open={open} onOpenChange={(v) => !v && close()} title={created ? t("ai.token_created") : t("ai.new_token")}
        footer={created ? <Button variant="primary" onClick={close}>{t("common.done")}</Button> : <><Button variant="ghost" onClick={close}>{t("common.cancel")}</Button><Button variant="primary" disabled={!form.name} loading={create.isPending} onClick={() => create.mutate()}>{t("ai.create")}</Button></>}>
        {created ? (
          <div className="space-y-4">
            <Alert tone="warning">{t("ai.token_once")}</Alert>
            <CodeBlock code={created} />
            <p className="text-sm text-muted">{t("ai.token_next")} <Link to="/help/mcp" className="font-semibold text-accent-text hover:underline" onClick={close}>{t("nav.help")}</Link></p>
          </div>
        ) : (
          <div className="space-y-4">
            {create.isError && <Alert tone="offline">{errorMessage(create.error, t)}</Alert>}
            <Field label={t("ai.token_name")} hint={t("ai.token_name_hint")}><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Claude Code laptop" autoFocus /></Field>
            <div>
              <div className="mb-2 text-[13px] font-semibold">{t("ai.scopes")}</div>
              <div className="space-y-3 rounded-md border border-line p-4">
                {(scopes.data ?? []).map((s) => (
                  <Checkbox key={s.scope} disabled={s.scope === "read"} checked={form.scopes.includes(s.scope)}
                    onCheckedChange={(v) => setForm({ ...form, scopes: v ? [...form.scopes, s.scope] : form.scopes.filter((x) => x !== s.scope) })}
                    label={t(`scopes.${s.scope}`)} description={t(`scopes.${s.scope}_desc`)} />
                ))}
              </div>
            </div>
            <Field label={t("ai.expires")}>
              <Select value={form.expires_days} onChange={(e) => setForm({ ...form, expires_days: e.target.value })}>
                <option value="30">30 {t("ai.days")}</option>
                <option value="90">90 {t("ai.days")}</option>
                <option value="365">365 {t("ai.days")}</option>
                <option value="never">{t("ai.never")}</option>
              </Select>
            </Field>
          </div>
        )}
      </Dialog>
    </>
  );
}

/* ------------------------------------------------------------------ activity / audit */
function AuditTable({ source }: { source?: string }) {
  const { t, i18n } = useTranslation();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<"all" | "ok" | "failed">("all");
  const [open, setOpen] = useState<number | null>(null);
  const params = new URLSearchParams({ limit: "200" });
  if (source) params.set("source", source);
  if (q) params.set("q", q);
  if (status !== "all") params.set("success", String(status === "ok"));
  const log = useQuery({ queryKey: ["audit", source, q, status], queryFn: () => get<AuditEntry[]>(`api/settings/audit?${params}`), refetchInterval: 15000 });
  const mcp = source === "mcp";
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
        <div className="relative min-w-60 flex-1">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("audit.search")} />
        </div>
        <Segmented value={status} onChange={setStatus} options={[{ value: "all", label: t("audit.all") }, { value: "ok", label: t("common.success") }, { value: "failed", label: t("common.failed") }]} />
      </div>
      {log.isLoading ? <CardBody><Spinner /></CardBody> : !log.data?.length ? <EmptyState icon={<ScrollText />} title={t("audit.empty")}>{mcp ? t("ai.activity_empty_hint") : undefined}</EmptyState> : (
        <Table>
          <thead><tr><Th>{t("audit.when")}</Th>{mcp ? <><Th>{t("ai.ai_client")}</Th><Th>{t("ai.tool")}</Th></> : <><Th>{t("audit.source")}</Th><Th>{t("audit.action")}</Th></>}<Th>{t("audit.user")}</Th><Th>{t("audit.target")}</Th><Th>{t("backups.status")}</Th></tr></thead>
          <tbody>
            {log.data.map((a) => (
              <Fragment key={a.id}>
                <Tr className="cursor-pointer" onClick={() => setOpen(open === a.id ? null : a.id)}>
                  <Td className="whitespace-nowrap text-muted" title={formatDate(a.at, i18n.language)}>{timeAgo(a.at, i18n.language)}</Td>
                  {mcp ? (
                    <>
                      <Td><div className="font-semibold">{clientLabel(a.detail.client, a.detail.token)}</div><div className="text-xs text-muted">{a.detail.token} {a.detail.token_kind === "oauth" ? "· OAuth" : "· token"}</div></Td>
                      <Td><span className="mono text-[13px]">{a.detail.tool ?? a.action.replace(/^mcp\./, "")}</span>{a.detail.duration_ms != null && <div className="text-xs text-muted">{a.detail.duration_ms} ms</div>}</Td>
                    </>
                  ) : (
                    <>
                      <Td><Badge tone={a.source === "mcp" ? "info" : a.source === "system" ? "neutral" : "accent"}>{a.source === "mcp" ? clientLabel(a.detail.client, "MCP") : a.source.toUpperCase()}</Badge></Td>
                      <Td className="mono text-[13px]">{a.action}</Td>
                    </>
                  )}
                  <Td>{a.username ?? <span className="text-muted">{t("audit.system")}</span>}</Td>
                  <Td className="max-w-56 truncate text-muted">{a.target ?? "–"}</Td>
                  <Td>{a.success ? <Badge tone="online" dot>{t("common.success")}</Badge> : <Badge tone="offline" dot>{t("common.failed")}</Badge>}</Td>
                </Tr>
                {open === a.id && (
                  <tr><td colSpan={6} className="border-b border-line bg-sunken px-5 py-3">
                    <div className="grid gap-3 text-xs md:grid-cols-2">
                      {a.detail.args && <div><div className="mb-1 font-semibold text-muted">{t("ai.parameters")}</div><pre className="mono overflow-x-auto rounded bg-raised p-2">{JSON.stringify(a.detail.args, null, 2)}</pre></div>}
                      <div><div className="mb-1 font-semibold text-muted">{a.detail.error ? t("ai.error") : t("ai.result")}</div><pre className="mono overflow-x-auto whitespace-pre-wrap rounded bg-raised p-2">{a.detail.error ?? JSON.stringify(a.detail.result ?? a.detail, null, 2)}</pre></div>
                      {a.detail.client?.user_agent && <div className="md:col-span-2 text-muted">User-Agent: <span className="mono">{a.detail.client.user_agent}</span> · IP: <span className="mono">{a.ip}</span></div>}
                    </div>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

export function AiActivityPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("ai.activity_title")} description={t("ai.activity_subtitle")} />
      <AuditTable source="mcp" />
    </>
  );
}

export function AuditPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("audit.title")} description={t("audit.subtitle")} />
      <AuditTable />
    </>
  );
}
