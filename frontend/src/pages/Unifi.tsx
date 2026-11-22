import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowRight, ArrowUpCircle, Bot, Cable, Cloud, DoorClosed, DoorOpen, Fingerprint, KeyRound, Lock, LockOpen,
  MessageSquare, Pencil, Plug, Plus, Power, RefreshCw, Router, Search, ShieldCheck, Siren, Stethoscope, Trash2, Users,
  Wifi, Zap,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { tabCls, TypeToConfirm } from "@/components/net";
import { useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, patch, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  NodeInfo, UnifiAccessConn, UnifiClient, UnifiConnection, UnifiDevice, UnifiDeviceDetail, UnifiDiagnosis, UnifiDoor, UnifiEvent,
  UnifiIsolation, UnifiKind, UnifiLogRow, UnifiOverview,
} from "@/lib/types";
import { cn, formatDate, formatDuration, timeAgo } from "@/lib/utils";

const useConnections = () => useQuery({ queryKey: ["unifi-connections"], queryFn: () => get<UnifiConnection[]>("api/unifi/connections"), refetchInterval: 60_000 });

function useIsAdmin() {
  const { user } = useAuth();
  return user?.role === "admin";
}

/* ------------------------------------------------------------------ small pieces */
const TYPE_LABEL: Record<string, string> = { gateway: "GW", switch: "SW", ap: "AP", other: "•" };

function TypeChip({ type }: { type: string }) {
  return (
    <span className={cn("inline-flex h-6 w-9 shrink-0 items-center justify-center rounded-md text-[11px] font-extrabold tracking-wide",
      type === "ap" ? "bg-info-soft text-ink" : type === "other" ? "bg-sunken text-muted" : "bg-nacht text-lucht dark:bg-lucht/20 dark:text-ink")}>
      {TYPE_LABEL[type] ?? "•"}
    </span>
  );
}

function StateBadge({ online, state }: { online: boolean; state?: string }) {
  const { t } = useTranslation();
  if (online) return <Badge tone="online" dot>{t("uf.online")}</Badge>;
  const s = (state ?? "").toUpperCase();
  if (["UPDATING", "GETTING_READY", "ADOPTING", "PENDING_ADOPTION"].includes(s)) return <Badge tone="warning" dot>{t(`uf.state_${s.toLowerCase()}`, { defaultValue: s })}</Badge>;
  return <Badge tone="offline" dot>{t("uf.offline")}</Badge>;
}

function ConnStatus({ c }: { c: UnifiConnection }) {
  const { t } = useTranslation();
  if (!c.enabled) return <Badge>{t("uf.disabled")}</Badge>;
  if (c.status === "ok") return <Badge tone="online" dot>{t("uf.status_ok")}</Badge>;
  if (c.status === "error") return <Badge tone="offline" dot>{t("uf.status_error")}</Badge>;
  return <Badge tone="neutral" dot>{t("uf.status_unknown")}</Badge>;
}

export function KindBadge({ kind }: { kind: UnifiKind }) {
  const { t } = useTranslation();
  const Icon = kind === "access" ? DoorClosed : kind === "site_manager" ? Cloud : Router;
  return <span className="inline-flex items-center gap-1.5 rounded-full bg-sunken px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-ink"><Icon className="size-3" />{t(`uf.kind_${kind}`)}</span>;
}

function SevDot({ s }: { s: string }) {
  return <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", s === "critical" ? "bg-offline" : s === "warning" ? "bg-warning" : "bg-online")} />;
}

function EventList({ events, empty }: { events: UnifiEvent[]; empty?: ReactNode }) {
  const { i18n } = useTranslation();
  if (!events.length) return <div className="px-5 py-6 text-sm text-muted">{empty}</div>;
  return (
    <ul className="divide-y divide-line">
      {events.map((e) => (
        <li key={e.id} className="flex gap-3 px-5 py-2.5 text-sm">
          <SevDot s={e.severity} />
          <div className="min-w-0 flex-1">
            <div className="break-words">{e.message}</div>
            <div className="text-xs text-muted">{[e.connection, e.site].filter(Boolean).join(" · ")}</div>
          </div>
          <div className="shrink-0 whitespace-nowrap text-xs text-muted" title={formatDate(e.at, i18n.language)}>{timeAgo(e.at, i18n.language)}</div>
        </li>
      ))}
    </ul>
  );
}

function AskClaude({ examples }: { examples: string[] }) {
  const { t } = useTranslation();
  return (
    <Card className="bg-nacht text-white dark:bg-raised">
      <CardBody className="space-y-3 py-5">
        <div className="flex items-center gap-2 text-lucht"><Bot className="size-5" /><span className="text-sm font-bold uppercase tracking-wider">{t("uf.ask_title")}</span></div>
        <div className="space-y-2">
          {examples.map((e) => (
            <div key={e} className="flex items-start gap-2 rounded-md bg-white/10 px-3 py-2 text-sm dark:bg-sunken">
              <MessageSquare className="mt-0.5 size-3.5 shrink-0 text-koraal" /><span className="flex-1 italic">“{e}”</span><CopyButton value={e} />
            </div>
          ))}
        </div>
        <p className="text-xs text-white/70 dark:text-muted">{t("uf.ask_note")}</p>
        <Link to="/help/capabilities" className="inline-flex items-center gap-1.5 text-sm font-semibold text-koraal hover:underline">{t("srv.claude_more")} <ArrowRight className="size-4" /></Link>
      </CardBody>
    </Card>
  );
}

function NoConnections({ kind }: { kind: "network" | "access" }) {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  return (
    <Card>
      <EmptyState icon={kind === "access" ? <DoorClosed /> : <Wifi />} title={t(`uf.empty_${kind}`)}
        action={admin && <Link to="/unifi/connections?add=1"><Button variant="primary"><Plus /> {t("uf.add_conn")}</Button></Link>}>
        {t(`uf.empty_${kind}_text`)}
      </EmptyState>
    </Card>
  );
}

/** Pick a site of a Network connection (connection + site in one select). */
function SitePicker({ conns, value, onChange }: { conns: UnifiConnection[]; value: string; onChange: (v: string) => void }) {
  const opts = conns.filter((c) => c.kind === "network").flatMap((c) => c.sites.map((s) => ({ value: `${c.id}|${s.id}`, label: `${c.name} · ${s.name}` })));
  useEffect(() => {
    if (!value && opts.length) onChange(opts[0].value);
  }, [value, opts, onChange]);
  return (
    <Select value={value} onChange={(e) => onChange(e.target.value)} className="h-9 w-auto max-w-xs py-0 text-[13px]">
      {opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </Select>
  );
}

/* ------------------------------------------------------------------ Network */
export function UnifiNetworkPage() {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const qc = useQueryClient();
  const [env, setEnv] = useState("");
  const [tab, setTab] = useState("devices");
  const [open, setOpen] = useState<UnifiDevice | null>(null);
  const q = useQuery({
    queryKey: ["unifi-overview", env],
    queryFn: () => get<UnifiOverview>(`api/unifi/network/overview${env ? `?environment_id=${env}` : ""}`),
    refetchInterval: 30_000,
  });
  const refresh = useMutation({
    mutationFn: async () => { for (const c of q.data?.connections ?? []) { try { await post(`api/unifi/connections/${c.id}/refresh`); } catch { /* shown on the connection */ } } },
    onSettled: () => { qc.invalidateQueries({ queryKey: ["unifi-overview"] }); qc.invalidateQueries({ queryKey: ["unifi-connections"] }); },
  });
  const d = q.data;
  const tot = d?.totals;
  return (
    <>
      <PageHeader title={t("uf.net_title")} description={t("uf.net_subtitle")}
        actions={<>
          <Select value={env} onChange={(e) => setEnv(e.target.value)} className="h-9 w-auto py-0 text-[13px]">
            <option value="">{t("srv.all_envs")}</option>
            {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
          </Select>
          <Button onClick={() => refresh.mutate()} loading={refresh.isPending}><RefreshCw /> {t("uf.refresh")}</Button>
        </>} />
      {q.isLoading ? <Spinner /> : !d?.connections.length ? <NoConnections kind="network" /> : (
        <>
          {tot && tot.errors > 0 && (
            <Alert tone="warning" className="mb-4" title={t("uf.conn_errors", { count: tot.errors })}>
              {d.connections.filter((c) => c.status === "error").map((c) => `${c.name}: ${c.last_error ?? ""}`).join("\n")}
            </Alert>
          )}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label={t("uf.k_devices")} value={tot ? `${tot.devices - tot.offline}/${tot.devices}` : "…"} hint={t("uf.k_devices_hint")} icon={<Router />} tone="info" />
            <Stat label={t("uf.k_offline")} value={tot?.offline ?? "…"} hint={t("uf.k_offline_hint")} icon={<AlertTriangle />} tone={tot?.offline ? "offline" : "online"} />
            <Stat label={t("uf.k_firmware")} value={tot?.firmware ?? "…"} hint={t("uf.k_firmware_hint")} icon={<ArrowUpCircle />} tone={tot?.firmware ? "accent" : "online"} />
            <Stat label={t("uf.k_clients")} value={tot?.clients ?? "…"} hint={t("uf.k_clients_hint", { count: d.sites.length })} icon={<Users />} tone="info" />
          </div>

          <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="min-w-0">
              <Tabs.Root value={tab} onValueChange={setTab}>
                <Tabs.List className="mb-4 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
                  <Tabs.Trigger value="devices" className={tabCls}>{t("uf.tab_devices")}</Tabs.Trigger>
                  <Tabs.Trigger value="sites" className={tabCls}>{t("uf.tab_sites")}</Tabs.Trigger>
                  <Tabs.Trigger value="clients" className={tabCls}>{t("uf.tab_clients")}</Tabs.Trigger>
                  <Tabs.Trigger value="wifi" className={tabCls}>WiFi</Tabs.Trigger>
                  <Tabs.Trigger value="checks" className={tabCls}>{t("uf.tab_checks")}</Tabs.Trigger>
                </Tabs.List>
                <Tabs.Content value="devices"><DevicesCard devices={d.devices} onOpen={setOpen} /></Tabs.Content>
                <Tabs.Content value="sites"><SitesCard d={d} /></Tabs.Content>
                <Tabs.Content value="clients"><ClientsCard conns={d.connections} /></Tabs.Content>
                <Tabs.Content value="wifi"><WifiCard d={d} /></Tabs.Content>
                <Tabs.Content value="checks"><ChecksCard conns={d.connections} /></Tabs.Content>
              </Tabs.Root>
            </div>
            <div className="space-y-6">
              <Card>
                <CardHeader icon={<Zap />} title={t("uf.events")} description={t("uf.events_desc")} />
                <EventList events={d.events.slice(0, 12)} empty={t("uf.events_empty")} />
              </Card>
              <AskClaude examples={[t("uf.ask_n1"), t("uf.ask_n2"), t("uf.ask_n3"), t("uf.ask_n4")]} />
            </div>
          </div>
        </>
      )}
      {open && <DeviceDialog dev={open} conn={d?.connections.find((c) => c.id === open.connection_id)} onClose={() => setOpen(null)} />}
    </>
  );
}

function DevicesCard({ devices, onOpen }: { devices: UnifiDevice[]; onOpen: (d: UnifiDevice) => void }) {
  const { t } = useTranslation();
  const [type, setType] = useState("all");
  const [qs, setQs] = useState("");
  const rows = devices.filter((d) => (type === "all" || d.type === type) && (!qs || `${d.name} ${d.model} ${d.ip} ${d.mac} ${d.site}`.toLowerCase().includes(qs.toLowerCase())));
  return (
    <Card className="min-w-0">
      <CardHeader icon={<Router />} title={t("uf.devices")} description={t("uf.devices_desc")}
        action={<Segmented value={type} onChange={setType} options={[{ value: "all", label: t("srv.all") }, { value: "gateway", label: "GW" }, { value: "switch", label: "SW" }, { value: "ap", label: "AP" }]} />} />
      <div className="border-b border-line px-5 py-3">
        <div className="relative max-w-sm"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input value={qs} onChange={(e) => setQs(e.target.value)} placeholder={t("uf.search_devices")} className="pl-9" /></div>
      </div>
      {!rows.length ? <div className="px-5 py-8 text-center text-sm text-muted">{t("uf.no_devices")}</div> : (
        <div className="overflow-x-auto">
          <Table>
            <thead><tr><Th>{t("uf.col_device")}</Th><Th>{t("uf.col_site")}</Th><Th>{t("uf.col_status")}</Th><Th>{t("uf.col_clients")}</Th><Th>{t("uf.col_uplink")}</Th><Th>Firmware</Th></tr></thead>
            <tbody>
              {rows.map((d) => (
                <Tr key={`${d.connection_id}-${d.id}`} className={cn(d.source === "network" && "cursor-pointer")} onClick={() => d.source === "network" && onOpen(d)}>
                  <Td>
                    <div className="flex items-center gap-2.5"><TypeChip type={d.type} />
                      <div className="min-w-0"><div className="truncate font-bold">{d.name}</div><div className="truncate text-xs text-muted">{d.model}{d.ip ? ` · ${d.ip}` : ""}</div></div>
                    </div>
                  </Td>
                  <Td><div className="whitespace-nowrap text-sm">{d.site ?? "–"}</div><div className="whitespace-nowrap text-xs text-muted">{d.connection}</div></Td>
                  <Td><StateBadge online={d.online} state={d.state} /></Td>
                  <Td className="tabular-nums">{d.source === "network" ? d.clients : "–"}</Td>
                  <Td className="text-sm">{d.uplink_name ? <span className="whitespace-nowrap">{d.uplink_name}{d.uplink_port ? ` · ${t("uf.port")} ${d.uplink_port}` : ""}</span> : <span className="text-muted">–</span>}</Td>
                  <Td>
                    <span className="mono text-xs">{d.firmware ?? "–"}</span>
                    {d.firmware_updatable && <Badge tone="warning" className="ml-1.5 whitespace-nowrap">{t("uf.fw_update")}</Badge>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

function SitesCard({ d }: { d: UnifiOverview }) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {d.sites.map((s) => (
        <Card key={`${s.connection_id}-${s.site_id}`}>
          <CardBody className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0"><div className="truncate font-bold">{s.site}</div><div className="truncate text-xs text-muted">{s.connection}{s.host ? ` · ${s.host}` : ""}{s.environment ? ` · ${s.environment}` : ""}</div></div>
              {s.cloud ? <KindBadge kind="site_manager" /> : <KindBadge kind="network" />}
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="rounded-md bg-sunken p-2"><div className="display text-xl">{s.devices ?? "–"}</div><div className="text-[11px] text-muted">{t("uf.devices_l")}</div></div>
              <div className="rounded-md bg-sunken p-2"><div className={cn("display text-xl", s.offline ? "text-offline" : "")}>{s.offline ?? "–"}</div><div className="text-[11px] text-muted">{t("uf.offline_l")}</div></div>
              <div className="rounded-md bg-sunken p-2"><div className="display text-xl">{s.clients?.total ?? "–"}</div><div className="text-[11px] text-muted">{t("uf.clients_l")}</div></div>
            </div>
            <div className="text-xs text-muted">
              {t("uf.clients_split", { wifi: s.clients?.wireless ?? 0, wired: s.clients?.wired ?? 0, guest: s.clients?.guest ?? 0 })}
              {s.isp && ` · ISP ${s.isp}`}{s.wan_uptime != null && ` · WAN ${s.wan_uptime}%`}
            </div>
            {!!s.wans?.length && (
              <div className="flex flex-wrap gap-1.5">{s.wans.map((w) => <Badge key={w.id} tone="info"><Cable className="size-3" />{w.name} {Object.values(w.status).map(String).join(" ")}</Badge>)}</div>
            )}
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

function ClientsCard({ conns }: { conns: UnifiConnection[] }) {
  const { t, i18n } = useTranslation();
  const admin = useIsAdmin();
  const [site, setSite] = useState("");
  const [qs, setQs] = useState("");
  const [cid, sid] = site.split("|");
  const conn = conns.find((c) => c.id === cid);
  const q = useQuery({ queryKey: ["unifi-clients", site], queryFn: () => get<UnifiClient[]>(`api/unifi/network/${cid}/sites/${sid}/clients`), enabled: !!sid, refetchInterval: 60_000 });
  const qc = useQueryClient();
  const guest = useMutation({
    mutationFn: (v: { id: string; authorize: boolean }) => post(`api/unifi/network/${cid}/sites/${sid}/clients/${v.id}/guest`, { authorize: v.authorize }),
    onSuccess: () => { toast.success(t("uf.done")); qc.invalidateQueries({ queryKey: ["unifi-clients"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rows = (q.data ?? []).filter((c) => !qs || `${c.name} ${c.mac} ${c.ip} ${c.uplink_name}`.toLowerCase().includes(qs.toLowerCase()));
  if (!conns.some((c) => c.kind === "network")) return <Card><CardBody className="text-sm text-muted">{t("uf.cloud_only")}</CardBody></Card>;
  return (
    <Card className="min-w-0">
      <CardHeader icon={<Users />} title={t("uf.clients")} description={t("uf.clients_desc")} action={<SitePicker conns={conns} value={site} onChange={setSite} />} />
      <div className="border-b border-line px-5 py-3">
        <div className="relative max-w-sm"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input value={qs} onChange={(e) => setQs(e.target.value)} placeholder={t("uf.search_clients")} className="pl-9" /></div>
      </div>
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : (
        <div className="overflow-x-auto">
          <Table>
            <thead><tr><Th>{t("uf.col_client")}</Th><Th>IP</Th><Th>{t("uf.col_conn")}</Th><Th>{t("uf.col_via")}</Th><Th>{t("uf.col_since")}</Th><Th /></tr></thead>
            <tbody>
              {rows.map((c) => (
                <Tr key={c.id}>
                  <Td><div className="font-bold">{c.name ?? "–"}</div><div className="mono text-xs text-muted">{c.mac}</div></Td>
                  <Td className="mono text-xs">{c.ip ?? <Badge tone="warning">{t("uf.no_ip")}</Badge>}</Td>
                  <Td><Badge tone={c.type === "WIRELESS" ? "info" : "neutral"}>{c.type === "WIRELESS" ? "WiFi" : c.type === "WIRED" ? t("uf.wired") : c.type}</Badge>{c.access === "GUEST" && <Badge tone="accent" className="ml-1">{t("uf.guest")}</Badge>}</Td>
                  <Td className="text-sm">{c.uplink_name ?? "–"}</Td>
                  <Td className="whitespace-nowrap text-xs text-muted">{c.connected_at ? timeAgo(c.connected_at, i18n.language) : "–"}</Td>
                  <Td className="text-right">
                    {admin && !conn?.read_only && c.access === "GUEST" && (
                      <Button size="sm" onClick={() => guest.mutate({ id: c.id, authorize: !c.authorized })}>{c.authorized ? t("uf.guest_revoke") : t("uf.guest_authorize")}</Button>
                    )}
                  </Td>
                </Tr>
              ))}
              {!rows.length && <tr><td colSpan={6} className="px-5 py-8 text-center text-sm text-muted">{t("uf.no_clients")}</td></tr>}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

function WifiCard({ d }: { d: UnifiOverview }) {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<{ cid: string; sid: string; id: string; name: string; enabled: boolean } | null>(null);
  const toggle = useMutation({
    mutationFn: (v: NonNullable<typeof confirm>) => post(`api/unifi/network/${v.cid}/sites/${v.sid}/wifi/${v.id}/enabled`, { enabled: v.enabled }),
    onSuccess: async (_r, v) => { toast.success(t("uf.done")); setConfirm(null); await post(`api/unifi/connections/${v.cid}/refresh`).catch(() => null); qc.invalidateQueries({ queryKey: ["unifi-overview"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const ro = (cid: string) => d.connections.find((c) => c.id === cid)?.read_only;
  if (!d.wifi.length) return <Card><CardBody className="text-sm text-muted">{t("uf.no_wifi")}</CardBody></Card>;
  return (
    <Card className="min-w-0">
      <CardHeader icon={<Wifi />} title={t("uf.wifi")} description={t("uf.wifi_desc")} />
      <div className="overflow-x-auto">
        <Table>
          <thead><tr><Th>SSID</Th><Th>{t("uf.col_site")}</Th><Th>{t("uf.col_security")}</Th><Th>{t("uf.col_network")}</Th><Th>{t("uf.col_flags")}</Th><Th>{t("uf.col_enabled")}</Th></tr></thead>
          <tbody>
            {d.wifi.map((w) => (
              <Tr key={`${w.connection_id}-${w.site_id}-${w.id}`}>
                <Td><div className="flex items-center gap-2 font-bold"><Wifi className="size-4 text-muted" />{w.name}</div></Td>
                <Td><div className="text-sm">{w.site}</div><div className="text-xs text-muted">{w.connection}</div></Td>
                <Td className="text-xs">{(w.security ?? "–").replaceAll("_", " ").replace("PERSONAL", "Personal").replace("ENTERPRISE", "Enterprise")}</Td>
                <Td className="text-sm">{w.network_name ?? "–"}{w.vlan != null && <Badge tone="info" className="ml-1.5">VLAN {w.vlan}</Badge>}</Td>
                <Td><div className="flex flex-wrap gap-1">
                  {w.guest && <Badge tone="accent">{t("uf.guest")}</Badge>}
                  {w.client_isolation && <Badge tone="online">{t("uf.isolation")}</Badge>}
                  {w.hidden && <Badge>{t("uf.hidden")}</Badge>}
                  {w.mac_filter && <Badge tone="warning">MAC {w.mac_filter.action?.toLowerCase()}</Badge>}
                  {w.blackout_days > 0 && <Badge>{t("uf.blackout")}</Badge>}
                </div></Td>
                <Td>
                  {admin && !ro(w.connection_id)
                    ? <Switch checked={w.enabled} onCheckedChange={(v) => setConfirm({ cid: w.connection_id, sid: w.site_id, id: w.id, name: w.name, enabled: v })} />
                    : w.enabled ? <Badge tone="online">{t("uf.on")}</Badge> : <Badge>{t("uf.off")}</Badge>}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </div>
      {confirm && (
        <TypeToConfirm open onOpenChange={(o) => !o && setConfirm(null)} word={confirm.name} danger={!confirm.enabled} loading={toggle.isPending}
          title={confirm.enabled ? t("uf.wifi_on_title", { name: confirm.name }) : t("uf.wifi_off_title", { name: confirm.name })}
          action={confirm.enabled ? t("uf.on") : t("uf.off")} onConfirm={() => toggle.mutate(confirm)}>
          {!confirm.enabled && <Alert tone="warning">{t("uf.wifi_off_warn")}</Alert>}
        </TypeToConfirm>
      )}
    </Card>
  );
}

function ChecksCard({ conns }: { conns: UnifiConnection[] }) {
  const { t } = useTranslation();
  const [site, setSite] = useState("");
  const [qs, setQs] = useState("");
  const [cid, sid] = site.split("|");
  const iso = useMutation({ mutationFn: () => get<UnifiIsolation>(`api/unifi/network/${cid}/sites/${sid}/guest-isolation`) });
  const diag = useMutation({ mutationFn: () => get<UnifiDiagnosis>(`api/unifi/network/${cid}/sites/${sid}/diagnose-client?q=${encodeURIComponent(qs)}`) });
  useEffect(() => { iso.reset(); diag.reset(); }, [site]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!conns.some((c) => c.kind === "network")) return <Card><CardBody className="text-sm text-muted">{t("uf.cloud_only")}</CardBody></Card>;
  const vTone = (v: string) => (v === "isolated" ? "online" : v === "partial" || v === "unknown" ? "warning" : v === "open" ? "offline" : "neutral");
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2"><span className="text-sm font-semibold text-muted">{t("uf.col_site")}</span><SitePicker conns={conns} value={site} onChange={setSite} /></div>
      <Card>
        <CardHeader icon={<Stethoscope />} title={t("uf.diag_title")} description={t("uf.diag_desc")} />
        <CardBody className="space-y-4">
          <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (qs.trim() && sid) diag.mutate(); }}>
            <Input value={qs} onChange={(e) => setQs(e.target.value)} placeholder={t("uf.diag_ph")} className="max-w-sm" />
            <Button type="submit" variant="dark" loading={diag.isPending} disabled={!qs.trim() || !sid}><Search /> {t("uf.diag_run")}</Button>
          </form>
          {diag.isError && <Alert tone="offline">{errorMessage(diag.error, t)}</Alert>}
          {diag.data && (
            <div className="space-y-3">
              <ul className="space-y-1.5">{diag.data.findings.map((f, i) => <li key={i} className="flex gap-2.5 text-sm"><SevDot s={f.severity === "info" ? "info" : f.severity} /><span>{f.text}</span></li>)}</ul>
              {!!diag.data.ssids.length && (
                <div className="grid gap-2 sm:grid-cols-2">
                  {diag.data.ssids.map((s) => (
                    <div key={s.name} className="rounded-md border border-line p-3 text-sm">
                      <div className="flex items-center gap-2 font-bold"><Wifi className="size-4 text-muted" />{s.name}{!s.enabled && <Badge>{t("uf.off")}</Badge>}</div>
                      <div className="text-xs text-muted">{s.security}{s.vlan != null ? ` · VLAN ${s.vlan}` : ""}</div>
                      {s.notes.map((n) => <div key={n} className="mt-1 text-xs">• {n}</div>)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<ShieldCheck />} title={t("uf.iso_title")} description={t("uf.iso_desc")}
          action={<Button variant="dark" onClick={() => iso.mutate()} loading={iso.isPending} disabled={!sid}>{t("uf.iso_run")}</Button>} />
        {(iso.data || iso.isError) && (
          <CardBody className="space-y-3">
            {iso.isError && <Alert tone="offline">{errorMessage(iso.error, t)}</Alert>}
            {iso.data && <>
              <div className="flex items-center gap-2"><Badge tone={vTone(iso.data.verdict)} dot>{t(`uf.iso_${iso.data.verdict}`)}</Badge></div>
              {iso.data.guest_networks.map((g) => (
                <div key={g.network} className="rounded-md border border-line">
                  <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5 text-sm font-bold">{g.network}{g.vlan != null && <Badge tone="info">VLAN {g.vlan}</Badge>}{g.zone && <Badge>{t("uf.zone")} {g.zone}</Badge>}
                    {g.ssids.map((s) => <Badge key={s.name} tone={s.client_isolation ? "online" : "warning"}>{s.name}: {s.client_isolation ? t("uf.isolation") : t("uf.no_isolation")}</Badge>)}
                  </div>
                  <ul className="divide-y divide-line">
                    {g.to.map((r) => (
                      <li key={r.network} className="flex flex-wrap items-start gap-3 px-4 py-2 text-sm">
                        <span className="w-40 shrink-0 font-semibold">→ {r.network}{r.vlan != null ? ` (VLAN ${r.vlan})` : ""}</span>
                        <Badge tone={vTone(r.verdict)}>{t(`uf.iso_${r.verdict}`)}</Badge>
                        <span className="min-w-0 flex-1 text-muted">{r.why}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              <p className="text-xs text-muted">{t("uf.iso_note")}</p>
            </>}
          </CardBody>
        )}
      </Card>
    </div>
  );
}

function DeviceDialog({ dev, conn, onClose }: { dev: UnifiDevice; conn?: UnifiConnection; onClose: () => void }) {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  const sid = dev.site_id ?? "";
  const q = useQuery({ queryKey: ["unifi-device", dev.connection_id, sid, dev.id], queryFn: () => get<UnifiDeviceDetail>(`api/unifi/network/${dev.connection_id}/sites/${sid}/devices/${dev.id}`) });
  const [ask, setAsk] = useState<null | { kind: "restart" } | { kind: "port"; idx: number }>(null);
  const act = useMutation({
    mutationFn: (a: NonNullable<typeof ask>) => a.kind === "restart"
      ? post(`api/unifi/network/${dev.connection_id}/sites/${sid}/devices/${dev.id}/restart`, {})
      : post(`api/unifi/network/${dev.connection_id}/sites/${sid}/devices/${dev.id}/ports/${a.idx}/power-cycle`, {}),
    onSuccess: () => { toast.success(t("uf.done")); setAsk(null); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const x = q.data;
  const canAct = admin && conn && !conn.read_only;
  const bps = (v?: number | null) => (v == null ? "–" : v > 1e6 ? `${(v / 1e6).toFixed(1)} Mbit/s` : `${Math.round(v / 1e3)} kbit/s`);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} xl title={<span className="flex items-center gap-2.5"><TypeChip type={dev.type} />{dev.name}</span>}
      description={`${dev.model ?? ""} · ${dev.site ?? ""} · ${dev.connection}`}
      footer={<>
        {canAct && <Button variant="danger" onClick={() => setAsk({ kind: "restart" })}><Power /> {t("uf.restart")}</Button>}
        <Button onClick={onClose}>{t("common.close")}</Button>
      </>}>
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : x && (
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-4">
            <Stat label={t("uf.col_status")} value={<StateBadge online={x.online} state={x.state} />} hint={x.ip ?? undefined} />
            <Stat label="Firmware" value={<span className="mono text-lg">{x.firmware ?? "–"}</span>} hint={x.firmware_updatable ? t("uf.fw_update") : t("uf.fw_current")} />
            <Stat label={t("uf.uptime")} value={x.stats.uptime_sec != null ? formatDuration(x.stats.uptime_sec) : "–"} hint={`CPU ${x.stats.cpu_pct ?? "–"}% · RAM ${x.stats.mem_pct ?? "–"}%`} />
            <Stat label={t("uf.col_uplink")} value={<span className="text-lg">{x.uplink_name ?? "–"}</span>} hint={`↑ ${bps(x.stats.uplink_tx_bps)} · ↓ ${bps(x.stats.uplink_rx_bps)}`} />
          </div>
          {!!x.ports.length && (
            <div>
              <div className="mb-2 text-sm font-bold">{t("uf.ports")} <span className="font-normal text-muted">{x.ports_up}/{x.ports_total} {t("uf.up")}</span></div>
              <div className="grid grid-cols-4 gap-2 sm:grid-cols-8 lg:grid-cols-12">
                {x.ports.map((p) => {
                  const up = p.state?.toUpperCase() === "UP";
                  return (
                    <div key={p.idx} className={cn("rounded-md border p-1.5 text-center text-[11px]", up ? "border-online/40 bg-online-soft" : "border-line bg-sunken text-muted")}>
                      <div className="font-bold">{p.idx}</div>
                      <div>{up ? (p.speed_mbps ? (p.speed_mbps >= 1000 ? `${p.speed_mbps / 1000}G` : `${p.speed_mbps}M`) : "up") : "–"}</div>
                      {p.poe && <div className="text-[10px] font-semibold">PoE</div>}
                      {canAct && p.poe && <button type="button" className="mt-0.5 text-[10px] font-semibold text-accent-text hover:underline" onClick={() => setAsk({ kind: "port", idx: p.idx })}>{t("uf.cycle")}</button>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {!!x.radios.length && (
            <div className="flex flex-wrap gap-2">{x.radios.map((r, i) => {
              const st = x.stats.radios.find((s) => s.frequency_ghz === r.frequency_ghz);
              return <Badge key={i} tone="info">{r.frequency_ghz} GHz · ch {r.channel ?? "?"}{r.channel_width_mhz ? ` · ${r.channel_width_mhz} MHz` : ""}{st?.tx_retries_pct != null ? ` · ${t("uf.retries")} ${st.tx_retries_pct}%` : ""}</Badge>;
            })}</div>
          )}
          <div>
            <div className="mb-2 text-sm font-bold">{t("uf.clients")} ({x.client_list.length})</div>
            {x.client_list.length ? (
              <div className="max-h-64 overflow-auto rounded-md border border-line">
                <Table><tbody>{x.client_list.map((c) => <Tr key={c.id}><Td className="font-semibold">{c.name}</Td><Td className="mono text-xs">{c.ip ?? "–"}</Td><Td className="mono text-xs text-muted">{c.mac}</Td><Td>{c.type === "WIRELESS" ? "WiFi" : t("uf.wired")}</Td></Tr>)}</tbody></Table>
              </div>
            ) : <div className="text-sm text-muted">{t("uf.no_clients")}</div>}
          </div>
        </div>
      )}
      {ask && (
        <TypeToConfirm open onOpenChange={(o) => !o && setAsk(null)} loading={act.isPending}
          word={ask.kind === "restart" ? dev.name : `${dev.name} port ${ask.idx}`}
          title={ask.kind === "restart" ? t("uf.restart_title", { name: dev.name }) : t("uf.cycle_title", { name: dev.name, port: ask.idx })}
          action={ask.kind === "restart" ? t("uf.restart") : t("uf.cycle")} onConfirm={() => act.mutate(ask)}>
          <Alert tone="warning">{ask.kind === "restart" ? t(dev.type === "gateway" ? "uf.restart_gw_warn" : "uf.restart_warn") : t("uf.cycle_warn")}</Alert>
        </TypeToConfirm>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ Access */
export function UnifiAccessPage() {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const [env, setEnv] = useState("");
  const q = useQuery({ queryKey: ["unifi-access", env], queryFn: () => get<UnifiAccessConn[]>(`api/unifi/access/overview${env ? `?environment_id=${env}` : ""}`), refetchInterval: 30_000 });
  const conns = q.data ?? [];
  const doors = conns.flatMap((c) => c.doors);
  return (
    <>
      <PageHeader title={t("uf.acc_title")} description={t("uf.acc_subtitle")}
        actions={<Select value={env} onChange={(e) => setEnv(e.target.value)} className="h-9 w-auto py-0 text-[13px]">
          <option value="">{t("srv.all_envs")}</option>
          {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
        </Select>} />
      {q.isLoading ? <Spinner /> : !conns.length ? <NoConnections kind="access" /> : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Stat label={t("uf.k_doors")} value={doors.length} icon={<DoorClosed />} tone="info" />
            <Stat label={t("uf.k_unlocked")} value={doors.filter((d) => d.lock === "unlock").length} hint={t("uf.k_unlocked_hint")} icon={<LockOpen />} tone={doors.some((d) => d.lock === "unlock") ? "warning" : "online"} />
            <Stat label={t("uf.k_open")} value={doors.filter((d) => d.position === "open").length} hint={t("uf.k_open_hint")} icon={<DoorOpen />} tone={doors.some((d) => d.position === "open") ? "warning" : "online"} />
          </div>
          <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="min-w-0 space-y-6">
              {conns.map((c) => <DoorsCard key={c.id} c={c} />)}
              <AccessLogCard conns={conns} />
            </div>
            <div className="space-y-6">
              <AskClaude examples={[t("uf.ask_a1"), t("uf.ask_a2"), t("uf.ask_a3")]} />
            </div>
          </div>
        </>
      )}
    </>
  );
}

function DoorsCard({ c }: { c: UnifiAccessConn }) {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const canAct = admin && !c.read_only;
  const [unlock, setUnlock] = useState<UnifiDoor | null>(null);
  const [rule, setRule] = useState<UnifiDoor | null>(null);
  const [emergency, setEmergency] = useState<null | "lockdown" | "evacuation" | "normal">(null);
  const sched = useQuery({ queryKey: ["unifi-sched", c.id], queryFn: () => get<{ schedules: { id: string; name: string }[] }>(`api/unifi/access/${c.id}/schedules`), retry: false });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["unifi-access"] }); };
  const doUnlock = useMutation({
    mutationFn: (d: UnifiDoor) => post(`api/unifi/access/${c.id}/doors/${d.id}/unlock`, {}),
    onSuccess: () => { toast.success(t("uf.unlocked_ok")); setUnlock(null); get(`api/unifi/access/${c.id}/doors`).finally(refresh); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const doEmergency = useMutation({
    mutationFn: (v: { mode: string; confirm: string }) => put(`api/unifi/access/${c.id}/emergency`, { lockdown: v.mode === "lockdown", evacuation: v.mode === "evacuation", confirm: v.confirm }),
    onSuccess: () => { toast.success(t("uf.done")); setEmergency(null); get(`api/unifi/access/${c.id}/doors`).finally(refresh); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Card>
      <CardHeader icon={<DoorClosed />} title={c.name} description={[c.environment_name, c.base_url].filter(Boolean).join(" · ")}
        action={<><ConnStatus c={c} />{c.read_only && <Badge>{t("uf.read_only")}</Badge>}</>} />
      {c.status === "error" && <div className="px-5 pt-4"><Alert tone="offline">{c.last_error}</Alert></div>}
      <CardBody>
        {!c.doors.length ? <div className="text-sm text-muted">{t("uf.no_doors")}</div> : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {c.doors.map((d) => {
              const locked = d.lock !== "unlock";
              return (
                <div key={d.id} className="rounded-lg border border-line p-4">
                  <div className="flex items-start gap-3">
                    <div className={cn("grid size-10 shrink-0 place-items-center rounded-md", locked ? "bg-nacht text-lucht dark:bg-lucht/15" : "bg-warning-soft text-warning")}>{locked ? <Lock className="size-5" /> : <LockOpen className="size-5" />}</div>
                    <div className="min-w-0">
                      <div className="truncate font-bold">{d.name}</div>
                      <div className="truncate text-xs text-muted">{d.full_name}</div>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <Badge tone={locked ? "online" : "warning"}>{locked ? t("uf.locked") : t("uf.unlocked")}</Badge>
                    {d.position && <Badge tone={d.position === "open" ? "warning" : "neutral"}>{d.position === "open" ? t("uf.door_open") : t("uf.door_closed")}</Badge>}
                  </div>
                  {canAct && (
                    <div className="mt-3 flex gap-2">
                      <Button size="sm" variant="dark" onClick={() => setUnlock(d)}><LockOpen /> {t("uf.unlock")}</Button>
                      <Button size="sm" onClick={() => setRule(d)}>{t("uf.lock_rule")}</Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {!!sched.data?.schedules?.length && (
          <div className="mt-4 text-sm"><span className="font-semibold">{t("uf.schedules")}: </span><span className="text-muted">{sched.data.schedules.map((s) => s.name).join(", ")}</span></div>
        )}
        {canAct && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
            <span className="flex items-center gap-1.5 text-sm font-semibold"><Siren className="size-4 text-offline" />{t("uf.emergency")}</span>
            <Button size="sm" variant="danger" onClick={() => setEmergency("lockdown")}>{t("uf.lockdown")}</Button>
            <Button size="sm" onClick={() => setEmergency("evacuation")}>{t("uf.evacuation")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setEmergency("normal")}>{t("uf.emergency_off")}</Button>
          </div>
        )}
      </CardBody>
      {unlock && (
        <Dialog open onOpenChange={(o) => !o && setUnlock(null)} title={t("uf.unlock_title", { name: unlock.name })}
          footer={<><Button onClick={() => setUnlock(null)}>{t("common.cancel")}</Button><Button variant="primary" loading={doUnlock.isPending} onClick={() => doUnlock.mutate(unlock)}><LockOpen /> {t("uf.unlock")}</Button></>}>
          <p className="text-sm">{t("uf.unlock_text")}</p>
        </Dialog>
      )}
      {rule && <LockRuleDialog c={c} door={rule} onClose={() => { setRule(null); refresh(); }} />}
      {emergency && (
        <TypeToConfirm open onOpenChange={(o) => !o && setEmergency(null)} word={c.name} loading={doEmergency.isPending} danger={emergency !== "normal"}
          title={t(`uf.em_${emergency}_title`)} action={t(`uf.em_${emergency}_btn`)} onConfirm={(typed) => doEmergency.mutate({ mode: emergency, confirm: typed })}>
          <Alert tone="warning">{t(`uf.em_${emergency}_text`)}</Alert>
        </TypeToConfirm>
      )}
    </Card>
  );
}

function LockRuleDialog({ c, door, onClose }: { c: UnifiConnection; door: UnifiDoor; onClose: () => void }) {
  const { t } = useTranslation();
  const [type, setType] = useState<"custom" | "keep_unlock" | "keep_lock" | "lock_early" | "reset">("custom");
  const [minutes, setMinutes] = useState(15);
  const cur = useQuery({ queryKey: ["unifi-rule", c.id, door.id], queryFn: () => get<{ type?: string; ended_time?: number }>(`api/unifi/access/${c.id}/doors/${door.id}/lock-rule`), retry: false });
  const save = useMutation({
    mutationFn: () => put(`api/unifi/access/${c.id}/doors/${door.id}/lock-rule`, { type, interval: type === "custom" ? minutes : null }),
    onSuccess: () => { toast.success(t("uf.done")); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={t("uf.rule_title", { name: door.name })} description={cur.data?.type ? t("uf.rule_current", { rule: t(`uf.rule_${cur.data.type}`, { defaultValue: cur.data.type }) }) : undefined}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-3">
        {(["custom", "keep_unlock", "keep_lock", "lock_early", "reset"] as const).map((r) => (
          <label key={r} className="flex cursor-pointer items-start gap-3 rounded-md border border-line p-3 has-[:checked]:border-nacht dark:has-[:checked]:border-lucht">
            <input type="radio" name="rule" checked={type === r} onChange={() => setType(r)} className="mt-1" />
            <span><span className="block text-sm font-semibold">{t(`uf.rule_${r}`)}</span><span className="block text-xs text-muted">{t(`uf.rule_${r}_hint`)}</span></span>
          </label>
        ))}
        {type === "custom" && <Field label={t("uf.minutes")}><Input type="number" min={1} max={1440} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} className="max-w-32" /></Field>}
      </div>
    </Dialog>
  );
}

function AccessLogCard({ conns }: { conns: UnifiAccessConn[] }) {
  const { t, i18n } = useTranslation();
  const [cid, setCid] = useState(conns[0]?.id ?? "");
  const [hours, setHours] = useState("24");
  const [door, setDoor] = useState("");
  const [actor, setActor] = useState("");
  const [topic, setTopic] = useState("door_openings");
  const params = new URLSearchParams({ hours, topic });
  if (door) params.set("door", door);
  if (actor) params.set("actor", actor);
  const q = useQuery({ queryKey: ["unifi-log", cid, hours, door, actor, topic], queryFn: () => get<UnifiLogRow[]>(`api/unifi/access/${cid}/logs?${params}`), enabled: !!cid });
  const doors = conns.find((c) => c.id === cid)?.doors ?? [];
  return (
    <Card className="min-w-0">
      <CardHeader icon={<KeyRound />} title={t("uf.log_title")} description={t("uf.log_desc")}
        action={conns.length > 1 && <Select value={cid} onChange={(e) => setCid(e.target.value)} className="h-9 w-auto py-0 text-[13px]">{conns.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</Select>} />
      <div className="flex flex-wrap gap-2 border-b border-line px-5 py-3">
        <Select value={hours} onChange={(e) => setHours(e.target.value)} className="h-9 w-auto py-0 text-[13px]">
          {["12", "24", "72", "168", "720"].map((h) => <option key={h} value={h}>{t(`uf.h_${h}`)}</option>)}
        </Select>
        <Select value={door} onChange={(e) => setDoor(e.target.value)} className="h-9 w-auto py-0 text-[13px]">
          <option value="">{t("uf.all_doors")}</option>
          {doors.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </Select>
        <Select value={topic} onChange={(e) => setTopic(e.target.value)} className="h-9 w-auto py-0 text-[13px]">
          {["door_openings", "critical", "device_events", "admin_activity", "visitor", "all"].map((x) => <option key={x} value={x}>{t(`uf.topic_${x}`)}</option>)}
        </Select>
        <Input value={actor} onChange={(e) => setActor(e.target.value)} placeholder={t("uf.search_person")} className="h-9 max-w-56" />
      </div>
      {q.isLoading ? <CardBody><Spinner /></CardBody> : q.isError ? <CardBody><Alert tone="offline">{errorMessage(q.error, t)}</Alert></CardBody> : (
        <div className="overflow-x-auto">
          <Table>
            <thead><tr><Th>{t("uf.col_when")}</Th><Th>{t("uf.col_who")}</Th><Th>{t("uf.col_door")}</Th><Th>{t("uf.col_how")}</Th><Th>{t("uf.col_result")}</Th></tr></thead>
            <tbody>
              {(q.data ?? []).map((r, i) => (
                <Tr key={i}>
                  <Td className="whitespace-nowrap text-xs">{formatDate(r.at, i18n.language)}</Td>
                  <Td className="font-semibold">{r.actor ?? "–"}</Td>
                  <Td className="text-sm">{r.targets.filter((x) => x.type === "door").map((x) => x.name).join(", ") || r.targets[0]?.name || "–"}</Td>
                  <Td className="text-xs text-muted">{r.method ?? r.message ?? "–"}</Td>
                  <Td>{r.result ? <Badge tone={r.result === "ACCESS" ? "online" : r.result === "BLOCKED" ? "offline" : "neutral"}>{r.result === "ACCESS" ? t("uf.granted") : r.result === "BLOCKED" ? t("uf.blocked") : r.result}</Badge> : "–"}</Td>
                </Tr>
              ))}
              {!q.data?.length && <tr><td colSpan={5} className="px-5 py-8 text-center text-sm text-muted">{t("uf.no_log")}</td></tr>}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ Connections */
export function UnifiConnectionsPage() {
  const { t, i18n } = useTranslation();
  const admin = useIsAdmin();
  const q = useConnections();
  const qc = useQueryClient();
  const [edit, setEdit] = useState<UnifiConnection | "new" | null>(() => (new URLSearchParams(location.search).get("add") ? "new" : null));
  const [remove, setRemove] = useState<UnifiConnection | null>(null);
  const test = useMutation({
    mutationFn: (c: UnifiConnection) => post<{ ok: boolean; error?: string; version?: string | null; ms?: number; sites?: unknown[]; hosts?: unknown[]; doors?: number }>(`api/unifi/connections/${c.id}/test`),
    onSuccess: (r) => {
      if (r.ok) toast.success(t("uf.test_ok", { ms: r.ms ?? 0 }) + (r.version ? ` · Network ${r.version}` : ""));
      else toast.error(r.error ?? "error");
      qc.invalidateQueries({ queryKey: ["unifi-connections"] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const delM = useMutation({
    mutationFn: (c: UnifiConnection) => del(`api/unifi/connections/${c.id}`),
    onSuccess: () => { setRemove(null); qc.invalidateQueries({ queryKey: ["unifi-connections"] }); qc.invalidateQueries({ queryKey: ["unifi-overview"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const rows = q.data ?? [];
  return (
    <>
      <PageHeader title={t("uf.conn_title")} description={t("uf.conn_subtitle")}
        actions={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("uf.add_conn")}</Button>} />
      <div className="mb-6 grid gap-4 md:grid-cols-3">
        {(["network", "site_manager", "access"] as const).map((k) => (
          <Card key={k}><CardBody className="space-y-2">
            <KindBadge kind={k} />
            <div className="font-bold">{t(`uf.kind_${k}_name`)}</div>
            <p className="text-sm text-muted">{t(`uf.kind_${k}_desc`)}</p>
            <div className="mono rounded bg-sunken px-2 py-1 text-[11px] text-muted">{t(`uf.kind_${k}_how`)}</div>
          </CardBody></Card>
        ))}
      </div>
      <Alert tone="info" className="mb-6">{t("uf.key_note")}</Alert>
      <Card>
        <CardHeader icon={<Plug />} title={t("uf.conns")} />
        {q.isLoading ? <CardBody><Spinner /></CardBody> : !rows.length ? (
          <EmptyState icon={<Plug />} title={t("uf.no_conns")} action={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("uf.add_conn")}</Button>}>{t("uf.no_conns_text")}</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <thead><tr><Th>{t("uf.col_name")}</Th><Th>{t("uf.col_kind")}</Th><Th>{t("uf.col_reach")}</Th><Th>{t("uf.col_status")}</Th><Th>{t("uf.col_contents")}</Th><Th /></tr></thead>
              <tbody>
                {rows.map((c) => (
                  <Tr key={c.id}>
                    <Td><div className="font-bold">{c.name}</div><div className="text-xs text-muted">{c.environment_name}</div></Td>
                    <Td><KindBadge kind={c.kind} />{c.read_only && <Badge className="ml-1.5">{t("uf.read_only")}</Badge>}</Td>
                    <Td className="text-sm"><div>{t(`uf.tr_${c.transport}`)}</div><div className="mono max-w-64 truncate text-[11px] text-muted" title={c.base_url ?? ""}>{c.transport === "cloud" ? c.console_id : c.base_url}</div>
                      {c.tls_mode !== "verify" && c.transport !== "cloud" && <div className="text-[11px] text-muted">{c.tls_mode === "pin" ? <><Fingerprint className="mr-1 inline size-3" />{t("uf.tls_pin")}</> : <span className="text-warning">{t("uf.tls_insecure")}</span>}</div>}
                    </Td>
                    <Td><ConnStatus c={c} />{c.version && <div className="mt-1 text-[11px] text-muted">Network {c.version}</div>}
                      {c.status === "error" && <div className="mt-1 max-w-64 text-[11px] text-offline">{c.last_error}</div>}
                      {c.last_ok_at && <div className="text-[11px] text-muted">{timeAgo(c.last_ok_at, i18n.language)}</div>}
                    </Td>
                    <Td className="text-xs text-muted">{totalsText(c, t)}</Td>
                    <Td className="whitespace-nowrap text-right">
                      {admin && <>
                        <Button size="sm" variant="ghost" loading={test.isPending && test.variables?.id === c.id} onClick={() => test.mutate(c)}>{t("uf.test")}</Button>
                        <Button size="icon" variant="ghost" onClick={() => setEdit(c)} aria-label={t("common.edit")}><Pencil /></Button>
                        <Button size="icon" variant="ghost" onClick={() => setRemove(c)} aria-label={t("common.delete")}><Trash2 /></Button>
                      </>}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      {edit && <ConnectionDialog conn={edit === "new" ? null : edit} all={rows} onClose={() => setEdit(null)} />}
      {remove && (
        <TypeToConfirm open onOpenChange={(o) => !o && setRemove(null)} word={remove.name} loading={delM.isPending} title={t("uf.delete_title", { name: remove.name })}
          action={t("common.delete")} onConfirm={() => delM.mutate(remove)}><p className="text-sm">{t("uf.delete_text")}</p></TypeToConfirm>
      )}
    </>
  );
}

function totalsText(c: UnifiConnection, t: (k: string, o?: Record<string, unknown>) => string) {
  const x = c.totals ?? {};
  if (c.kind === "access") return x.doors != null ? t("uf.tot_access", { doors: x.doors }) : "–";
  if (c.kind === "site_manager") return x.hosts != null ? t("uf.tot_cloud", { hosts: x.hosts, sites: x.sites, devices: x.devices }) : "–";
  return x.devices != null ? t("uf.tot_net", { sites: x.sites, devices: x.devices, clients: x.clients }) : "–";
}

function ConnectionDialog({ conn, all, onClose }: { conn: UnifiConnection | null; all: UnifiConnection[]; onClose: () => void }) {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const qc = useQueryClient();
  const nodesQ = useQuery({ queryKey: ["all-nodes-unifi"], queryFn: () => get<NodeInfo[]>("api/nodes") });
  const [kind, setKind] = useState<UnifiKind>(conn?.kind ?? "network");
  const [f, setF] = useState({
    name: conn?.name ?? "", environment_id: conn?.environment_id ?? "", transport: conn?.transport ?? "direct", base_url: conn?.base_url ?? "",
    agent_node_id: conn?.agent_node_id ?? "", cloud_connection_id: conn?.cloud_connection_id ?? "", console_id: conn?.console_id ?? "",
    api_key: "", read_only: conn?.read_only ?? false, tls_mode: conn?.tls_mode ?? "pin", tls_fingerprint: conn?.tls_fingerprint ?? "", enabled: conn?.enabled ?? true,
  });
  const set = (p: Partial<typeof f>) => setF((x) => ({ ...x, ...p }));
  useEffect(() => { if (!f.environment_id && envs.data?.length) set({ environment_id: envs.data[0].id }); }, [envs.data, f.environment_id]);
  useEffect(() => {
    if (!conn && kind === "site_manager") set({ transport: "direct", tls_mode: "verify" });
  }, [kind, conn]);
  const clouds = all.filter((c) => c.kind === "site_manager");
  const cloudHosts = useQuery({
    queryKey: ["unifi-cloud", f.cloud_connection_id], enabled: f.transport === "cloud" && !!f.cloud_connection_id,
    queryFn: () => get<{ hosts?: { id: string; name: string; type: string; state: string }[] }>(`api/unifi/cloud/${f.cloud_connection_id}`),
  });
  const cert = useMutation({
    mutationFn: () => post<{ fingerprint: string; subject: string; issuer: string; not_after: string; self_signed: boolean }>("api/unifi/certificate", { url: f.base_url, kind: kind === "access" ? "access" : "network", agent_node_id: f.transport === "agent" ? f.agent_node_id : null }),
    onSuccess: (r) => set({ tls_fingerprint: r.fingerprint, tls_mode: "pin" }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const save = useMutation({
    mutationFn: () => {
      const body = { ...f, kind, api_key: f.api_key || null, agent_node_id: f.agent_node_id || null, cloud_connection_id: f.cloud_connection_id || null,
        console_id: f.console_id || null, base_url: f.base_url || null, tls_fingerprint: f.tls_mode === "pin" ? f.tls_fingerprint : null };
      if (conn) {
        const { kind: _k, ...rest } = body;
        void _k;
        return patch<UnifiConnection>(`api/unifi/connections/${conn.id}`, rest.api_key ? rest : { ...rest, api_key: undefined });
      }
      return post<UnifiConnection>("api/unifi/connections", body);
    },
    onSuccess: (r) => {
      if (r.status === "error") toast.warning(t("uf.saved_error", { error: r.last_error ?? "" }));
      else toast.success(t("uf.saved"));
      qc.invalidateQueries({ queryKey: ["unifi-connections"] }); qc.invalidateQueries({ queryKey: ["unifi-overview"] }); qc.invalidateQueries({ queryKey: ["unifi-access"] });
      onClose();
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const agents = useMemo(() => (nodesQ.data ?? []).filter((n) => !f.environment_id || n.environment_id === f.environment_id), [nodesQ.data, f.environment_id]);
  const transports = kind === "network" ? (["direct", "agent", "cloud"] as const) : (["direct", "agent"] as const);
  const needsUrl = kind !== "site_manager" && f.transport !== "cloud";
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} wide title={conn ? t("uf.edit_conn", { name: conn.name }) : t("uf.add_conn")} description={t("uf.dialog_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!f.name || !f.environment_id} onClick={() => save.mutate()}>{conn ? t("common.save") : t("uf.connect")}</Button></>}>
      <div className="space-y-4">
        {!conn && <Segmented value={kind} onChange={setKind} options={[{ value: "network", label: t("uf.kind_network") }, { value: "site_manager", label: t("uf.kind_site_manager") }, { value: "access", label: t("uf.kind_access") }]} />}
        <Alert tone="info" title={t(`uf.key_where_${kind}_title`)}>{t(`uf.key_where_${kind}`)}</Alert>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("uf.f_name")}><Input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder={t("uf.f_name_ph")} /></Field>
          <Field label={t("uf.f_env")} hint={t("uf.f_env_hint")}>
            <Select value={f.environment_id} onChange={(e) => set({ environment_id: e.target.value })}>{envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}</Select>
          </Field>
        </div>
        {kind !== "site_manager" && (
          <Field label={t("uf.f_transport")}>
            <Segmented value={f.transport} onChange={(v) => set({ transport: v })} options={transports.map((x) => ({ value: x, label: t(`uf.tr_${x}`) }))} />
            <p className="mt-1.5 text-xs text-muted">{t(`uf.tr_${f.transport}_hint`)}</p>
          </Field>
        )}
        {f.transport === "agent" && (
          <Field label={t("uf.f_agent")} hint={t("uf.f_agent_hint")}>
            <Select value={f.agent_node_id} onChange={(e) => set({ agent_node_id: e.target.value })}>
              <option value="">{t("uf.choose")}</option>
              {agents.map((n) => <option key={n.id} value={n.id} disabled={!n.online}>{n.name} ({n.kind}){n.online ? "" : ` · ${t("uf.offline")}`}</option>)}
            </Select>
          </Field>
        )}
        {f.transport === "cloud" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("uf.f_cloud")}>
              <Select value={f.cloud_connection_id} onChange={(e) => set({ cloud_connection_id: e.target.value, console_id: "" })}>
                <option value="">{t("uf.choose")}</option>
                {clouds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </Select>
            </Field>
            <Field label={t("uf.f_console")} hint={t("uf.f_console_hint")}>
              <Select value={f.console_id} onChange={(e) => set({ console_id: e.target.value })}>
                <option value="">{t("uf.choose")}</option>
                {(cloudHosts.data?.hosts ?? []).map((h) => <option key={h.id} value={h.id}>{h.name} ({h.state ?? h.type})</option>)}
                {f.console_id && !(cloudHosts.data?.hosts ?? []).some((h) => h.id === f.console_id) && <option value={f.console_id}>{f.console_id}</option>}
              </Select>
            </Field>
            {!clouds.length && <Alert tone="warning" className="sm:col-span-2">{t("uf.no_cloud")}</Alert>}
          </div>
        )}
        {needsUrl && (
          <Field label={t("uf.f_url")} hint={t(kind === "access" ? "uf.f_url_access_hint" : "uf.f_url_hint")}>
            <Input value={f.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder={kind === "access" ? "https://192.168.1.1:12445" : "https://192.168.1.1"} className="mono" />
          </Field>
        )}
        {f.transport !== "cloud" && (
          <Field label={t("uf.f_key")} hint={conn?.has_key ? t("uf.f_key_keep") : t("uf.f_key_hint")}>
            <Input type="password" autoComplete="off" value={f.api_key} onChange={(e) => set({ api_key: e.target.value })} placeholder={conn?.has_key ? "••••••••••••" : ""} className="mono" />
          </Field>
        )}
        <Switch checked={f.read_only} onCheckedChange={(v) => set({ read_only: v })} label={t("uf.f_ro")} description={t("uf.f_ro_hint")} />
        {needsUrl && (
          <div className="space-y-3 rounded-md border border-line p-4">
            <Field label={t("uf.f_tls")}>
              <Select value={f.tls_mode} onChange={(e) => set({ tls_mode: e.target.value as typeof f.tls_mode })}>
                <option value="pin">{t("uf.tls_pin_opt")}</option>
                <option value="verify">{t("uf.tls_verify_opt")}</option>
                <option value="insecure">{t("uf.tls_insecure_opt")}</option>
              </Select>
            </Field>
            {f.tls_mode === "pin" && (
              <Field label={t("uf.f_fp")} hint={cert.data ? `${cert.data.subject} · ${t("uf.valid_until")} ${cert.data.not_after.slice(0, 10)}${cert.data.self_signed ? ` · ${t("uf.self_signed")}` : ""}` : t("uf.f_fp_hint")}>
                <div className="flex gap-2">
                  <Input value={f.tls_fingerprint} onChange={(e) => set({ tls_fingerprint: e.target.value })} placeholder="AB:CD:…" className="mono text-xs" />
                  <Button onClick={() => cert.mutate()} loading={cert.isPending} disabled={!f.base_url || (f.transport === "agent" && !f.agent_node_id)}><Fingerprint /> {t("uf.fetch_cert")}</Button>
                </div>
              </Field>
            )}
            {f.tls_mode === "insecure" && <Alert tone="warning">{t("uf.tls_insecure_warn")}</Alert>}
          </div>
        )}
        {conn && <Switch checked={f.enabled} onCheckedChange={(v) => set({ enabled: v })} label={t("uf.f_enabled")} description={t("uf.f_enabled_hint")} />}
      </div>
    </Dialog>
  );
}
