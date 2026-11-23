import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  GitBranch,
  KeyRound,
  LogIn,
  Network,
  Pencil,
  Phone,
  Plug,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  Unplug,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";
import { RiskBadge, type Risk } from "@/pages/Gateway";

/* ------------------------------------------------------------------ types */
type Transport = "http" | "sse" | "stdio";
type AuthKind = "none" | "bearer" | "headers" | "oauth";
type UpStatus = "unknown" | "online" | "error" | "auth_required" | "disabled";
type ToolState = "new" | "changed" | "active" | "disabled";
const RISKS: Risk[] = ["read", "operate", "change", "destructive"];

interface HubTool {
  name: string;
  exposed: string;
  title: string | null;
  description: string | null;
  annotations: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  state: ToolState;
  risk: Risk;
  suggested_risk: Risk;
  risk_overridden: boolean;
  first_seen: string | null;
}

export interface Upstream {
  id: string;
  name: string;
  namespace: string;
  description: string | null;
  customer_id: string | null;
  customer_name: string | null;
  transport: Transport;
  url: string | null;
  agent_node_id: string | null;
  tls_mode: "verify" | "pin" | "insecure";
  fingerprint: string | null;
  command: string | null;
  args: string[];
  auth_kind: AuthKind;
  enabled: boolean;
  status: UpStatus;
  last_error: string | null;
  last_checked_at: string | null;
  latency_ms: number | null;
  server_info: { name?: string; version?: string; protocol?: string };
  preset: string | null;
  timeout: number;
  token_env: string | null;
  headers_public: Record<string, string>;
  env_public: Record<string, string>;
  secret_header_names: string[];
  secret_env_names: string[];
  has_token: boolean;
  oauth: { client_id: string | null; scopes: string | null; has_client_secret: boolean; connected: boolean; expires_at: string | null };
  tool_count: number;
  active_count: number;
  pending_count: number;
  instructions?: string | null;
  tools?: HubTool[];
}

interface Preset {
  id: string;
  name: string;
  namespace: string;
  transport: Transport;
  url: string;
  auth_kind: AuthKind;
  description: string;
  token_hint?: string;
  headers_hint?: Record<string, string>;
  docs?: string;
  via_agent_recommended?: boolean;
  stdio?: { command: string; token_env: string; env_hint?: Record<string, string> };
}

interface HubData {
  upstreams: Upstream[];
  presets: Preset[];
  agents: { id: string; name: string; customer_id: string; online: boolean }[];
  customers: { id: string; name: string }[];
}

const statusTone = { unknown: "neutral", online: "online", error: "offline", auth_required: "warning", disabled: "neutral" } as const;

export function UpstreamStatus({ status }: { status: UpStatus }) {
  const { t } = useTranslation();
  return <Badge tone={statusTone[status]} dot>{t(`hub.status.${status}`)}</Badge>;
}

function presetIcon(id: string | null | undefined, className = "size-5") {
  if (id === "github") return <GitBranch className={className} />;
  if (id === "keystone") return <Phone className={className} />;
  return <Plug className={className} />;
}

function useHubInvalidate() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["hub"] });
    qc.invalidateQueries({ queryKey: ["hub-upstream"] });
    qc.invalidateQueries({ queryKey: ["hub-catalog"] });
  };
}

function hubError(e: unknown, t: (k: string) => string): string {
  const d = (e as { detail?: { code?: string; message?: string } })?.detail;
  if (d && typeof d === "object" && d.message) {
    const key = `hub.err.${d.code}`;
    const tr = t(key);
    return tr === key ? d.message : tr;
  }
  return errorMessage(e, t);
}

/* ------------------------------------------------------------------ the MCP hub page */
export function HubPage() {
  const { t, i18n } = useTranslation();
  const invalidate = useHubInvalidate();
  const [params, setParams] = useSearchParams();
  const q = useQuery({ queryKey: ["hub"], queryFn: () => get<HubData>("api/hub/upstreams"), refetchInterval: 30_000 });
  const [edit, setEdit] = useState<{ upstream: Upstream | null; preset: Preset | null } | null>(null);
  const [choose, setChoose] = useState(false);
  const [review, setReview] = useState<string | null>(null);
  const [remove, setRemove] = useState<Upstream | null>(null);

  useEffect(() => {
    const o = params.get("oauth");
    if (!o) return;
    if (o === "ok") toast.success(t("hub.oauth.connected"));
    else toast.error(`${t("hub.oauth.failed")}: ${params.get("message") || ""}`);
    if (params.get("id")) setReview(params.get("id"));
    setParams({}, { replace: true });
  }, [params, setParams, t]);

  const refresh = useMutation({
    mutationFn: (id: string) => post<{ diff: { added: string[]; changed: string[]; needs_review: number } }>(`api/hub/upstreams/${id}/refresh`),
    onSuccess: (r) => {
      invalidate();
      toast.success(r.diff.needs_review ? t("hub.refreshed_review", { count: r.diff.needs_review }) : t("hub.refreshed"));
    },
    onError: (e) => { invalidate(); toast.error(hubError(e, t)); },
  });
  const delMut = useMutation({
    mutationFn: (id: string) => del(`api/hub/upstreams/${id}`),
    onSuccess: () => { invalidate(); setRemove(null); toast.success(t("hub.deleted")); },
  });

  if (!q.data) return <Spinner />;
  const d = q.data;
  const pending = d.upstreams.reduce((n, u) => n + u.pending_count, 0);
  return (
    <>
      <PageHeader title={t("hub.title")} description={t("hub.desc")}
        actions={<Button variant="primary" onClick={() => setChoose(true)}><Plus />{t("hub.add")}</Button>} />
      {pending > 0 && <Alert tone="warning" className="mb-4" title={t("hub.pending_title", { count: pending })}>{t("hub.pending_desc")}</Alert>}
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="min-w-0 space-y-4 xl:col-span-2">
          {d.upstreams.length === 0 ? (
            <Card><EmptyState icon={<Network />} title={t("hub.empty_title")} action={<Button variant="primary" onClick={() => setChoose(true)}><Plus />{t("hub.add")}</Button>}>{t("hub.empty_desc")}</EmptyState></Card>
          ) : d.upstreams.map((u) => (
            <Card key={u.id}>
              <div className="flex flex-wrap items-start gap-4 px-5 py-4">
                <div className="grid size-11 shrink-0 place-items-center rounded-md bg-sunken text-ink">{presetIcon(u.preset)}</div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-[15px] font-bold">{u.name}</h2>
                    <UpstreamStatus status={u.status} />
                    <Badge tone="neutral"><code>{u.namespace}__*</code></Badge>
                    {u.pending_count > 0 && <Badge tone="warning">{t("hub.to_review", { count: u.pending_count })}</Badge>}
                  </div>
                  <div className="mt-1 text-sm text-muted">
                    {u.customer_name ? t("hub.for_customer", { name: u.customer_name }) : t("hub.shared")}
                    {" · "}{t(`hub.transport.${u.transport}`)}{u.agent_node_id ? ` · ${t("hub.via_agent_short", { name: d.agents.find((a) => a.id === u.agent_node_id)?.name ?? "?" })}` : ""}
                    {" · "}{t("hub.tools_count", { active: u.active_count, total: u.tool_count })}
                    {u.last_checked_at && <> · {t("hub.checked", { when: timeAgo(u.last_checked_at, i18n.language) })}{u.latency_ms != null && ` (${u.latency_ms} ms)`}</>}
                  </div>
                  {u.description && <p className="mt-1 text-sm">{u.description}</p>}
                  {u.last_error && u.status !== "online" && <p className="mt-2 break-words rounded-sm bg-sunken px-2 py-1 font-mono text-xs text-offline">{u.last_error}</p>}
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  <Button size="sm" variant={u.pending_count ? "dark" : "secondary"} onClick={() => setReview(u.id)}><Wrench />{u.pending_count ? t("hub.review") : t("hub.tools")}</Button>
                  <Button size="icon" variant="ghost" aria-label={t("hub.refresh")} title={t("hub.refresh")} onClick={() => refresh.mutate(u.id)} disabled={refresh.isPending}><RefreshCw className={cn(refresh.isPending && refresh.variables === u.id && "animate-spin")} /></Button>
                  <Button size="icon" variant="ghost" aria-label={t("common.edit")} title={t("common.edit")} onClick={() => setEdit({ upstream: u, preset: d.presets.find((p) => p.id === u.preset) ?? null })}><Pencil /></Button>
                  <Button size="icon" variant="ghost" aria-label={t("common.delete")} title={t("common.delete")} onClick={() => setRemove(u)}><Trash2 /></Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
        <div className="min-w-0 space-y-6">
          <HubAccessCard />
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("hub.how_title")} />
            <CardBody className="space-y-2 text-sm text-muted">
              <p>{t("hub.how_1")}</p>
              <p>{t("hub.how_2")}</p>
              <p>{t("hub.how_3")}</p>
              <p>{t("hub.how_4")} <Link to="/ai/tools" className="text-accent-text underline-offset-4 hover:underline">{t("nav.tool_catalog")}</Link></p>
            </CardBody>
          </Card>
        </div>
      </div>

      <Dialog open={choose} onOpenChange={setChoose} title={t("hub.choose_title")} description={t("hub.choose_desc")} wide>
        <div className="grid gap-3 sm:grid-cols-3">
          {d.presets.map((p) => (
            <button key={p.id} type="button" onClick={() => { setChoose(false); setEdit({ upstream: null, preset: p }); }}
              className="flex flex-col items-start gap-2 rounded-md border border-line bg-raised p-4 text-left transition-colors hover:border-koraal hover:bg-sunken">
              <span className="grid size-10 place-items-center rounded-md bg-sunken">{presetIcon(p.id)}</span>
              <span className="font-bold">{p.name || t("hub.custom")}</span>
              <span className="text-xs text-muted">{t(`hub.preset.${p.id}`)}</span>
            </button>
          ))}
        </div>
      </Dialog>

      {edit && <UpstreamDialog data={d} upstream={edit.upstream} preset={edit.preset} onClose={(id) => { setEdit(null); if (id) setReview(id); }} />}
      <ReviewDialog id={review} onClose={() => setReview(null)} />
      <Dialog open={!!remove} onOpenChange={(v) => !v && setRemove(null)} title={t("hub.delete_title", { name: remove?.name })}
        footer={<><Button onClick={() => setRemove(null)}>{t("common.cancel")}</Button><Button variant="danger" loading={delMut.isPending} onClick={() => remove && delMut.mutate(remove.id)}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("hub.delete_desc")}</p>
      </Dialog>
    </>
  );
}

/* ------------------------------------------------------------------ add / edit */
interface KV { k: string; v: string }
const toKV = (m: Record<string, string> | undefined | null): KV[] => Object.entries(m ?? {}).map(([k, v]) => ({ k, v }));
const fromKV = (rows: KV[]) => Object.fromEntries(rows.filter((r) => r.k.trim()).map((r) => [r.k.trim(), r.v]));

function KVEditor({ rows, onChange, secret, keyPlaceholder }: { rows: KV[]; onChange: (r: KV[]) => void; secret?: boolean; keyPlaceholder: string }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <div key={i} className="flex gap-2">
          <Input className="h-9 w-2/5 font-mono text-xs" value={r.k} placeholder={keyPlaceholder} onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, k: e.target.value } : x)))} />
          <Input className="h-9 flex-1 font-mono text-xs" type={secret ? "password" : "text"} value={r.v} placeholder={secret && r.v === "***" ? "" : t("hub.value")}
            onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, v: e.target.value } : x)))} />
          <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => onChange(rows.filter((_, j) => j !== i))}><Trash2 /></Button>
        </div>
      ))}
      <Button size="sm" variant="ghost" onClick={() => onChange([...rows, { k: "", v: "" }])}><Plus />{t("hub.add_row")}</Button>
    </div>
  );
}

function UpstreamDialog({ data, upstream, preset, onClose }: { data: HubData; upstream: Upstream | null; preset: Preset | null; onClose: (openId?: string) => void }) {
  const { t } = useTranslation();
  const invalidate = useHubInvalidate();
  const u = upstream;
  const p = preset;
  const [f, setF] = useState(() => ({
    name: u?.name ?? p?.name ?? "",
    namespace: u?.namespace ?? p?.namespace ?? "",
    description: u?.description ?? p?.description ?? "",
    customer_id: u?.customer_id ?? "",
    transport: (u?.transport ?? p?.transport ?? "http") as Transport,
    url: u?.url ?? p?.url ?? "",
    via_agent: u ? !!u.agent_node_id : !!p?.via_agent_recommended,
    agent_node_id: u?.agent_node_id ?? "",
    tls_mode: u?.tls_mode ?? "verify",
    fingerprint: u?.fingerprint ?? "",
    command: u?.command ?? p?.stdio?.command ?? "",
    args: (u?.args ?? []).join("\n"),
    auth_kind: (u?.auth_kind ?? p?.auth_kind ?? "none") as AuthKind,
    token: "",
    token_env: u?.token_env ?? p?.stdio?.token_env ?? "",
    headers_public: toKV(u?.headers_public ?? p?.headers_hint),
    secret_headers: u ? u.secret_header_names.map((k) => ({ k, v: "***" })) : [],
    env_public: toKV(u?.env_public ?? p?.stdio?.env_hint),
    secret_env: u ? u.secret_env_names.map((k) => ({ k, v: "***" })) : [],
    oauth_client_id: u?.oauth.client_id ?? "",
    oauth_client_secret: "",
    oauth_scopes: u?.oauth.scopes ?? "",
    timeout: u?.timeout ?? 60,
    enabled: u?.enabled ?? true,
  }));
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((x) => ({ ...x, [k]: v }));
  const agents = data.agents.filter((a) => !f.customer_id || a.customer_id === f.customer_id);
  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: f.name, namespace: f.namespace, description: f.description, customer_id: f.customer_id || null,
        transport: f.transport, url: f.url, via_agent: f.transport === "http" && f.via_agent, agent_node_id: f.agent_node_id || null,
        tls_mode: f.tls_mode, fingerprint: f.fingerprint || null, command: f.command,
        args: f.args.split("\n").map((s) => s.trim()).filter(Boolean), auth_kind: f.auth_kind,
        token: f.token ? f.token : u ? null : "", token_env: f.token_env || null,
        headers_public: fromKV(f.headers_public), secret_headers: fromKV(f.secret_headers),
        env_public: fromKV(f.env_public), secret_env: fromKV(f.secret_env),
        oauth_client_id: f.oauth_client_id, oauth_client_secret: f.oauth_client_secret ? f.oauth_client_secret : null,
        oauth_scopes: f.oauth_scopes, timeout: f.timeout, enabled: f.enabled, preset: p?.id ?? u?.preset ?? null,
      };
      return u ? put<Upstream>(`api/hub/upstreams/${u.id}`, body) : post<Upstream>("api/hub/upstreams", body);
    },
    onSuccess: (r) => {
      invalidate();
      toast.success(t("common.saved"));
      onClose(r.pending_count > 0 || r.auth_kind === "oauth" ? r.id : undefined);
    },
  });
  const cert = useMutation({
    mutationFn: () => post<{ fingerprint: string }>("api/hub/certificate", { url: f.url, agent_node_id: f.via_agent ? f.agent_node_id : null }),
    onSuccess: (r) => set("fingerprint", r.fingerprint),
  });
  const isStdio = f.transport === "stdio";
  const tokenHint = p?.token_hint ? t(`hub.token_hint.${p.id}`) : undefined;
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide
      title={u ? t("hub.edit_title", { name: u.name }) : t("hub.add_title", { name: p?.name || t("hub.custom") })}
      description={p?.docs ? <a href={p.docs} target="_blank" rel="noreferrer" className="text-accent-text hover:underline">{p.docs}</a> : undefined}
      footer={<><Button onClick={() => onClose()}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{u ? t("common.save") : t("hub.add_and_connect")}</Button></>}>
      <div className="space-y-4">
        {save.isError && <Alert tone="offline">{hubError(save.error, t)}</Alert>}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("hub.f.name")}><Input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Keystone VoIP" /></Field>
          <Field label={t("hub.f.namespace")} hint={t("hub.f.namespace_hint", { example: `${f.namespace || "keystone"}__list_calls` })}>
            <Input className="font-mono" value={f.namespace} onChange={(e) => set("namespace", e.target.value.toLowerCase())} placeholder="keystone" />
          </Field>
        </div>
        <Field label={t("hub.f.customer")} hint={t("hub.f.customer_hint")}>
          <Select value={f.customer_id} onChange={(e) => set("customer_id", e.target.value)}>
            <option value="">{t("hub.shared_option")}</option>
            {data.customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
        </Field>
        <Field label={t("hub.f.transport")}>
          <Segmented value={f.transport} onChange={(v) => set("transport", v)} options={(["http", "sse", "stdio"] as Transport[]).map((x) => ({ value: x, label: t(`hub.transport.${x}`) }))} />
        </Field>
        {isStdio ? (
          <>
            <Alert tone="info">{t("hub.f.stdio_note")}</Alert>
            <Field label={t("hub.f.command")} hint={t("hub.f.command_hint")}><Input className="font-mono" value={f.command} onChange={(e) => set("command", e.target.value)} placeholder="github-mcp-server stdio" /></Field>
            <Field label={t("hub.f.args")} hint={t("hub.f.args_hint")}><Textarea rows={2} className="font-mono text-xs" value={f.args} onChange={(e) => set("args", e.target.value)} /></Field>
            <Field label={t("hub.f.env_public")}><KVEditor rows={f.env_public} onChange={(r) => set("env_public", r)} keyPlaceholder="GITHUB_TOOLSETS" /></Field>
            <Field label={t("hub.f.secret_env")} hint={t("hub.f.secret_hint")}><KVEditor secret rows={f.secret_env} onChange={(r) => set("secret_env", r)} keyPlaceholder="API_KEY" /></Field>
          </>
        ) : (
          <>
            <Field label={t("hub.f.url")}><Input className="font-mono" value={f.url} onChange={(e) => set("url", e.target.value)} placeholder="https://keystone.lan/mcp" /></Field>
            {f.transport === "http" && (
              <Switch checked={f.via_agent} onCheckedChange={(v) => set("via_agent", v)} label={t("hub.f.via_agent")} description={t("hub.f.via_agent_desc")} />
            )}
            {f.via_agent && f.transport === "http" && (
              <Field label={t("hub.f.agent")}>
                <Select value={f.agent_node_id} onChange={(e) => set("agent_node_id", e.target.value)}>
                  <option value="">{t("hub.f.choose_agent")}</option>
                  {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.online ? "" : ` (${t("hub.offline")})`}</option>)}
                </Select>
              </Field>
            )}
            {f.url.startsWith("https://") && (
              <div className="grid gap-3 sm:grid-cols-[auto_1fr] sm:items-end">
                <Field label={t("hub.f.tls")}>
                  <Select value={f.tls_mode} onChange={(e) => set("tls_mode", e.target.value as typeof f.tls_mode)}>
                    {(["verify", "pin", "insecure"] as const).map((m) => <option key={m} value={m}>{t(`hub.tls.${m}`)}</option>)}
                  </Select>
                </Field>
                {f.tls_mode === "pin" && (
                  <div className="flex gap-2">
                    <Input className="font-mono text-xs" value={f.fingerprint} onChange={(e) => set("fingerprint", e.target.value)} placeholder="AB:CD:…" />
                    <Button loading={cert.isPending} onClick={() => cert.mutate()}>{t("hub.f.fetch_cert")}</Button>
                  </div>
                )}
              </div>
            )}
            <Field label={t("hub.f.headers_public")} hint={t("hub.f.headers_public_hint")}><KVEditor rows={f.headers_public} onChange={(r) => set("headers_public", r)} keyPlaceholder="X-MCP-Toolsets" /></Field>
          </>
        )}
        <Field label={t("hub.f.auth")}>
          <Select value={f.auth_kind} onChange={(e) => set("auth_kind", e.target.value as AuthKind)}>
            {(["none", "bearer", "headers", ...(isStdio ? [] : ["oauth"])] as AuthKind[]).map((a) => <option key={a} value={a}>{t(`hub.auth.${a}`)}</option>)}
          </Select>
        </Field>
        {f.auth_kind === "bearer" && (
          <>
            <Field label={t("hub.f.token")} hint={tokenHint ?? (u?.has_token ? t("hub.f.token_keep") : undefined)}>
              <Input type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)} placeholder={u?.has_token ? "••••••••" : ""} />
            </Field>
            {isStdio && <Field label={t("hub.f.token_env")} hint={t("hub.f.token_env_hint")}><Input className="font-mono" value={f.token_env} onChange={(e) => set("token_env", e.target.value)} placeholder="GITHUB_PERSONAL_ACCESS_TOKEN" /></Field>}
          </>
        )}
        {(f.auth_kind === "headers" || f.auth_kind === "bearer") && !isStdio && (
          <Field label={t("hub.f.secret_headers")} hint={t("hub.f.secret_hint")}><KVEditor secret rows={f.secret_headers} onChange={(r) => set("secret_headers", r)} keyPlaceholder="X-API-Key" /></Field>
        )}
        {f.auth_kind === "oauth" && (
          <>
            <Alert tone="info">{t("hub.f.oauth_note")}</Alert>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("hub.f.oauth_client_id")} hint={t("hub.f.oauth_client_id_hint")}><Input value={f.oauth_client_id} onChange={(e) => set("oauth_client_id", e.target.value)} /></Field>
              <Field label={t("hub.f.oauth_client_secret")}><Input type="password" autoComplete="off" value={f.oauth_client_secret} onChange={(e) => set("oauth_client_secret", e.target.value)} placeholder={u?.oauth.has_client_secret ? "••••••••" : ""} /></Field>
            </div>
            <Field label={t("hub.f.oauth_scopes")}><Input value={f.oauth_scopes} onChange={(e) => set("oauth_scopes", e.target.value)} placeholder="repo read:org" /></Field>
          </>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("hub.f.description")}><Input value={f.description} onChange={(e) => set("description", e.target.value)} /></Field>
          <Field label={t("hub.f.timeout")}><Input type="number" min={5} max={600} value={f.timeout} onChange={(e) => set("timeout", Number(e.target.value) || 60)} /></Field>
        </div>
        <Switch checked={f.enabled} onCheckedChange={(v) => set("enabled", v)} label={t("hub.f.enabled")} description={t("hub.f.enabled_desc")} />
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ review the tools of one server */
const stateTone = { new: "warning", changed: "warning", active: "online", disabled: "neutral" } as const;

function ReviewDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useHubInvalidate();
  const q = useQuery({ queryKey: ["hub-upstream", id], queryFn: () => get<Upstream>(`api/hub/upstreams/${id}`), enabled: !!id });
  const [open, setOpen] = useState<string | null>(null);
  const accept = useMutation({
    mutationFn: (names: string[] | null) => post<Upstream>(`api/hub/upstreams/${id}/accept`, { names }),
    onSuccess: () => { invalidate(); toast.success(t("hub.accepted")); },
  });
  const setTool = useMutation({
    mutationFn: ({ name, ...b }: { name: string; enabled?: boolean; risk?: string }) => put<Upstream>(`api/hub/upstreams/${id}/tools/${encodeURIComponent(name)}`, b),
    onSuccess: () => invalidate(),
  });
  const oauth = useMutation({
    mutationFn: () => post<{ authorize_url: string }>(`api/hub/upstreams/${id}/oauth/start`),
    onSuccess: (r) => { window.location.href = r.authorize_url; },
    onError: (e) => toast.error(hubError(e, t)),
  });
  const disconnect = useMutation({ mutationFn: () => post(`api/hub/upstreams/${id}/oauth/disconnect`), onSuccess: () => invalidate() });
  const u = q.data;
  const tools = u?.tools ?? [];
  const pending = tools.filter((x) => x.state === "new" || x.state === "changed");
  return (
    <Dialog open={!!id} onOpenChange={(v) => !v && onClose()} xl title={u ? t("hub.review_title", { name: u.name }) : "…"}
      description={u ? <>{u.server_info.name && `${u.server_info.name} ${u.server_info.version ?? ""} · `}<code>{u.namespace}__*</code></> : undefined}
      footer={<Button onClick={onClose}>{t("common.close")}</Button>}>
      {!u ? <Spinner /> : (
        <div className="space-y-4">
          {u.auth_kind === "oauth" && (
            <Alert tone={u.oauth.connected ? "online" : "warning"} title={u.oauth.connected ? t("hub.oauth.is_connected") : t("hub.oauth.not_connected")}>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button size="sm" variant={u.oauth.connected ? "secondary" : "primary"} loading={oauth.isPending} onClick={() => oauth.mutate()}><LogIn />{u.oauth.connected ? t("hub.oauth.reconnect") : t("hub.oauth.connect")}</Button>
                {u.oauth.connected && <Button size="sm" variant="ghost" onClick={() => disconnect.mutate()}><Unplug />{t("hub.oauth.disconnect")}</Button>}
              </div>
            </Alert>
          )}
          {u.last_error && u.status !== "online" && <Alert tone="offline">{u.last_error}</Alert>}
          {pending.length > 0 && (
            <Alert tone="warning" title={t("hub.review_pending", { count: pending.length })}>
              <p>{t("hub.review_pending_desc")}</p>
              <Button size="sm" variant="dark" className="mt-2" loading={accept.isPending} onClick={() => accept.mutate(null)}><CheckCircle2 />{t("hub.accept_all", { count: pending.length })}</Button>
            </Alert>
          )}
          {u.instructions && (
            <details className="rounded-md border border-line px-3 py-2 text-sm"><summary className="cursor-pointer font-semibold">{t("hub.instructions")}</summary><p className="mt-2 whitespace-pre-wrap text-muted">{u.instructions}</p></details>
          )}
          {tools.length === 0 ? <p className="text-sm text-muted">{t("hub.no_tools")}</p> : (
            <Table>
              <thead><tr><Th>{t("hub.tool")}</Th><Th>{t("hub.state")}</Th><Th>{t("hub.risk")}</Th><Th>{t("hub.on")}</Th><Th /></tr></thead>
              <tbody>
                {tools.map((x) => (
                  <Tr key={x.name}>
                    <Td className="max-w-md">
                      <button type="button" className="text-left" onClick={() => setOpen(open === x.name ? null : x.name)}>
                        <div className="font-mono text-[13px] font-semibold">{x.exposed}</div>
                        <div className={cn("text-xs text-muted", open !== x.name && "line-clamp-2")}>{x.description}</div>
                      </button>
                      {open === x.name && <pre className="mt-2 max-h-60 overflow-auto rounded-sm bg-sunken p-2 text-[11px]">{JSON.stringify(x.input_schema, null, 2)}</pre>}
                    </Td>
                    <Td><Badge tone={stateTone[x.state]}>{t(`hub.tstate.${x.state}`)}</Badge></Td>
                    <Td>
                      <Select className="h-8 w-36 text-xs" value={x.risk_overridden ? x.risk : "auto"} onChange={(e) => setTool.mutate({ name: x.name, risk: e.target.value })}>
                        <option value="auto">{t("hub.risk_auto", { risk: t(`gw.risk.${x.suggested_risk}`) })}</option>
                        {RISKS.map((r) => <option key={r} value={r}>{t(`gw.risk.${r}`)}</option>)}
                      </Select>
                    </Td>
                    <Td>{x.state !== "new" && x.state !== "changed" && <Switch checked={x.state === "active"} onCheckedChange={(v) => setTool.mutate({ name: x.name, enabled: v })} />}</Td>
                    <Td className="text-right">{(x.state === "new" || x.state === "changed") && <Button size="sm" onClick={() => accept.mutate([x.name])}>{t("hub.accept")}</Button>}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ which AI apps may use the hub */
interface AccessApp { kind: "oauth" | "personal"; id: string; name: string; hub: boolean; last_used_at: string | null }

function HubAccessCard() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["hub", "access"], queryFn: () => get<{ apps: AccessApp[] }>("api/hub/access") });
  const toggle = useMutation({
    mutationFn: (a: AccessApp) => post(a.kind === "oauth" ? `api/tokens/connections/${a.id}/scopes` : `api/tokens/${a.id}/scopes`,
      a.hub ? { remove: ["hub"] } : { add: ["hub"] }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["hub", "access"] }); toast.success(t("common.saved")); },
  });
  return (
    <Card>
      <CardHeader icon={<Bot />} title={t("hub.access_title")} description={t("hub.access_desc")} />
      {!q.data ? <CardBody><Spinner /></CardBody> : q.data.apps.length === 0 ? (
        <CardBody className="text-sm text-muted">{t("hub.access_none")} <Link to="/ai/connections" className="text-accent-text hover:underline">{t("nav.ai_connections")}</Link></CardBody>
      ) : (
        <ul className="divide-y divide-line">
          {q.data.apps.map((a) => (
            <li key={`${a.kind}:${a.id}`} className="flex items-center justify-between gap-3 px-5 py-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 truncate font-semibold">{a.kind === "oauth" ? <Bot className="size-4 text-muted" /> : <KeyRound className="size-4 text-muted" />}{a.name}</div>
                <div className="text-xs text-muted">{a.last_used_at ? t("hub.used", { when: timeAgo(a.last_used_at, i18n.language) }) : t("hub.never_used")}</div>
              </div>
              <Switch checked={a.hub} disabled={toggle.isPending} onCheckedChange={() => toggle.mutate(a)} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ tool catalog */
interface CatalogTool {
  name: string;
  risk: Risk;
  scope: string;
  description: string;
  source: "stackpilot" | "hub";
  upstream_id: string | null;
  upstream_name: string | null;
  namespace: string | null;
  customer_name: string | null;
  default_rule: string | null;
  token_rule: string | null;
  enabled: boolean;
}
interface CatalogGroup { id: string; name: string; namespace: string | null; status?: UpStatus; enabled?: boolean; default_rule: string | null; token_rule: string | null; pending_count?: number }
interface CatalogData { tools: CatalogTool[]; groups: CatalogGroup[]; tokens: { token_id: string; name: string; kind: string; username: string; hub: boolean }[] }

export function ToolCatalogPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [query, setQuery] = useState("");
  const [risk, setRisk] = useState<"" | Risk>("");
  const q = useQuery({ queryKey: ["hub-catalog", token], queryFn: () => get<CatalogData>(`api/hub/catalog${token ? `?token_id=${token}` : ""}`) });
  const sw = useMutation({
    mutationFn: (b: { key: string; enabled: boolean }) => post("api/hub/catalog/switch", { ...b, token_id: token || null }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["hub-catalog"] }); qc.invalidateQueries({ queryKey: ["gateway-policies"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const groups = useMemo(() => {
    if (!q.data) return [];
    const ql = query.trim().toLowerCase();
    const ordered = [...q.data.groups.filter((g) => g.id !== "stackpilot"), ...q.data.groups.filter((g) => g.id === "stackpilot")];
    return ordered.map((g) => ({
      g,
      tools: q.data.tools.filter((x) => (g.id === "stackpilot" ? x.source === "stackpilot" : x.upstream_id === g.id)
        && (!risk || x.risk === risk) && (!ql || x.name.toLowerCase().includes(ql) || x.description.toLowerCase().includes(ql))),
    })).filter(({ g, tools }) => tools.length > 0 || (g.id !== "stackpilot" && !ql && !risk));
  }, [q.data, query, risk]);
  if (!q.data) return <Spinner />;
  const ruleOf = (r: { default_rule: string | null; token_rule: string | null }) => (token ? r.token_rule : r.default_rule);
  const offByDefault = (r: { default_rule: string | null }) => token && r.default_rule === "block";
  return (
    <>
      <PageHeader title={t("cat.title")} description={t("cat.desc")} />
      <Card className="mb-4">
        <CardBody className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
          <Field label={t("cat.for")} hint={token ? t("cat.for_token_hint") : t("cat.for_all_hint")}>
            <Select value={token} onChange={(e) => setToken(e.target.value)}>
              <option value="">{t("cat.all_tokens")}</option>
              {q.data.tokens.map((x) => <option key={x.token_id} value={x.token_id}>{x.name} ({x.username}){x.hub ? "" : ` · ${t("cat.no_hub")}`}</option>)}
            </Select>
          </Field>
          <Field label={t("cat.search")}>
            <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input className="pl-9" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("cat.search_ph")} /></div>
          </Field>
          <Field label={t("hub.risk")}>
            <Select value={risk} onChange={(e) => setRisk(e.target.value as "" | Risk)}>
              <option value="">{t("cat.all_risks")}</option>
              {RISKS.map((r) => <option key={r} value={r}>{t(`gw.risk.${r}`)}</option>)}
            </Select>
          </Field>
        </CardBody>
      </Card>
      <div className="space-y-4">
        {groups.map(({ g, tools }) => {
          const wildcard = g.namespace ? `${g.namespace}__*` : null;
          const groupOff = wildcard ? ruleOf(g) === "block" : false;
          return (
            <Card key={g.id}>
              <CardHeader icon={g.id === "stackpilot" ? <ShieldCheck /> : <Network />}
                title={<span className="flex flex-wrap items-center gap-2">{g.name}{g.namespace && <Badge tone="neutral"><code>{g.namespace}__*</code></Badge>}{g.status && <UpstreamStatus status={g.enabled === false ? "disabled" : g.status} />}{!!g.pending_count && <Badge tone="warning">{t("hub.to_review", { count: g.pending_count })}</Badge>}</span>}
                description={t("cat.group_count", { count: tools.length })}
                action={wildcard ? (
                  <Switch checked={!groupOff} disabled={sw.isPending || !!offByDefault(g)} onCheckedChange={(v) => sw.mutate({ key: wildcard, enabled: v })} label={t("cat.whole_server")} />
                ) : undefined} />
              {tools.length === 0 ? (
                <CardBody className="text-sm text-muted">{g.pending_count ? <Link to="/ai/hub" className="text-accent-text hover:underline">{t("cat.review_first")}</Link> : t("hub.no_tools")}</CardBody>
              ) : (
                <Table>
                  <tbody>
                    {tools.map((x) => {
                      const off = ruleOf(x) === "block";
                      const lockedOff = offByDefault(x) || (wildcard && groupOff);
                      return (
                        <Tr key={x.name} className={cn((off || lockedOff) && "opacity-60")}>
                          <Td className="max-w-xl"><div className="font-mono text-[13px] font-semibold">{x.name}</div><div className="line-clamp-2 text-xs text-muted">{x.description}</div></Td>
                          <Td className="w-32"><RiskBadge risk={x.risk} /></Td>
                          <Td className="w-28 text-xs text-muted">{x.customer_name ?? (x.source === "hub" ? t("hub.shared") : "")}</Td>
                          <Td className="w-16 text-right">
                            <Switch checked={!off && !lockedOff} disabled={sw.isPending || !!lockedOff} onCheckedChange={(v) => sw.mutate({ key: x.name, enabled: v })} />
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                </Table>
              )}
            </Card>
          );
        })}
      </div>
    </>
  );
}

export function HubCommandCenterCard() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["hub"], queryFn: () => get<HubData>("api/hub/upstreams"), refetchInterval: 60_000 });
  const ups = q.data?.upstreams ?? [];
  const body: ReactNode = ups.length === 0 ? (
    <p className="text-sm text-muted">{t("hub.cc_empty")}</p>
  ) : (
    <ul className="space-y-2">
      {ups.slice(0, 6).map((u) => (
        <li key={u.id} className="flex items-center justify-between gap-2 text-sm">
          <span className="flex min-w-0 items-center gap-2 truncate">{presetIcon(u.preset, "size-4 text-muted")}<span className="truncate font-semibold">{u.name}</span><code className="text-xs text-muted">{u.namespace}__</code></span>
          <span className="flex items-center gap-1.5">{u.pending_count > 0 && <AlertTriangle className="size-4 text-warning" />}<UpstreamStatus status={u.status} /></span>
        </li>
      ))}
    </ul>
  );
  return (
    <Card>
      <CardHeader icon={<Network />} title={t("nav.mcp_hub")} action={<Link to="/ai/hub" className="text-sm font-semibold text-accent-text hover:underline">{t("hub.cc_open")}</Link>} />
      <CardBody>{body}</CardBody>
    </Card>
  );
}
