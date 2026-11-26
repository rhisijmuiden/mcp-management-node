import { useQuery } from "@tanstack/react-query";
import {
  Archive, BookText, Bot, Check, CircleAlert, CloudCog, Globe, Laptop, LifeBuoy, Monitor, Network, Radar, Rocket, Server, Wifi,
} from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { getAll } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useCustomer } from "./customers";

interface Counts { total: number; online: number }
export interface CustomerLinks {
  customer_id: string;
  tiles: {
    proxmox: Counts; servers: Counts; workstations: Counts;
    network: { connections: number; errors: number; devices: number };
    m365: { tenants: number; errors: number; names: string[] };
    mcp: { servers: number; enabled: number; shared: number; errors: number };
    deployments: { total: number; failed: number; repos: string[] };
    backups: { considered: number; protected: number; problems: number; last_backup: string | null };
    tickets: { open: number };
    ai: { tokens: number; customer_only: number };
    docs: { documents: number; vault: number };
    portal: { enabled: boolean; users: number; active: number };
    uptime: { checks: number };
    certificates: { total: number };
  };
  todo: { key: string; to: string; values: Record<string, number> }[];
  done: { key: string; to: string; values: Record<string, number> }[];
}

export const useCustomerLinks = (id: string) =>
  useQuery({ queryKey: ["customer", id, "links"], queryFn: () => getAll<CustomerLinks>(`api/customers/${id}/links`), refetchInterval: 60_000 });

type Tone = "ok" | "warn" | "bad" | "empty" | "plain";

/** Tiles: everything linked to the customer. An empty tile opens the step-by-step guide. */
export function LinkedTiles({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const q = useCustomerLinks(customerId);
  const { customerId: current, setCustomer } = useCustomer();
  const navigate = useNavigate();
  if (q.isLoading) return <Spinner />;
  if (!q.data) return null;
  const d = q.data.tiles;
  const go = (to: string) => {
    if (current !== customerId) setCustomer(customerId);
    navigate(to);
  };
  const machine = (c: Counts): Tone => (!c.total ? "empty" : c.online < c.total ? "warn" : "ok");
  const tiles: { key: string; icon: ReactNode; value: ReactNode; hint: ReactNode; tone: Tone; to: string }[] = [
    { key: "proxmox", icon: <Server />, value: t("links.nodes", { count: d.proxmox.total }), hint: d.proxmox.total ? t("links.online", { online: d.proxmox.online, total: d.proxmox.total }) : t("links.pair"), tone: machine(d.proxmox), to: d.proxmox.total ? "/proxmox" : "/integrations?setup=agents" },
    { key: "servers", icon: <Monitor />, value: d.servers.total, hint: d.servers.total ? t("links.online", { online: d.servers.online, total: d.servers.total }) : t("links.pair"), tone: machine(d.servers), to: d.servers.total ? "/servers" : "/integrations?setup=agents" },
    { key: "workstations", icon: <Laptop />, value: d.workstations.total, hint: d.workstations.total ? t("links.online", { online: d.workstations.online, total: d.workstations.total }) : t("links.pair"), tone: d.workstations.total ? "plain" : "empty", to: d.workstations.total ? "/workstations" : "/integrations?setup=agents" },
    { key: "network", icon: <Wifi />, value: d.network.connections ? t("links.devices", { count: d.network.devices }) : "–", hint: d.network.connections ? t("links.consoles", { count: d.network.connections }) : t("links.connect"), tone: !d.network.connections ? "empty" : d.network.errors ? "bad" : "ok", to: d.network.connections ? "/unifi/network" : "/integrations?setup=unifi" },
    { key: "m365", icon: <CloudCog />, value: d.m365.tenants ? d.m365.names.join(", ") : "–", hint: d.m365.tenants ? (d.m365.errors ? t("links.errors", { count: d.m365.errors }) : t("links.connected")) : t("links.connect"), tone: !d.m365.tenants ? "empty" : d.m365.errors ? "bad" : "ok", to: d.m365.tenants ? "/m365" : "/integrations?setup=m365" },
    { key: "mcp", icon: <Network />, value: d.mcp.servers, hint: t("links.mcp_hint", { shared: d.mcp.shared }), tone: d.mcp.errors ? "bad" : d.mcp.servers ? "ok" : "plain", to: d.mcp.servers ? "/ai/hub" : "/integrations?setup=mcp" },
    { key: "deployments", icon: <Rocket />, value: d.deployments.total, hint: d.deployments.repos.length ? d.deployments.repos.join(", ") : t("links.none"), tone: d.deployments.failed ? "bad" : d.deployments.total ? "ok" : "plain", to: "/deployments" },
    { key: "backups", icon: <Archive />, value: d.backups.considered ? `${d.backups.protected} / ${d.backups.considered}` : "–", hint: d.backups.considered ? (d.backups.problems ? t("links.problems", { count: d.backups.problems }) : t("links.all_protected")) : t("links.not_checked"), tone: !d.backups.considered ? "plain" : d.backups.problems ? "bad" : "ok", to: "/backups" },
    { key: "tickets", icon: <LifeBuoy />, value: d.tickets.open, hint: d.tickets.open ? t("links.open") : t("links.none_open"), tone: d.tickets.open ? "warn" : "plain", to: "/tickets" },
    { key: "ai", icon: <Bot />, value: d.ai.tokens, hint: d.ai.customer_only ? t("links.ai_only", { count: d.ai.customer_only }) : t("links.ai_all"), tone: "plain", to: "/ai/tokens" },
    { key: "docs", icon: <BookText />, value: d.docs.documents, hint: t("links.vault", { count: d.docs.vault }), tone: "plain", to: "/docs" },
    { key: "portal", icon: <Globe />, value: d.portal.enabled ? t("links.portal_users", { count: d.portal.users }) : t("links.portal_off"), hint: d.portal.users ? t("links.portal_active", { count: d.portal.active }) : t("links.portal_invite"), tone: !d.portal.enabled || !d.portal.users ? "empty" : "ok", to: d.portal.enabled ? "/tickets?tab=accounts" : "/admin/portal" },
    { key: "uptime", icon: <Radar />, value: d.uptime.checks, hint: t("links.certs", { count: d.certificates.total }), tone: d.uptime.checks ? "plain" : "empty", to: "/uptime" },
  ];
  const toneCls: Record<Tone, string> = {
    ok: "text-online", warn: "text-warning", bad: "text-offline", empty: "text-accent-text", plain: "text-muted",
  };
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {tiles.map((x) => (
        <button key={x.key} type="button" onClick={() => go(x.to)}
          className={cn("group flex min-w-0 cursor-pointer flex-col gap-1 rounded-lg border bg-raised p-3.5 text-left transition-colors hover:border-ink/30",
            x.tone === "empty" ? "border-dashed border-line bg-transparent" : "border-line shadow-card")}>
          <span className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-[0.07em] text-muted [&_svg]:size-4">
            {x.icon}<span className="truncate">{t(`links.t_${x.key}`)}</span>
            {(x.tone === "bad" || x.tone === "warn") && <span className={cn("ml-auto size-2 shrink-0 rounded-full", x.tone === "bad" ? "bg-offline" : "bg-warning")} />}
          </span>
          <span className="truncate text-lg font-bold tabular-nums">{x.value}</span>
          <span className={cn("truncate text-xs", toneCls[x.tone])}>{x.hint}</span>
        </button>
      ))}
    </div>
  );
}

/** What is not set up yet at this customer, with a link to fix each item. */
export function SetupChecklist({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const q = useCustomerLinks(customerId);
  const { customerId: current, setCustomer } = useCustomer();
  if (!q.data) return null;
  const { todo, done } = q.data;
  return (
    <Card>
      <CardHeader icon={<CircleAlert />} title={t("links.todo_title")} description={todo.length ? t("links.todo_desc", { count: todo.length }) : t("links.todo_none")} />
      <CardBody className="space-y-0 py-2 text-sm">
        {todo.map((x) => (
          <div key={x.key} className="flex items-center gap-2.5 border-b border-line py-2 last:border-0">
            <span className="grid size-5 shrink-0 place-items-center rounded-full bg-warning-soft text-[11px] font-black text-warning">!</span>
            <span className="min-w-0 flex-1">{t(`links.todo.${x.key}`, x.values)}</span>
            <Link to={x.to} onClick={() => { if (current !== customerId) setCustomer(customerId); }} className="shrink-0 text-xs font-bold text-accent-text hover:underline">{t("links.fix")}</Link>
          </div>
        ))}
        {done.map((x) => (
          <div key={x.key} className="flex items-center gap-2.5 border-b border-line py-2 text-muted last:border-0">
            <span className="grid size-5 shrink-0 place-items-center rounded-full bg-online-soft text-online"><Check className="size-3" /></span>
            <span className="min-w-0 flex-1">{t(`links.done.${x.key}`, x.values)}</span>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}
