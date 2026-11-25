import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Award, BellOff, Bell, CheckCheck, Eye, EyeOff, FileDiff, Flame, Globe, KeyRound, Pencil, Plus, RefreshCw, Router,
  Settings2, ShieldCheck, Trash2, Waypoints,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { DiffView, tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
type CertStatus = "ok" | "expiring" | "critical" | "expired" | "error" | "ignored";
interface Cert {
  id: string; customer_id: string; customer_name?: string | null; source: "endpoint" | "machine"; endpoint_id: string | null;
  node_id: string | null; node_name: string | null; endpoint_name: string | null; location: string; subject: string | null;
  common_name: string | null; sans: string[]; issuer: string | null; issuer_name: string | null; serial: string | null;
  fingerprint: string | null; not_before: string | null; not_after: string | null; days_left: number | null; key_info: string | null;
  self_signed: boolean; trusted: boolean | null; trust_error: string | null; hostname_ok: boolean | null; in_use: boolean | null;
  renew: string | null; ignored: boolean; ignored_note: string | null; renewed_at: string | null; previous_not_after: string | null;
  status: CertStatus; last_seen: string | null;
}
interface Endpoint {
  id: string; customer_id: string; customer_name?: string | null; name: string | null; host: string; port: number; sni: string | null;
  starttls: string; probe_node_id: string | null; probe_name: string | null; source: "manual" | "uptime" | "nightly";
  warn_days: number | null; enabled: boolean; last_check_at: string | null; last_error: string | null; label: string;
}
interface CertList { certificates: Cert[]; counts: Record<CertStatus, number>; endpoint_errors: Endpoint[]; warn_days: number; critical_days: number }
interface FwSource {
  id: string; customer_id: string; customer_name: string | null; kind: "proxmox" | "linux" | "windows" | "unifi"; label: string;
  enabled: boolean; summary: Record<string, unknown>; last_check_at: string | null; last_error: string | null; unacknowledged: number;
  versions: number; last_change_at: string | null;
}
interface FwChange {
  id: string; source_id: string; customer_name: string | null; taken_at: string; added: number; removed: number; made_by: string | null;
  acknowledged_at: string | null; acknowledged_by: string | null; note: string | null; source_label: string; source_kind: string;
  diff?: string | null; content?: string;
}
type VpnStatus = "up" | "down" | "idle" | "missing" | "unknown";
interface Tunnel {
  id: string; customer_name: string | null; kind: string; name: string; node_name: string | null; connection_name: string | null;
  remote: string | null; status: VpnStatus; status_since: string | null; watched: boolean | null; watching: boolean;
  last_up_at: string | null; changes_24h: number; detail: Record<string, unknown>; changes: { at: string; from: string; to: string }[];
}
interface VpnCheck { id: string; name: string; customer_name: string | null; target: string; port: number | null; mode: string; status: string; status_since: string | null; last_error: string | null; enabled: boolean }
interface Overview {
  certificates: Record<CertStatus, number>; endpoint_errors: number; firewall: { sources: number; unacknowledged: number };
  vpn: Record<VpnStatus, number> & { watched_down: number; total: number };
}
interface Settings {
  cert_interval_hours: number; cert_warn_days: number; cert_critical_days: number; alert_certs: boolean; alert_untrusted: boolean;
  uptime_endpoints: boolean; extra_paths: string[]; fw_interval_minutes: number; fw_keep_versions: number; alert_fw_changes: boolean;
  auto_ack_own: boolean; fw_ignore: string[]; vpn_interval_minutes: number; vpn_down_minutes: number; alert_vpn: boolean;
  wg_handshake_seconds: number;
}

const TABS = ["certificates", "firewall", "vpn", "settings"] as const;
type Tab = (typeof TABS)[number];
const CERT_TONE: Record<CertStatus, "online" | "warning" | "offline" | "neutral"> = { ok: "online", expiring: "warning", critical: "offline", expired: "offline", error: "offline", ignored: "neutral" };
const VPN_TONE: Record<VpnStatus, "online" | "warning" | "offline" | "neutral"> = { up: "online", down: "offline", missing: "offline", idle: "neutral", unknown: "neutral" };
const STARTTLS = ["none", "smtp", "imap", "pop3", "ftp"];

function qs(path: string, params: Record<string, string | number | boolean | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "" && v !== false) u.set(k, String(v));
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["netsec"] });
}

/* ------------------------------------------------------------------ page */
export function SecurityPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { customerId } = useCustomer();
  const params = useParams();
  const navigate = useNavigate();
  const tab: Tab = (TABS as readonly string[]).includes(params.tab ?? "") ? (params.tab as Tab) : "certificates";
  const ov = useQuery({
    queryKey: ["netsec", "overview", customerId],
    queryFn: () => get<Overview>(qs("api/netsec/overview", { customer_id: customerId })),
    refetchInterval: 60_000,
  });
  const invalidate = useInvalidate();
  const refresh = useMutation({
    mutationFn: () => post("api/netsec/refresh", { customer_id: customerId, what: tab === "settings" ? "all" : tab }),
    onSuccess: () => { toast.success(t("sec.refresh_started")); setTimeout(invalidate, 8000); setTimeout(invalidate, 30000); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const c = ov.data?.certificates;
  const soon = (c?.critical ?? 0) + (c?.expiring ?? 0);
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><ShieldCheck className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("sec.title")}</span></span>}
        description={t("sec.desc")}
        actions={admin && customerId && <Button variant="secondary" loading={refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw /> {t("sec.refresh")}</Button>} />
      <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("sec.k_certs")} value={Object.entries(c ?? {}).filter(([k]) => k !== "error").reduce((a, [, v]) => a + v, 0)} tone="info" icon={<Award />}
          hint={t("sec.k_certs_hint", { count: ov.data?.endpoint_errors ?? 0 })} />
        <Stat label={t("sec.k_expiring")} value={soon + (c?.expired ?? 0)} tone={c?.expired || c?.critical ? "offline" : soon ? "warning" : "online"} icon={<KeyRound />}
          hint={t("sec.k_expiring_hint", { expired: c?.expired ?? 0, days: 21 })} />
        <Stat label={t("sec.k_fw")} value={ov.data?.firewall.unacknowledged ?? 0} tone={ov.data?.firewall.unacknowledged ? "warning" : "online"} icon={<Flame />}
          hint={t("sec.k_fw_hint", { count: ov.data?.firewall.sources ?? 0 })} />
        <Stat label={t("sec.k_vpn")} value={`${ov.data?.vpn.up ?? 0} / ${ov.data?.vpn.total ?? 0}`} tone={ov.data?.vpn.watched_down ? "offline" : "online"} icon={<Waypoints />}
          hint={ov.data?.vpn.watched_down ? t("sec.k_vpn_down", { count: ov.data.vpn.watched_down }) : t("sec.k_vpn_hint")} />
      </div>
      <Tabs.Root value={tab} onValueChange={(v) => navigate(`/security/${v}`)}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          {TABS.filter((x) => x !== "settings" || admin).map((x) => <Tabs.Trigger key={x} value={x} className={tabCls}>{t(`sec.tab.${x}`)}</Tabs.Trigger>)}
        </Tabs.List>
        <Tabs.Content value="certificates"><CertificatesTab admin={admin} /></Tabs.Content>
        <Tabs.Content value="firewall"><FirewallTab admin={admin} /></Tabs.Content>
        <Tabs.Content value="vpn"><VpnTab admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="settings"><SettingsTab /></Tabs.Content>}
      </Tabs.Root>
    </div>
  );
}

/* ------------------------------------------------------------------ certificates */
function CertificatesTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const [status, setStatus] = useState<CertStatus | "">("");
  const [source, setSource] = useState("");
  const [open, setOpen] = useState<Cert | null>(null);
  const [editEp, setEditEp] = useState<Endpoint | "new" | null>(null);
  const q = useQuery({
    queryKey: ["netsec", "certs", customerId, status, source],
    queryFn: () => get<CertList>(qs("api/netsec/certificates", { customer_id: customerId, status, source })),
    refetchInterval: 120_000,
  });
  const eps = useQuery({ queryKey: ["netsec", "endpoints", customerId], queryFn: () => get<Endpoint[]>(qs("api/netsec/endpoints", { customer_id: customerId })) });
  const list = q.data?.certificates ?? [];
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Award />} title={t("sec.certs_title")} description={t("sec.certs_desc", { days: q.data?.warn_days ?? 21 })} />
        <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
          <Select className="w-auto" value={status} onChange={(e) => setStatus(e.target.value as CertStatus | "")}>
            <option value="">{t("sec.all_statuses")}</option>
            {(["expired", "critical", "expiring", "ok", "ignored"] as CertStatus[]).map((s) => <option key={s} value={s}>{t(`sec.cs.${s}`)} ({q.data?.counts[s] ?? 0})</option>)}
          </Select>
          <Select className="w-auto" value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">{t("sec.all_sources")}</option>
            <option value="endpoint">{t("sec.src.endpoint")}</option>
            <option value="machine">{t("sec.src.machine")}</option>
          </Select>
        </div>
        {q.isLoading ? <div className="p-5"><Spinner /></div> : q.isError ? <div className="p-5"><Alert tone="offline">{errorMessage(q.error, t)}</Alert></div> : !list.length ? (
          <EmptyState icon={<Award />} title={t("sec.no_certs")}>{t("sec.no_certs_hint")}</EmptyState>
        ) : (
          <Table>
            <thead><Tr><Th>{t("sec.col_cert")}</Th><Th>{t("sec.col_where")}</Th><Th>{t("sec.col_issuer")}</Th><Th>{t("sec.col_expires")}</Th><Th>{t("sec.col_status")}</Th></Tr></thead>
            <tbody>
              {list.map((x) => (
                <Tr key={x.id} className="cursor-pointer" onClick={() => setOpen(x)}>
                  <Td className="max-w-xs">
                    <div className="truncate font-semibold">{x.common_name ?? x.location}</div>
                    <div className="truncate text-xs text-muted">{x.sans.slice(0, 3).join(", ")}{x.sans.length > 3 ? ` +${x.sans.length - 3}` : ""}</div>
                  </Td>
                  <Td className="max-w-xs text-xs">
                    <div className="flex items-center gap-1.5 font-medium">{x.source === "endpoint" ? <Globe className="size-3.5 text-muted" /> : <Router className="size-3.5 text-muted" />}{x.source === "endpoint" ? (x.endpoint_name ?? x.location) : x.node_name}</div>
                    <div className="truncate text-muted">{x.source === "endpoint" ? (x.endpoint_name ? x.location : "") : x.location}</div>
                    {!customerId && x.customer_name && <div className="text-muted">{x.customer_name}</div>}
                  </Td>
                  <Td className="text-xs">{x.self_signed ? <Badge tone="neutral">{t("sec.self_signed")}</Badge> : x.issuer_name ?? "–"}</Td>
                  <Td className="text-xs">
                    <div className={cn("font-semibold", (x.days_left ?? 0) < 0 && "text-offline")}>{x.days_left == null ? "–" : x.days_left < 0 ? t("sec.expired_ago", { count: -x.days_left }) : t("sec.days_left", { count: x.days_left })}</div>
                    <div className="text-muted">{x.not_after ? formatDate(x.not_after, i18n.language) : ""}</div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      <Badge tone={CERT_TONE[x.status]} dot>{t(`sec.cs.${x.status}`)}</Badge>
                      {x.trusted === false && <Badge tone="warning">{t("sec.untrusted")}</Badge>}
                      {x.hostname_ok === false && <Badge tone="warning">{t("sec.name_mismatch")}</Badge>}
                      {x.in_use && <Badge tone="info">{t("sec.in_use")}</Badge>}
                      {x.renew && <Badge tone="neutral">{t(`sec.renew.${x.renew}`, { defaultValue: x.renew })}</Badge>}
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Card>
        <CardHeader icon={<Globe />} title={t("sec.endpoints_title")} description={t("sec.endpoints_desc")}
          action={admin && <Button size="sm" variant="primary" onClick={() => setEditEp("new")}><Plus /> {t("sec.add_endpoint")}</Button>} />
        {eps.isLoading ? <div className="p-5"><Spinner /></div> : !(eps.data ?? []).length ? <EmptyState icon={<Globe />} title={t("sec.no_endpoints")}>{t("sec.no_endpoints_hint")}</EmptyState> : (
          <Table>
            <thead><Tr><Th>{t("sec.col_address")}</Th><Th>{t("sec.col_checked_from")}</Th><Th>{t("sec.col_last")}</Th><Th /></Tr></thead>
            <tbody>
              {(eps.data ?? []).map((e) => (
                <Tr key={e.id}>
                  <Td>
                    <div className="flex flex-wrap items-center gap-1.5 font-semibold">{e.name ?? e.label}{e.source !== "manual" && <Badge tone="neutral">{t(`sec.ep_src.${e.source}`)}</Badge>}{!e.enabled && <Badge tone="neutral">{t("sec.paused")}</Badge>}</div>
                    <div className="text-xs text-muted">{e.label}{e.sni ? ` · SNI ${e.sni}` : ""}{!customerId && e.customer_name ? ` · ${e.customer_name}` : ""}</div>
                  </Td>
                  <Td className="text-xs">{e.probe_name ?? t("sec.from_stackpilot")}</Td>
                  <Td className="text-xs">{e.last_error ? <span className="text-offline">{e.last_error}</span> : e.last_check_at ? timeAgo(e.last_check_at, i18n.language) : t("sec.never")}</Td>
                  <Td className="text-right">{admin && e.source !== "uptime" && <Button size="sm" variant="ghost" onClick={() => setEditEp(e)}><Pencil /></Button>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {open && <CertDialog cert={open} admin={admin} onClose={() => setOpen(null)} />}
      {admin && editEp && <EndpointDialog ep={editEp === "new" ? null : editEp} onClose={() => setEditEp(null)} />}
    </div>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="flex items-baseline justify-between gap-3 border-b border-line/60 pb-1.5"><dt className="shrink-0 text-muted">{k}</dt><dd className="min-w-0 break-all text-right font-medium">{v}</dd></div>;
}

function CertDialog({ cert: x, admin, onClose }: { cert: Cert; admin: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const [note, setNote] = useState(x.ignored_note ?? "");
  const ignore = useMutation({
    mutationFn: (ignored: boolean) => post(`api/netsec/certificates/${x.id}/ignore`, { ignored, note }),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={x.common_name ?? x.location} description={x.source === "endpoint" ? x.location : `${x.node_name} · ${x.location}`}>
      <div className="space-y-4">
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={CERT_TONE[x.status]} dot>{t(`sec.cs.${x.status}`)}</Badge>
          {x.trusted === true && <Badge tone="online">{t("sec.trusted")}</Badge>}
          {x.trusted === false && <Badge tone="warning">{t("sec.untrusted")}</Badge>}
          {x.hostname_ok === false && <Badge tone="warning">{t("sec.name_mismatch")}</Badge>}
          {x.self_signed && <Badge tone="neutral">{t("sec.self_signed")}</Badge>}
          {x.in_use && <Badge tone="info">{t("sec.in_use")}</Badge>}
        </div>
        {x.trust_error && <Alert tone="warning" title={t("sec.untrusted")}>{x.trust_error}</Alert>}
        <dl className="grid grid-cols-1 gap-y-2 text-sm">
          <Row k={t("sec.subject")} v={x.subject ?? "–"} />
          <Row k={t("sec.names")} v={x.sans.length ? x.sans.join(", ") : "–"} />
          <Row k={t("sec.col_issuer")} v={x.issuer ?? "–"} />
          <Row k={t("sec.valid")} v={`${x.not_before ? formatDate(x.not_before, i18n.language) : "?"} → ${x.not_after ? formatDate(x.not_after, i18n.language) : "?"}`} />
          <Row k={t("sec.key")} v={x.key_info ?? "–"} />
          <Row k={t("sec.renewal")} v={x.renew ? t(`sec.renew.${x.renew}`, { defaultValue: x.renew }) : t("sec.renew_none")} />
          {x.renewed_at && <Row k={t("sec.renewed")} v={`${formatDate(x.renewed_at, i18n.language)} (${t("sec.was_until", { date: x.previous_not_after ? formatDate(x.previous_not_after, i18n.language) : "?" })})`} />}
          <Row k={t("sec.fingerprint")} v={<span className="font-mono text-xs">{x.fingerprint ?? "–"}</span>} />
          <Row k={t("sec.serial")} v={<span className="font-mono text-xs">{x.serial ?? "–"}</span>} />
          <Row k={t("sec.last_seen")} v={x.last_seen ? timeAgo(x.last_seen, i18n.language) : "–"} />
        </dl>
        {admin && (
          <div className="space-y-2 border-t border-line pt-4">
            <Field label={t("sec.ignore_note")} hint={t("sec.ignore_hint")}><Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} /></Field>
            {x.ignored
              ? <Button size="sm" variant="secondary" loading={ignore.isPending} onClick={() => ignore.mutate(false)}><Bell /> {t("sec.watch_again")}</Button>
              : <Button size="sm" variant="secondary" loading={ignore.isPending} onClick={() => ignore.mutate(true)}><BellOff /> {t("sec.ignore")}</Button>}
          </div>
        )}
      </div>
    </Dialog>
  );
}

function EndpointDialog({ ep, onClose }: { ep: Endpoint | null; onClose: () => void }) {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const [f, setF] = useState({
    customer_id: ep?.customer_id ?? customerId ?? "", name: ep?.name ?? "", host: ep?.host ?? "", port: String(ep?.port ?? 443),
    sni: ep?.sni ?? "", starttls: ep?.starttls ?? "none", probe_node_id: ep?.probe_node_id ?? "", warn_days: ep?.warn_days != null ? String(ep.warn_days) : "",
    enabled: ep?.enabled ?? true,
  });
  const probes = useQuery({
    queryKey: ["netsec", "probes", f.customer_id],
    queryFn: () => get<{ id: string; name: string; kind: string; online: boolean }[]>(`api/uptime/probes?customer_id=${f.customer_id}`),
    enabled: !!f.customer_id,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = { ...f, port: Number(f.port) || 443, sni: f.sni || null, name: f.name || null, probe_node_id: f.probe_node_id || null, warn_days: f.warn_days ? Number(f.warn_days) : null };
      return ep ? put(`api/netsec/endpoints/${ep.id}`, body) : post("api/netsec/endpoints", body);
    },
    onSuccess: () => { toast.success(t("sec.ep_saved")); invalidate(); setTimeout(invalidate, 5000); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/netsec/endpoints/${ep!.id}`),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={ep ? t("sec.edit_endpoint") : t("sec.add_endpoint")} description={t("sec.endpoint_dialog_desc")}
      footer={<>
        {ep && <Button variant="ghost" className="mr-auto text-offline" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 /> {t("sec.delete")}</Button>}
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!f.customer_id || !f.host} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="grid gap-4 sm:grid-cols-2">
        {!customerId && !ep && (
          <Field label={t("sec.customer")} className="sm:col-span-2">
            <Select value={f.customer_id} onChange={set("customer_id")}>
              <option value="">–</option>
              {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
        )}
        <Field label={t("sec.ep_name")} className="sm:col-span-2"><Input value={f.name} onChange={set("name")} placeholder={t("sec.ep_name_ph")} /></Field>
        <Field label={t("sec.ep_host")}><Input value={f.host} onChange={set("host")} placeholder="mail.klant.nl" /></Field>
        <Field label={t("sec.ep_port")}><Input type="number" min={1} max={65535} value={f.port} onChange={set("port")} /></Field>
        <Field label={t("sec.ep_starttls")} hint={t("sec.ep_starttls_hint")}>
          <Select value={f.starttls} onChange={set("starttls")}>{STARTTLS.map((s) => <option key={s} value={s}>{t(`sec.starttls.${s}`)}</option>)}</Select>
        </Field>
        <Field label={t("sec.ep_sni")} hint={t("sec.ep_sni_hint")}><Input value={f.sni} onChange={set("sni")} /></Field>
        <Field label={t("sec.ep_probe")} hint={t("sec.ep_probe_hint")}>
          <Select value={f.probe_node_id} onChange={set("probe_node_id")}>
            <option value="">{t("sec.from_stackpilot")}</option>
            {(probes.data ?? []).map((n) => <option key={n.id} value={n.id}>{n.name}{n.online ? "" : ` (${t("sec.offline")})`}</option>)}
          </Select>
        </Field>
        <Field label={t("sec.ep_warn")} hint={t("sec.ep_warn_hint")}><Input type="number" min={1} max={180} value={f.warn_days} onChange={set("warn_days")} /></Field>
        <div className="sm:col-span-2"><Switch checked={f.enabled} onCheckedChange={(v) => setF({ ...f, enabled: v })} label={t("sec.ep_enabled")} /></div>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ firewall */
function FirewallTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const [onlyOpen, setOnlyOpen] = useState(false);
  const [sourceId, setSourceId] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const sources = useQuery({ queryKey: ["netsec", "fw-sources", customerId], queryFn: () => get<FwSource[]>(qs("api/netsec/firewall/sources", { customer_id: customerId })), refetchInterval: 120_000 });
  const changes = useQuery({
    queryKey: ["netsec", "fw-changes", customerId, onlyOpen, sourceId],
    queryFn: () => get<FwChange[]>(qs("api/netsec/firewall/changes", { customer_id: customerId, unacknowledged: onlyOpen, source_id: sourceId, days: 90 })),
    refetchInterval: 120_000,
  });
  const ackAll = useMutation({
    mutationFn: (body: Record<string, string>) => post<{ acknowledged: number }>("api/netsec/firewall/ack", body),
    onSuccess: (r) => { toast.success(t("sec.acked", { count: r.acknowledged })); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const toggle = useMutation({
    mutationFn: (s: FwSource) => patch(`api/netsec/firewall/sources/${s.id}`, { enabled: !s.enabled }),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const list = sources.data ?? [];
  const openCount = list.reduce((a, s) => a + s.unacknowledged, 0);
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Flame />} title={t("sec.fw_title")} description={t("sec.fw_desc")}
          action={admin && customerId && openCount > 0 && <Button size="sm" variant="secondary" loading={ackAll.isPending} onClick={() => ackAll.mutate({ customer_id: customerId })}><CheckCheck /> {t("sec.ack_all", { count: openCount })}</Button>} />
        {sources.isLoading ? <div className="p-5"><Spinner /></div> : !list.length ? <EmptyState icon={<Flame />} title={t("sec.no_fw")}>{t("sec.no_fw_hint")}</EmptyState> : (
          <Table>
            <thead><Tr><Th>{t("sec.col_firewall")}</Th><Th>{t("sec.col_rules")}</Th><Th>{t("sec.col_last_change")}</Th><Th>{t("sec.col_status")}</Th><Th /></Tr></thead>
            <tbody>
              {list.map((s) => (
                <Tr key={s.id}>
                  <Td>
                    <div className="font-semibold">{s.label}</div>
                    <div className="text-xs text-muted">{t(`sec.fwk.${s.kind}`)}{!customerId && s.customer_name ? ` · ${s.customer_name}` : ""}</div>
                  </Td>
                  <Td className="text-xs"><FwSummary s={s} /></Td>
                  <Td className="text-xs">{s.last_change_at ? timeAgo(s.last_change_at, i18n.language) : t("sec.no_changes")}<div className="text-muted">{t("sec.checked", { ago: s.last_check_at ? timeAgo(s.last_check_at, i18n.language) : t("sec.never") })}</div></Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {s.unacknowledged > 0 ? <Badge tone="warning" dot>{t("sec.to_review", { count: s.unacknowledged })}</Badge> : <Badge tone="online" dot>{t("sec.reviewed")}</Badge>}
                      {s.last_error && <Badge tone="offline" >{t("sec.read_error")}</Badge>}
                      {!s.enabled && <Badge tone="neutral">{t("sec.paused")}</Badge>}
                    </div>
                    {s.last_error && <div className="mt-1 max-w-xs truncate text-xs text-offline" title={s.last_error}>{s.last_error}</div>}
                  </Td>
                  <Td className="whitespace-nowrap text-right">
                    <Button size="sm" variant="ghost" title={t("sec.view_current")} onClick={() => setCurrent(s.id)}><Eye /></Button>
                    {admin && s.unacknowledged > 0 && <Button size="sm" variant="ghost" title={t("sec.ack_source")} onClick={() => ackAll.mutate({ source_id: s.id })}><CheckCheck /></Button>}
                    {admin && <Button size="sm" variant="ghost" title={s.enabled ? t("sec.pause") : t("sec.resume")} onClick={() => toggle.mutate(s)}>{s.enabled ? <EyeOff /> : <Eye />}</Button>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Card>
        <CardHeader icon={<FileDiff />} title={t("sec.changes_title")} description={t("sec.changes_desc")} />
        <div className="flex flex-wrap items-center gap-4 border-b border-line px-5 py-3">
          <Select className="w-auto" value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
            <option value="">{t("sec.all_firewalls")}</option>
            {list.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </Select>
          <Switch checked={onlyOpen} onCheckedChange={setOnlyOpen} label={t("sec.only_open")} />
        </div>
        {changes.isLoading ? <div className="p-5"><Spinner /></div> : !(changes.data ?? []).length ? <EmptyState icon={<ShieldCheck />} title={t("sec.no_changes_found")}>{t("sec.no_changes_hint")}</EmptyState> : (
          <Table>
            <thead><Tr><Th>{t("sec.col_when")}</Th><Th>{t("sec.col_firewall")}</Th><Th>{t("sec.col_diff")}</Th><Th>{t("sec.col_review")}</Th></Tr></thead>
            <tbody>
              {(changes.data ?? []).map((c) => (
                <Tr key={c.id} className="cursor-pointer" onClick={() => setOpen(c.id)}>
                  <Td className="text-xs"><div className="font-semibold">{formatDate(c.taken_at, i18n.language)}</div><div className="text-muted">{timeAgo(c.taken_at, i18n.language)}</div></Td>
                  <Td className="text-xs"><div className="font-medium">{c.source_label}</div>{!customerId && <div className="text-muted">{c.customer_name}</div>}</Td>
                  <Td className="text-xs"><span className="font-mono text-online">+{c.added}</span> <span className="font-mono text-offline">−{c.removed}</span>{c.made_by && <div className="text-muted">{t("sec.made_by", { who: c.made_by })}</div>}</Td>
                  <Td className="text-xs">{c.acknowledged_at ? <span><Badge tone="online">{t("sec.reviewed")}</Badge> <span className="text-muted">{c.acknowledged_by === "stackpilot" ? t("sec.auto_ack") : c.acknowledged_by}</span>{c.note && <div className="text-muted">{c.note}</div>}</span> : <Badge tone="warning" dot>{t("sec.open")}</Badge>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {open && <ChangeDialog id={open} admin={admin} onClose={() => setOpen(null)} />}
      {current && <CurrentDialog sourceId={current} onClose={() => setCurrent(null)} />}
    </div>
  );
}

function FwSummary({ s }: { s: FwSource }) {
  const { t } = useTranslation();
  const m = s.summary as Record<string, unknown>;
  if (s.kind === "windows") {
    const off = (m.disabled_profiles as string[] | undefined) ?? [];
    return <span>{t("sec.n_rules", { count: Number(m.rules ?? 0) })}{off.length > 0 && <div className="text-offline">{t("sec.profiles_off", { list: off.join(", ") })}</div>}</span>;
  }
  if (s.kind === "proxmox") return <span>{t("sec.n_rules", { count: Number(m.rules ?? 0) })}<div className="text-muted">{m.cluster_enabled === false ? t("sec.pve_off") : (m.status as string) ?? ""}</div></span>;
  if (s.kind === "unifi") return <span>{t("sec.n_policies", { count: Number(m.rules ?? 0), zones: Number(m.zones ?? 0) })}</span>;
  return <span>{t("sec.n_rules", { count: Number(m.rules ?? 0) })}<div className="text-muted">{String(m.tool ?? "")}{m.ufw ? ` · ${m.ufw}` : ""}</div></span>;
}

function ChangeDialog({ id, admin, onClose }: { id: string; admin: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const [note, setNote] = useState("");
  const [full, setFull] = useState(false);
  const q = useQuery({ queryKey: ["netsec", "fw-version", id], queryFn: () => get<FwChange>(`api/netsec/firewall/versions/${id}`) });
  const ack = useMutation({
    mutationFn: () => post("api/netsec/firewall/ack", { version_id: id, note: note || null }),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const v = q.data;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={v ? v.source_label : t("sec.change")} description={v ? formatDate(v.taken_at, i18n.language) : undefined}
      footer={admin && v && !v.acknowledged_at ? <>
        <Input className="mr-auto max-w-md" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("sec.ack_note_ph")} maxLength={500} />
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={ack.isPending} onClick={() => ack.mutate()}><CheckCheck /> {t("sec.ack")}</Button>
      </> : undefined}>
      {q.isLoading || !v ? <Spinner /> : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-mono text-online">+{v.added}</span><span className="font-mono text-offline">−{v.removed}</span>
            {v.made_by && <Badge tone="info">{t("sec.made_by", { who: v.made_by })}</Badge>}
            {v.acknowledged_at && <Badge tone="online">{t("sec.reviewed_by", { who: v.acknowledged_by === "stackpilot" ? t("sec.auto_ack") : v.acknowledged_by, ago: timeAgo(v.acknowledged_at, i18n.language) })}</Badge>}
            <div className="ml-auto"><Switch checked={full} onCheckedChange={setFull} label={t("sec.show_full")} /></div>
          </div>
          {v.note && <Alert tone="info">{v.note}</Alert>}
          {full ? <pre className="max-h-[60vh] overflow-auto rounded-md bg-nacht p-3 text-[12px] leading-5 text-[#e8edf7]">{v.content}</pre>
            : <DiffView diff={v.diff ?? ""} className="max-h-[60vh]" />}
        </div>
      )}
    </Dialog>
  );
}

function CurrentDialog({ sourceId, onClose }: { sourceId: string; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const q = useQuery({ queryKey: ["netsec", "fw-current", sourceId], queryFn: () => get<FwChange>(`api/netsec/firewall/sources/${sourceId}/current`) });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={q.data?.source_label ?? t("sec.current")} description={q.data ? t("sec.current_since", { date: formatDate(q.data.taken_at, i18n.language) }) : undefined}>
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="neutral">{t("sec.no_version")}</Alert> : <pre className="max-h-[65vh] overflow-auto rounded-md bg-nacht p-3 text-[12px] leading-5 text-[#e8edf7]">{q.data?.content}</pre>}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ VPN */
function VpnTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const q = useQuery({
    queryKey: ["netsec", "vpn", customerId],
    queryFn: () => get<{ tunnels: Tunnel[]; counts: Record<VpnStatus, number>; uptime_checks: VpnCheck[] }>(qs("api/netsec/vpn", { customer_id: customerId })),
    refetchInterval: 60_000,
  });
  const watch = useMutation({
    mutationFn: ({ id, watched }: { id: string; watched: boolean | null }) => patch(`api/netsec/vpn/${id}`, { watched }),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const forget = useMutation({
    mutationFn: (id: string) => del(`api/netsec/vpn/${id}`),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rows = q.data?.tunnels ?? [];
  const checks = q.data?.uptime_checks ?? [];
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Waypoints />} title={t("sec.vpn_title")} description={t("sec.vpn_desc")} />
        {q.isLoading ? <div className="p-5"><Spinner /></div> : !rows.length ? <EmptyState icon={<Waypoints />} title={t("sec.no_vpn")}>{t("sec.no_vpn_hint")}</EmptyState> : (
          <Table>
            <thead><Tr><Th>{t("sec.col_tunnel")}</Th><Th>{t("sec.col_on")}</Th><Th>{t("sec.col_status")}</Th><Th>{t("sec.col_watch")}</Th>{admin && <Th />}</Tr></thead>
            <tbody>
              {rows.map((x) => (
                <Tr key={x.id}>
                  <Td>
                    <div className="font-semibold">{x.name}</div>
                    <div className="text-xs text-muted">{t(`sec.vk.${x.kind}`, { defaultValue: x.kind })}{x.remote ? ` · ${x.remote}` : ""}</div>
                    <VpnDetail x={x} />
                  </Td>
                  <Td className="text-xs">{x.node_name ?? x.connection_name ?? "–"}{!customerId && x.customer_name && <div className="text-muted">{x.customer_name}</div>}</Td>
                  <Td className="text-xs">
                    <Badge tone={VPN_TONE[x.status]} dot>{t(`sec.vs.${x.status}`)}</Badge>
                    <div className="mt-1 text-muted">{x.status_since ? t("sec.since", { ago: timeAgo(x.status_since, i18n.language) }) : ""}</div>
                    {x.changes_24h >= 4 && <div className="text-warning">{t("sec.flapping", { count: x.changes_24h })}</div>}
                  </Td>
                  <Td className="text-xs">
                    {admin ? (
                      <Select className="h-8 w-auto text-xs" value={x.watched == null ? "auto" : x.watched ? "on" : "off"}
                        onChange={(e) => watch.mutate({ id: x.id, watched: e.target.value === "auto" ? null : e.target.value === "on" })}>
                        <option value="auto">{t("sec.watch_auto", { state: x.watched == null ? (x.watching ? t("sec.watch_yes") : t("sec.watch_no")) : "" })}</option>
                        <option value="on">{t("sec.watch_on")}</option>
                        <option value="off">{t("sec.watch_off")}</option>
                      </Select>
                    ) : x.watching ? <Badge tone="info">{t("sec.watch_on")}</Badge> : <span className="text-muted">{t("sec.watch_off")}</span>}
                  </Td>
                  {admin && <Td className="text-right">{(x.status === "missing" || x.status === "unknown") && <Button size="sm" variant="ghost" title={t("sec.forget")} onClick={() => forget.mutate(x.id)}><Trash2 /></Button>}</Td>}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {checks.length > 0 && (
        <Card>
          <CardHeader icon={<Globe />} title={t("sec.vpn_checks_title")} description={t("sec.vpn_checks_desc")} />
          <Table>
            <thead><Tr><Th>{t("sec.col_check")}</Th><Th>{t("sec.col_address")}</Th><Th>{t("sec.col_status")}</Th></Tr></thead>
            <tbody>
              {checks.map((c) => (
                <Tr key={c.id}>
                  <Td><div className="font-semibold">{c.name}</div>{!customerId && <div className="text-xs text-muted">{c.customer_name}</div>}</Td>
                  <Td className="text-xs">{c.target}{c.port ? `:${c.port}` : ""} · {t(`sec.vpn_mode.${c.mode}`, { defaultValue: c.mode })}</Td>
                  <Td className="text-xs"><Badge tone={c.status === "up" ? "online" : c.status === "down" ? "offline" : "neutral"} dot>{t(`sec.us.${c.status}`, { defaultValue: c.status })}</Badge>{c.last_error && <div className="text-offline">{c.last_error}</div>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}

function VpnDetail({ x }: { x: Tunnel }) {
  const { t } = useTranslation();
  const d = x.detail as Record<string, unknown>;
  if (x.kind === "wireguard") {
    const age = d.handshake_age as number | null;
    return <div className="text-xs text-muted">{age == null ? t("sec.wg_never") : t("sec.wg_handshake", { s: age })}{d.keepalive ? ` · keepalive ${d.keepalive}s` : ""}</div>;
  }
  if (x.kind === "ipsec") {
    const ch = (d.children as string[] | undefined) ?? [];
    return ch.length ? <div className="text-xs text-muted">{ch.join(", ")}</div> : null;
  }
  return null;
}

/* ------------------------------------------------------------------ settings */
function SettingsTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["netsec", "settings"], queryFn: () => get<Settings>("api/netsec/settings") });
  const [f, setF] = useState<Settings | null>(null);
  const [paths, setPaths] = useState("");
  const [ignore, setIgnore] = useState("");
  useEffect(() => {
    if (q.data && !f) {
      setF(q.data);
      setPaths(q.data.extra_paths.join("\n"));
      setIgnore(q.data.fw_ignore.join("\n"));
    }
  }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => put<Settings>("api/netsec/settings", { ...f, extra_paths: lines(paths), fw_ignore: lines(ignore) }),
    onSuccess: (r) => { toast.success(t("sec.settings_saved")); setF(r); qc.invalidateQueries({ queryKey: ["netsec"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const num = (k: keyof Settings) => ({ value: String(f?.[k] ?? ""), onChange: (e: { target: { value: string } }) => f && setF({ ...f, [k]: Number(e.target.value) }) });
  const sw = (k: keyof Settings, label: string, desc?: string) => <Switch checked={!!f?.[k]} onCheckedChange={(v) => f && setF({ ...f, [k]: v })} label={label} description={desc} />;
  const sections = f && [
    { icon: <Award />, title: t("sec.set_certs"), body: (
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label={t("sec.set_cert_interval")}><Input type="number" min={1} max={168} {...num("cert_interval_hours")} /></Field>
        <Field label={t("sec.set_warn")}><Input type="number" min={1} max={180} {...num("cert_warn_days")} /></Field>
        <Field label={t("sec.set_critical")}><Input type="number" min={0} max={60} {...num("cert_critical_days")} /></Field>
        <div className="space-y-3 sm:col-span-3">
          {sw("alert_certs", t("sec.set_alert_certs"))}
          {sw("alert_untrusted", t("sec.set_alert_untrusted"))}
          {sw("uptime_endpoints", t("sec.set_uptime"), t("sec.set_uptime_hint"))}
        </div>
        <Field label={t("sec.set_paths")} hint={t("sec.set_paths_hint")} className="sm:col-span-3"><Textarea rows={3} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="/opt/app/tls/*.pem" className="font-mono text-xs" /></Field>
      </div>) },
    { icon: <Flame />, title: t("sec.set_fw"), body: (
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("sec.set_fw_interval")}><Input type="number" min={5} max={1440} {...num("fw_interval_minutes")} /></Field>
        <Field label={t("sec.set_keep")}><Input type="number" min={5} max={500} {...num("fw_keep_versions")} /></Field>
        <div className="space-y-3 sm:col-span-2">
          {sw("alert_fw_changes", t("sec.set_alert_fw"))}
          {sw("auto_ack_own", t("sec.set_auto_ack"), t("sec.set_auto_ack_hint"))}
        </div>
        <Field label={t("sec.set_ignore")} hint={t("sec.set_ignore_hint")} className="sm:col-span-2"><Textarea rows={4} value={ignore} onChange={(e) => setIgnore(e.target.value)} className="font-mono text-xs" /></Field>
      </div>) },
    { icon: <Waypoints />, title: t("sec.set_vpn"), body: (
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label={t("sec.set_vpn_interval")}><Input type="number" min={1} max={120} {...num("vpn_interval_minutes")} /></Field>
        <Field label={t("sec.set_down")}><Input type="number" min={1} max={1440} {...num("vpn_down_minutes")} /></Field>
        <Field label={t("sec.set_wg")} hint={t("sec.set_wg_hint")}><Input type="number" min={150} max={3600} {...num("wg_handshake_seconds")} /></Field>
        <div className="sm:col-span-3">{sw("alert_vpn", t("sec.set_alert_vpn"))}</div>
      </div>) },
  ];
  if (q.isLoading || !f || !sections) return <Spinner />;
  return (
    <div className="space-y-5">
      {sections.map((s) => <Card key={s.title}><CardHeader icon={s.icon} title={s.title} /><CardBody>{s.body}</CardBody></Card>)}
      <div className="flex justify-end"><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}><Settings2 /> {t("common.save")}</Button></div>
    </div>
  );
}

function lines(text: string): string[] {
  return text.split("\n").map((x) => x.trim()).filter(Boolean);
}
