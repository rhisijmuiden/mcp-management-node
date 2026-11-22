import * as DM from "@radix-ui/react-dropdown-menu";
import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowRight, Bot, Building2, ChevronDown, FileText, KeyRound, Laptop, MessageSquare, Monitor, Network, Pencil, Plus,
  Search, Server, Sparkles, Trash2, UserRound, Wifi, X,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { CustomerDialog, CustomerKindIcon, NewCustomerButton, useCustomer, useCustomers } from "@/components/customers";
import { EnvDot, PairDialog, StatusDot } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, del, errorMessage, getAll } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Customer, CustomerContact, CustomerOverview, Machine, ScanFinding, ScanResult } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { NewTokenDialog } from "./Ai";
import { NotifyCard } from "./Proxmox";
import { AddServerDialog } from "./Servers";

const tabCls =
  "cursor-pointer border-b-2 border-transparent px-1 pb-2.5 pt-1 text-[14px] font-semibold text-muted transition-colors hover:text-ink data-[state=active]:border-koraal data-[state=active]:text-ink";

/* ------------------------------------------------------------------ list */
export function CustomersPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const list = useCustomers();
  const { setCustomer } = useCustomer();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const rows = (list.data ?? []).filter((c) => !q || `${c.name} ${c.contact_name ?? ""} ${c.description ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const tot = useMemo(() => {
    const d = list.data ?? [];
    return {
      customers: d.length, machines: d.reduce((a, c) => a + c.machines, 0), online: d.reduce((a, c) => a + c.online, 0),
      attention: d.filter((c) => c.status === "attention").length, updates: d.reduce((a, c) => a + c.updates, 0),
    };
  }, [list.data]);
  return (
    <>
      <PageHeader title={t("cust.title")} description={t("cust.subtitle")} actions={admin && <NewCustomerButton />} />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("cust.k_customers")} value={tot.customers} icon={<Building2 />} tone="info" />
        <Stat label={t("cust.k_machines")} value={`${tot.online} / ${tot.machines}`} hint={t("cust.k_machines_hint")} icon={<Server />} tone={tot.online < tot.machines ? "warning" : "online"} />
        <Stat label={t("cust.k_attention")} value={tot.attention} hint={t("cust.k_attention_hint")} icon={<AlertTriangle />} tone={tot.attention ? "warning" : "online"} />
        <Stat label={t("cust.k_updates")} value={tot.updates} hint={t("cust.k_updates_hint")} icon={<Sparkles />} tone={tot.updates ? "accent" : "online"} />
      </div>
      <Card className="mt-6">
        <div className="flex flex-wrap items-center gap-3 border-b border-line p-3">
          <div className="relative max-w-sm flex-1">
            <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("cust.search")} aria-label={t("cust.search")} />
          </div>
        </div>
        {list.isLoading ? <CardBody><Spinner /></CardBody> : !rows.length ? (
          <EmptyState icon={<Building2 />} title={t("cust.empty")} action={admin && <NewCustomerButton />}>{t("cust.empty_text")}</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <thead>
                <tr><Th>{t("cust.customer")}</Th><Th>{t("cust.status")}</Th><Th>{t("cust.col_machines")}</Th><Th>{t("cust.col_network")}</Th><Th>{t("px.updates")}</Th><Th>{t("cust.col_alerts")}</Th><Th>{t("cust.maintenance")}</Th><Th /></tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <Tr key={c.id} className="cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                    <Td>
                      <Link to={`/customers/${c.id}`} className="flex items-center gap-2 font-bold hover:underline" onClick={(e) => e.stopPropagation()}><EnvDot color={c.color} /> {c.name}</Link>
                      <div className="flex items-center gap-1.5 text-xs text-muted"><CustomerKindIcon kind={c.kind} className="size-3" />{c.kind === "home" ? t("px.kind_home") : t("px.kind_customer")}{c.contact_name && ` · ${c.contact_name}`}</div>
                    </Td>
                    <Td><StatusBadge status={c.status} /></Td>
                    <Td className="whitespace-nowrap text-sm">
                      <span className="font-semibold">{c.online}/{c.machines}</span> <span className="text-muted">{t("px.online").toLowerCase()}</span>
                      <div className="text-xs text-muted">{t("cust.split", { px: c.counts?.proxmox ?? 0, srv: c.counts?.servers ?? 0, ws: c.counts?.workstations ?? 0 })}</div>
                    </Td>
                    <Td className="text-sm">{c.unifi.connections ? <>{t("cust.devices_n", { count: c.unifi.devices })}{c.unifi.errors > 0 && <Badge tone="offline" className="ml-1.5">{t("cust.errors_n", { count: c.unifi.errors })}</Badge>}</> : <span className="text-muted">–</span>}</Td>
                    <Td>{c.updates ? <Badge tone="accent">{c.updates}</Badge> : <span className="text-muted">0</span>}</Td>
                    <Td>{c.alerts ? <Badge tone="warning">{c.alerts}</Badge> : <span className="text-muted">0</span>}</Td>
                    <Td className="text-sm text-muted">{c.maintenance_window || "–"}</Td>
                    <Td className="text-right">
                      <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setCustomer(c.id); toast.success(t("cust.switched", { name: c.name })); }}>{t("cust.work_in")}</Button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
    </>
  );
}

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "ok") return <Badge tone="online" dot>{t("cust.status_ok")}</Badge>;
  if (status === "attention") return <Badge tone="warning" dot>{t("cust.status_attention")}</Badge>;
  return <Badge>{t("cust.status_empty")}</Badge>;
}

/* ------------------------------------------------------------------ detail */
const TABS = ["overview", "proxmox", "servers", "workstations", "network", "ai", "contacts", "reports"] as const;
type Tab = (typeof TABS)[number];

export function CustomerPage() {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [params, setParams] = useSearchParams();
  const tab = (TABS.includes(params.get("tab") as Tab) ? params.get("tab") : "overview") as Tab;
  const ov = useQuery({
    queryKey: ["customer", id, "overview"],
    queryFn: () => getAll<CustomerOverview>(`api/customers/${id}/overview`),
    refetchInterval: 30_000,
  });
  const [edit, setEdit] = useState(false);
  const [pair, setPair] = useState<null | "proxmox" | "server" | "workstation">(null);
  if (ov.isLoading) return <Spinner />;
  if (!ov.data) return <Alert tone="offline">{t("cust.not_found")}</Alert>;
  const d = ov.data;
  const c = d.customer;
  const offline = d.machines.filter((m) => !m.online && m.type !== "workstation").length;
  const status = !d.machines.length && !d.unifi.length ? "empty" : offline || d.totals.network.errors || d.alerts.length ? "attention" : "ok";
  const count = (n: number) => (n ? <span className="ml-1.5 rounded-full bg-sunken px-1.5 text-[11px] text-muted">{n}</span> : null);
  return (
    <>
      <nav className="mb-1.5 text-[13px] text-muted" aria-label={t("cust.breadcrumb")}>
        <Link to="/customers" className="hover:text-ink hover:underline">{t("cust.title")}</Link> / <span>{c.name}</span>
      </nav>
      <PageHeader
        title={<span className="flex items-center gap-3"><EnvDot color={c.color} className="size-3.5" /> {c.name}</span>}
        description={
          <span className="mt-1 flex flex-wrap gap-2">
            <StatusBadge status={status} />
            {c.maintenance_window && <Badge>{t("cust.maintenance")}: {c.maintenance_window}</Badge>}
            {c.sla && <Badge>{t("cust.sla")}: {c.sla}</Badge>}
            {c.contact_name && <Badge>{t("cust.contact")}: {c.contact_name}</Badge>}
          </span>
        }
        actions={admin && (
          <>
            <Button onClick={() => setEdit(true)}><Pencil /> {t("common.edit")}</Button>
            <DM.Root>
              <DM.Trigger asChild><Button variant="primary"><Plus /> {t("cust.add_machine")} <ChevronDown /></Button></DM.Trigger>
              <DM.Portal>
                <DM.Content align="end" sideOffset={6} className="z-50 min-w-56 rounded-md border border-line bg-raised p-1 shadow-xl">
                  {([["proxmox", <Server key="p" className="size-4" />, t("cust.add_proxmox")], ["server", <Monitor key="s" className="size-4" />, t("cust.add_server")], ["workstation", <Laptop key="w" className="size-4" />, t("cust.add_workstation")]] as const).map(([k, icon, label]) => (
                    <DM.Item key={k} onSelect={() => setPair(k)} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">{icon}{label}</DM.Item>
                  ))}
                  <DM.Item asChild className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
                    <Link to="/unifi/connections"><Wifi className="size-4" />{t("cust.add_unifi")}</Link>
                  </DM.Item>
                </DM.Content>
              </DM.Portal>
            </DM.Root>
          </>
        )}
      />
      {c.description && <p className="-mt-3 mb-5 max-w-3xl text-sm text-muted">{c.description}</p>}
      <Tabs.Root value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <Tabs.List className="mb-6 flex flex-wrap gap-x-6 gap-y-1 border-b border-line" aria-label={t("cust.sections")}>
          <Tabs.Trigger value="overview" className={tabCls}>{t("cust.tab_overview")}</Tabs.Trigger>
          <Tabs.Trigger value="proxmox" className={tabCls}>Proxmox{count(d.totals.proxmox.total)}</Tabs.Trigger>
          <Tabs.Trigger value="servers" className={tabCls}>{t("cust.tab_servers")}{count(d.totals.servers.total)}</Tabs.Trigger>
          <Tabs.Trigger value="workstations" className={tabCls}>{t("cust.workstations")}{count(d.totals.workstations.total)}</Tabs.Trigger>
          <Tabs.Trigger value="network" className={tabCls}>{t("cust.tab_network")}{count(d.totals.network.connections)}</Tabs.Trigger>
          <Tabs.Trigger value="ai" className={tabCls}>{t("cust.tab_ai")}</Tabs.Trigger>
          <Tabs.Trigger value="contacts" className={tabCls}>{t("cust.tab_contacts")}</Tabs.Trigger>
          <Tabs.Trigger value="reports" className={tabCls}>{t("cust.tab_reports")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="overview"><OverviewTab d={d} admin={admin} onTab={(v) => setParams({ tab: v }, { replace: true })} /></Tabs.Content>
        <Tabs.Content value="proxmox">
          <div className="grid gap-6 xl:grid-cols-5">
            <div className="xl:col-span-3"><MachinesCard title="Proxmox" icon={<Server />} rows={d.machines.filter((m) => m.type === "proxmox")} emptyText={t("cust.empty_proxmox")} onAdd={admin ? () => setPair("proxmox") : undefined} /></div>
            <div className="xl:col-span-2">{admin && <NotifyCard env={c} />}</div>
          </div>
        </Tabs.Content>
        <Tabs.Content value="servers"><MachinesCard title={t("cust.tab_servers")} icon={<Monitor />} rows={d.machines.filter((m) => m.type === "server")} emptyText={t("cust.empty_servers")} onAdd={admin ? () => setPair("server") : undefined} /></Tabs.Content>
        <Tabs.Content value="workstations">
          <Alert tone="info" className="mb-4">{t("cust.ws_hint")}</Alert>
          <MachinesCard title={t("cust.workstations")} icon={<Laptop />} rows={d.machines.filter((m) => m.type === "workstation")} emptyText={t("cust.empty_workstations")} onAdd={admin ? () => setPair("workstation") : undefined} />
        </Tabs.Content>
        <Tabs.Content value="network"><NetworkTab d={d} /></Tabs.Content>
        <Tabs.Content value="ai"><AiTab d={d} /></Tabs.Content>
        <Tabs.Content value="contacts"><ContactsTab c={c} admin={admin} machines={d.machines.length + d.unifi.length} /></Tabs.Content>
        <Tabs.Content value="reports">
          <Card><EmptyState icon={<FileText />} title={t("cust.reports_title")}>{t("cust.reports_text")}</EmptyState></Card>
        </Tabs.Content>
      </Tabs.Root>
      <CustomerDialog open={edit} onOpenChange={setEdit} customer={c} />
      <PairDialog open={pair === "proxmox"} onOpenChange={(v) => !v && setPair(null)} environmentId={c.id} />
      <AddServerDialog open={pair === "server" || pair === "workstation"} onOpenChange={(v) => !v && setPair(null)} customerId={c.id} role={pair === "workstation" ? "workstation" : "server"} />
    </>
  );
}

/* ------------------------------------------------------------------ overview tab */
function OverviewTab({ d, admin, onTab }: { d: CustomerOverview; admin: boolean; onTab: (t: Tab) => void }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const tot = d.totals;
  const c = d.customer;
  const rows = d.machines.filter((m) => !q || `${m.name} ${m.os ?? ""} ${m.hostname ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const unifiRows = d.unifi.filter((u) => !q || u.name.toLowerCase().includes(q.toLowerCase()));
  const tile = (tab: Tab, label: string, value: ReactNode, hint: ReactNode, tone: "info" | "warning" | "online" | "offline" | "accent", icon: ReactNode) => (
    <button type="button" onClick={() => onTab(tab)} className="cursor-pointer text-left"><Stat label={label} value={value} hint={hint} tone={tone} icon={icon} /></button>
  );
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        {tile("proxmox", "Proxmox", t("cust.nodes_n", { count: tot.proxmox.total }), t("cust.guests_n", { count: tot.proxmox.guests ?? 0 }), tot.proxmox.online < tot.proxmox.total ? "offline" : "info", <Server />)}
        {tile("servers", t("cust.tab_servers"), tot.servers.total, t("srv.k_split", { linux: tot.servers.linux, windows: tot.servers.windows }), tot.servers.attention ? "warning" : "info", <Monitor />)}
        {tile("workstations", t("cust.workstations"), tot.workstations.total, tot.workstations.with_updates ? t("cust.need_updates", { count: tot.workstations.with_updates }) : t("cust.up_to_date"), tot.workstations.with_updates ? "warning" : "info", <Laptop />)}
        {tile("network", t("cust.tab_network"), t("cust.sites_n", { count: tot.network.sites }), tot.network.connections ? t("cust.unifi_devices", { count: tot.network.devices }) : t("cust.no_unifi"), tot.network.errors || tot.network.offline ? "warning" : "info", <Wifi />)}
        {tile("overview", t("cust.col_alerts"), tot.alerts, tot.alerts ? t("cust.alerts_open") : t("cust.no_alerts"), tot.alerts ? "warning" : "online", <AlertTriangle />)}
      </div>
      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-6">
          <ScanCard customer={c} admin={admin} />
          <Card className="min-w-0">
            <CardHeader icon={<Building2 />} title={t("cust.everything")} description={t("cust.everything_desc")}
              action={<div className="relative w-48"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input className="h-9 pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("cust.filter")} aria-label={t("cust.filter")} /></div>} />
            {!rows.length && !unifiRows.length ? <EmptyState icon={<Server />} title={t("cust.nothing_yet")}>{t("cust.nothing_yet_text")}</EmptyState> : (
              <div className="overflow-x-auto">
                <Table>
                  <thead><tr><Th>{t("px.name")}</Th><Th>{t("cust.col_type")}</Th><Th>{t("srv.col_os")}</Th><Th>{t("cust.status")}</Th><Th>{t("px.updates")}</Th></tr></thead>
                  <tbody>
                    {rows.map((m) => <MachineRow key={m.id} m={m} />)}
                    {unifiRows.map((u) => (
                      <Tr key={u.id}>
                        <Td><Link to="/unifi/network" className="flex items-center gap-2 font-semibold hover:underline"><StatusDot online={u.status === "ok"} /> {u.name}</Link></Td>
                        <Td>{t(`cust.type_unifi_${u.kind}`)}</Td>
                        <Td className="text-sm text-muted">{u.kind === "access" ? t("cust.doors_n", { count: u.totals.doors ?? 0 }) : t("cust.unifi_devices", { count: u.totals.devices ?? 0 })}</Td>
                        <Td>{u.status === "error" ? <Badge tone="offline" dot>{t("cust.st_error")}</Badge> : u.totals.offline ? <Badge tone="warning" dot>{t("cust.offline_n", { count: u.totals.offline })}</Badge> : <Badge tone="online" dot>{t("px.online")}</Badge>}</Td>
                        <Td>{u.totals.firmware ? <Badge tone="accent">{t("cust.firmware_n", { count: u.totals.firmware })}</Badge> : <span className="text-xs text-muted">{t("cust.up_to_date")}</span>}</Td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            )}
          </Card>
          {d.alerts.length > 0 && (
            <Card>
              <CardHeader icon={<AlertTriangle />} title={t("cust.open_alerts")} action={<Link to="/monitoring" className="text-sm font-semibold text-accent-text hover:underline">{t("nav.monitoring")} →</Link>} />
              <CardBody className="space-y-2 text-sm">
                {d.alerts.map((a) => (
                  <div key={a.id} className="flex items-start gap-2"><Badge tone={a.severity === "critical" ? "offline" : "warning"}>{a.node ?? "–"}</Badge><span className="flex-1">{a.message}</span></div>
                ))}
              </CardBody>
            </Card>
          )}
        </div>
        <div className="space-y-6">
          <AiAccessCard d={d} onTab={onTab} />
          <AskCard name={c.name} />
          <Card>
            <CardHeader icon={<Bot />} title={t("cust.notes_ai")} action={<button type="button" onClick={() => onTab("contacts")} className="cursor-pointer text-sm font-semibold text-accent-text hover:underline">{t("common.edit")}</button>} />
            <CardBody className="text-sm">
              {c.notes ? <p className="whitespace-pre-wrap leading-relaxed text-muted">{c.notes}</p> : <p className="text-muted">{t("cust.no_notes")}</p>}
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}

function MachineRow({ m }: { m: Machine }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const to = m.type === "proxmox" ? `/proxmox/nodes/${m.id}` : `/servers/${m.id}`;
  const attn = m.attention.filter((a) => a !== "offline");
  return (
    <Tr className="cursor-pointer" onClick={() => navigate(to)}>
      <Td>
        <Link to={to} className="flex items-center gap-2 font-semibold hover:underline" onClick={(e) => e.stopPropagation()}><StatusDot online={m.online} /> {m.name}</Link>
        {m.cluster && <span className="text-xs text-muted">{t("px.cluster")} {m.cluster}</span>}
      </Td>
      <Td className="whitespace-nowrap">{t(`cust.type_${m.type}`)}</Td>
      <Td className="max-w-56 truncate text-sm" title={m.os ?? ""}>{m.os ?? "–"}</Td>
      <Td className="whitespace-nowrap">
        {!m.online ? <span className="text-sm text-muted">{m.type === "workstation" ? t("cust.asleep") : t("px.offline")} · {timeAgo(m.last_seen_at, i18n.language)}</span>
          : attn.length ? <Badge tone="warning" dot>{attn.map((a) => t(`srv.att_${a}`)).join(", ")}</Badge>
            : <Badge tone="online" dot>{t("px.online")}{m.mem_pct != null ? ` · RAM ${m.mem_pct.toFixed(0)}%` : ""}</Badge>}
      </Td>
      <Td className="whitespace-nowrap">
        {m.updates ? <Badge tone="accent">{t("cust.pending_n", { count: m.updates })}</Badge> : <span className="text-xs text-muted">{t("cust.up_to_date")}</span>}
        {m.reboot_required && <Badge tone="warning" className="ml-1.5">{t("px.reboot")}</Badge>}
      </Td>
    </Tr>
  );
}

function MachinesCard({ title, icon, rows, emptyText, onAdd }: { title: string; icon: ReactNode; rows: Machine[]; emptyText: string; onAdd?: () => void }) {
  const { t } = useTranslation();
  return (
    <Card className="min-w-0">
      <CardHeader icon={icon} title={title} action={onAdd && <Button size="sm" onClick={onAdd}><Plus /> {t("cust.add_machine")}</Button>} />
      {!rows.length ? <EmptyState icon={icon} title={t("cust.none_yet")}>{emptyText}</EmptyState> : (
        <div className="overflow-x-auto">
          <Table>
            <thead><tr><Th>{t("px.name")}</Th><Th>{t("cust.col_type")}</Th><Th>{t("srv.col_os")}</Th><Th>{t("cust.status")}</Th><Th>{t("px.updates")}</Th></tr></thead>
            <tbody>{rows.map((m) => <MachineRow key={m.id} m={m} />)}</tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

const SEV_TONE: Record<string, "offline" | "warning" | "info" | "neutral"> = { high: "offline", medium: "warning", low: "info", info: "neutral" };

function ScanCard({ customer, admin }: { customer: Customer; admin: boolean }) {
  const { t } = useTranslation();
  const [result, setResult] = useState<ScanResult | null>(null);
  const scan = useMutation({
    mutationFn: () => api<ScanResult>(`api/customers/${customer.id}/scan`, { method: "POST", json: {}, allCustomers: true }),
    onSuccess: setResult,
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  useEffect(() => setResult(null), [customer.id]);
  const prompt = t("cust.ask_optimise", { name: customer.name });
  return (
    <Card>
      <CardHeader icon={<Sparkles />} title={t("cust.scan_title")} description={t("cust.scan_desc")}
        action={admin && <Button size="sm" variant="primary" loading={scan.isPending} onClick={() => scan.mutate()}>{!scan.isPending && <Sparkles />} {result ? t("cust.scan_again") : t("cust.scan_run")}</Button>} />
      <CardBody className="space-y-3">
        <div className="flex items-center gap-2 rounded-md bg-sunken px-3 py-2 text-sm"><MessageSquare className="size-4 shrink-0 text-koraal" /><span className="flex-1 italic">“{prompt}”</span><CopyButton value={prompt} /></div>
        {!result ? <p className="text-sm text-muted">{t("cust.scan_hint")}</p> : !result.findings.length ? (
          <Alert tone="online">{t("cust.scan_clean")}</Alert>
        ) : (
          <ol className="space-y-2.5">
            {result.findings.map((f: ScanFinding, i) => (
              <li key={i} className="flex gap-3 rounded-md border border-line p-3 text-sm">
                <Badge tone={SEV_TONE[f.severity]} className="h-fit shrink-0">{t(`cust.sev_${f.severity}`)}</Badge>
                <div className="min-w-0 flex-1">
                  <div><span className="font-semibold">{f.target}</span> · {f.finding}</div>
                  <div className="text-muted">{f.suggestion}</div>
                </div>
              </li>
            ))}
          </ol>
        )}
        {result && <p className="text-xs text-muted">{t("cust.scan_checked", { px: result.checked.proxmox_nodes, srv: result.checked.servers, ws: result.checked.workstations, net: result.checked.unifi_connections })}</p>}
      </CardBody>
    </Card>
  );
}

function AiAccessCard({ d, onTab }: { d: CustomerOverview; onTab: (t: Tab) => void }) {
  const { t } = useTranslation();
  return (
    <Card className="bg-nacht text-white dark:bg-raised">
      <CardBody className="space-y-4 py-5">
        <div className="flex items-center gap-2 text-lucht"><KeyRound className="size-5" /><span className="text-sm font-bold uppercase tracking-wider">{t("cust.ai_access")}</span></div>
        <p className="text-sm text-white/75 dark:text-muted">{t("cust.ai_access_text")}</p>
        <div className="space-y-2 text-sm">
          {d.ai_access.length ? d.ai_access.slice(0, 5).map((a) => (
            <div key={a.id} className="flex items-center justify-between gap-3">
              <span className="mono truncate">{a.name}</span>
              <span className="shrink-0 text-lucht">{a.all_customers ? t("cust.all_customers_short") : t("cust.this_customer")}</span>
            </div>
          )) : <p className="text-white/60 dark:text-muted">{t("cust.no_tokens")}</p>}
        </div>
        <Button variant="primary" className="w-full justify-center" onClick={() => onTab("ai")}><KeyRound /> {t("cust.new_token")}</Button>
      </CardBody>
    </Card>
  );
}

function AskCard({ name }: { name: string }) {
  const { t } = useTranslation();
  const examples = [t("cust.ask_1", { name }), t("cust.ask_2", { name }), t("cust.ask_3", { name }), t("cust.ask_4", { name })];
  return (
    <Card>
      <CardHeader icon={<MessageSquare />} title={t("cust.try_asking")} />
      <CardBody className="space-y-2">
        {examples.map((e) => (
          <div key={e} className="flex items-start gap-2 rounded-md bg-sunken px-3 py-2 text-sm"><span className="flex-1">“{e}”</span><CopyButton value={e} /></div>
        ))}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ network tab */
function NetworkTab({ d }: { d: CustomerOverview }) {
  const { t, i18n } = useTranslation();
  if (!d.unifi.length) {
    return <Card><EmptyState icon={<Network />} title={t("cust.no_unifi")} action={<Link to="/unifi/connections"><Button variant="primary"><Plus /> {t("cust.add_unifi")}</Button></Link>}>{t("cust.no_unifi_text")}</EmptyState></Card>;
  }
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {d.unifi.map((u) => (
        <Card key={u.id}>
          <CardHeader icon={<Wifi />} title={u.name} description={t(`cust.type_unifi_${u.kind}`)}
            action={u.status === "error" ? <Badge tone="offline" dot>{t("cust.st_error")}</Badge> : <Badge tone="online" dot>{t("px.online")}</Badge>} />
          <CardBody className="space-y-2 text-sm">
            {u.last_error && <Alert tone="offline">{u.last_error}</Alert>}
            <div className="grid grid-cols-3 gap-2">
              {u.kind === "access" ? (
                <div><div className="text-xs text-muted">{t("cust.doors")}</div><div className="font-bold">{u.totals.doors ?? 0}</div></div>
              ) : (
                <>
                  <div><div className="text-xs text-muted">{t("cust.devices")}</div><div className="font-bold">{u.totals.devices ?? 0}</div></div>
                  <div><div className="text-xs text-muted">{t("px.offline")}</div><div className="font-bold">{u.totals.offline ?? 0}</div></div>
                  <div><div className="text-xs text-muted">{t("cust.firmware")}</div><div className="font-bold">{u.totals.firmware ?? 0}</div></div>
                </>
              )}
            </div>
            <div className="text-xs text-muted">{t("cust.last_ok", { when: timeAgo(u.last_ok_at, i18n.language) })}</div>
            <Link to={u.kind === "access" ? "/unifi/access" : "/unifi/network"} className="inline-flex items-center gap-1 font-semibold text-accent-text hover:underline">{t("cust.open")} <ArrowRight className="size-4" /></Link>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ AI access tab */
function AiTab({ d }: { d: CustomerOverview }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const c = d.customer;
  const ctx = useQuery({ queryKey: ["customer", c.id, "context"], queryFn: () => getAll<{ uri: string; markdown: string }>(`api/customers/${c.id}/context`) });
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Card className="min-w-0">
        <CardHeader icon={<KeyRound />} title={t("cust.ai_tokens")} description={t("cust.ai_tokens_desc")} action={<Button variant="primary" size="sm" onClick={() => setOpen(true)}><Plus /> {t("cust.new_token")}</Button>} />
        {!d.ai_access.length ? <EmptyState icon={<KeyRound />} title={t("cust.no_tokens")}>{t("cust.no_tokens_text")}</EmptyState> : (
          <Table>
            <thead><tr><Th>{t("ai.token_name")}</Th><Th>{t("cust.access")}</Th><Th>{t("ai.scopes")}</Th><Th>{t("ai.used_by")}</Th></tr></thead>
            <tbody>
              {d.ai_access.map((a) => (
                <Tr key={a.id}>
                  <Td><div className="font-semibold">{a.name}</div><div className="text-xs text-muted">{a.kind === "oauth" ? t("cust.oauth_app") : t("cust.personal_token")}</div></Td>
                  <Td>{a.all_customers ? <Badge tone="warning">{t("cust.all_customers_short")}</Badge> : <Badge tone="online">{a.customers > 1 ? t("cust.n_customers", { count: a.customers }) : t("cust.this_customer")}</Badge>}</Td>
                  <Td className="text-xs text-muted">{a.scopes.join(", ")}</Td>
                  <Td className="text-sm text-muted">{timeAgo(a.last_used_at, i18n.language)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
        <CardBody className="border-t border-line text-sm text-muted">
          {t("cust.ai_manage_hint")} <Link to="/ai/tokens" className="font-semibold text-accent-text hover:underline">{t("nav.api_tokens")}</Link> · <Link to="/ai/connections" className="font-semibold text-accent-text hover:underline">{t("nav.ai_connections")}</Link>
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<Bot />} title={t("cust.ai_sees")} description={ctx.data ? <span className="mono break-all">{ctx.data.uri}</span> : undefined} />
        <CardBody>{ctx.data ? <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-md bg-sunken p-3 text-[12.5px] leading-relaxed">{ctx.data.markdown}</pre> : <Spinner />}</CardBody>
      </Card>
      <NewTokenDialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) qc.invalidateQueries({ queryKey: ["customer", c.id] }); }} customerIds={[c.id]} defaultName={`${c.name} AI`} />
    </div>
  );
}

/* ------------------------------------------------------------------ contacts & notes */
function ContactsTab({ c, admin, machines }: { c: Customer; admin: boolean; machines: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [notes, setNotes] = useState(c.notes ?? "");
  const [contacts, setContacts] = useState<CustomerContact[]>(c.contacts ?? []);
  const [confirmDel, setConfirmDel] = useState(false);
  useEffect(() => {
    setNotes(c.notes ?? "");
    setContacts(c.contacts ?? []);
  }, [c]);
  const save = useMutation({
    mutationFn: () => api(`api/customers/${c.id}`, { method: "PUT", allCustomers: true, json: { name: c.name, kind: c.kind, color: c.color, description: c.description, notes, contacts: contacts.filter((x) => x.name.trim()) } }),
    onSuccess: () => {
      toast.success(t("common.saved"));
      qc.invalidateQueries({ queryKey: ["customer", c.id] });
      qc.invalidateQueries({ queryKey: ["customers"] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/customers/${c.id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      navigate("/customers");
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const setC = (i: number, k: keyof CustomerContact, v: string) => setContacts(contacts.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const dirty = notes !== (c.notes ?? "") || JSON.stringify(contacts) !== JSON.stringify(c.contacts ?? []);
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <Card>
        <CardHeader icon={<Bot />} title={t("cust.notes_ai")} description={t("cust.notes_desc")} />
        <CardBody className="space-y-3">
          <Textarea className="min-h-56 font-sans text-sm" value={notes} onChange={(e) => setNotes(e.target.value)} disabled={!admin} placeholder={t("cust.notes_ph")} aria-label={t("cust.notes_ai")} />
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div><dt className="text-xs font-semibold text-muted">{t("cust.maintenance")}</dt><dd>{c.maintenance_window || "–"}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">{t("cust.sla")}</dt><dd>{c.sla || "–"}</dd></div>
          </dl>
          <p className="text-xs text-muted">{t("cust.notes_where")}</p>
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<UserRound />} title={t("cust.contacts")} description={t("cust.contacts_desc")}
          action={admin && <Button size="sm" onClick={() => setContacts([...contacts, { name: "", role: "", email: "", phone: "" }])}><Plus /> {t("cust.add_contact")}</Button>} />
        <CardBody className="space-y-4">
          {c.contact_name && (
            <div className="rounded-md bg-sunken p-3 text-sm">
              <div className="text-xs font-semibold text-muted">{t("cust.main_contact")}</div>
              <div className="font-semibold">{c.contact_name}</div>
              <div className="text-muted">{[c.contact_email, c.contact_phone].filter(Boolean).join(" · ")}</div>
            </div>
          )}
          {!contacts.length && <p className="text-sm text-muted">{t("cust.no_contacts")}</p>}
          {contacts.map((x, i) => (
            <div key={i} className="grid gap-2 rounded-md border border-line p-3 sm:grid-cols-2">
              <Field label={t("px.name")}><Input value={x.name} onChange={(e) => setC(i, "name", e.target.value)} disabled={!admin} /></Field>
              <Field label={t("cust.contact_role")}><Input value={x.role ?? ""} onChange={(e) => setC(i, "role", e.target.value)} disabled={!admin} placeholder={t("cust.contact_role_ph")} /></Field>
              <Field label={t("cust.contact_email")}><Input type="email" value={x.email ?? ""} onChange={(e) => setC(i, "email", e.target.value)} disabled={!admin} /></Field>
              <Field label={t("cust.contact_phone")}>
                <div className="flex gap-2"><Input value={x.phone ?? ""} onChange={(e) => setC(i, "phone", e.target.value)} disabled={!admin} />
                  {admin && <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => setContacts(contacts.filter((_, j) => j !== i))}><X /></Button>}</div>
              </Field>
            </div>
          ))}
        </CardBody>
      </Card>
      {admin && (
        <div className="flex flex-wrap items-center gap-3 xl:col-span-2">
          <Button variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
          <div className="flex-1" />
          <Button variant="danger" size="sm" disabled={machines > 0} title={machines ? t("cust.delete_hint") : undefined} onClick={() => setConfirmDel(true)}><Trash2 /> {t("cust.delete")}</Button>
        </div>
      )}
      <Dialog open={confirmDel} onOpenChange={setConfirmDel} title={t("cust.delete_title", { name: c.name })}
        footer={<><Button onClick={() => setConfirmDel(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>{t("common.delete")}</Button></>}>
        <p className="text-sm">{t("cust.delete_text")}</p>
      </Dialog>
    </div>
  );
}

export function useCustomerQuery(id: string) {
  return useQuery({ queryKey: ["customer", id], queryFn: () => getAll<Customer>(`api/customers/${id}`), enabled: !!id });
}

