import * as DM from "@radix-ui/react-dropdown-menu";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AppWindow, ArrowUpCircle, ChevronDown, Cpu, HardDrive, Laptop, LockKeyhole, LogOut, MessageSquare, Package,
  Plus, RefreshCw, Search, ShieldAlert, ShieldCheck, ShieldQuestion, Terminal, UserRound, Wrench,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { useCustomer } from "@/components/customers";
import { StatusDot, UsageBar, useEnvironments } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { errorMessage, get, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SecurityStatus, SoftwareHit, WorkstationDetail, WorkstationRow, WorkstationsOverview, WsCheck } from "@/lib/types";
import { cn, formatBytes, formatDate, timeAgo } from "@/lib/utils";
import { AddServerDialog, OsBadge } from "./Servers";

/* ------------------------------------------------------------------ shared bits */
export function SecurityBadge({ status }: { status: SecurityStatus | null | undefined }) {
  const { t } = useTranslation();
  if (!status) return <Badge className="whitespace-nowrap"><ShieldQuestion className="size-3" />{t("ws.sec_unknown")}</Badge>;
  const tone = status === "ok" ? "online" : status === "warning" ? "warning" : "offline";
  const Icon = status === "ok" ? ShieldCheck : ShieldAlert;
  return <Badge tone={tone} className="whitespace-nowrap"><Icon className="size-3" />{t(`ws.sec_${status}`)}</Badge>;
}

function issueText(t: (k: string, o?: Record<string, unknown>) => string, code: string, extra: Record<string, unknown> = {}) {
  const key = `ws.check.${code}`;
  const out = t(key, { ...extra, profiles: Array.isArray(extra.profiles) ? (extra.profiles as string[]).join(", ") : extra.profiles });
  return out === key ? code : out;
}

export function useWorkstations(customerId?: string) {
  const qs = customerId ? `?customer_id=${encodeURIComponent(customerId)}` : "";
  return useQuery({ queryKey: ["workstations", customerId ?? ""], queryFn: () => get<WorkstationsOverview>(`api/workstations${qs}`), refetchInterval: 30_000 });
}

/** The table of workstations, used on the Workstations page and on the customer page. */
export function WorkstationsTable({ rows, showCustomer = true }: { rows: WorkstationRow[]; showCustomer?: boolean }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="overflow-x-auto">
      <Table>
        <thead>
          <tr><Th>{t("ws.col_ws")}</Th><Th>{t("srv.col_os")}</Th><Th>{t("ws.col_user")}</Th><Th>{t("ws.col_security")}</Th><Th>{t("ws.col_encryption")}</Th><Th>{t("srv.col_updates")}</Th></tr>
        </thead>
        <tbody>
          {rows.map((w) => (
            <Tr key={w.id} className="cursor-pointer" onClick={() => navigate(`/workstations/${w.id}`)}>
              <Td>
                <div className="flex items-center gap-2"><StatusDot online={w.online} /><Link to={`/workstations/${w.id}`} className="font-bold hover:underline" onClick={(e) => e.stopPropagation()}>{w.name}</Link></div>
                <div className="whitespace-nowrap text-xs text-muted">
                  {showCustomer && w.customer}{showCustomer && " · "}{w.online ? t("px.online") : `${t("ws.last_seen")} ${timeAgo(w.last_seen_at, i18n.language)}`}
                </div>
                {w.model && <div className="max-w-56 truncate text-[11px] text-muted" title={w.model}>{w.model}</div>}
              </Td>
              <Td>
                <OsBadge kind={w.kind} />
                <div className="mt-1 max-w-52 truncate text-xs text-muted" title={w.os ?? ""}>{w.os}{w.os_build && ` · ${w.os_build}`}</div>
              </Td>
              <Td className="text-sm">{w.user ? <span className="inline-flex items-center gap-1.5"><UserRound className="size-3.5 text-muted" />{w.user}</span> : <span className="text-muted">–</span>}</Td>
              <Td>
                <SecurityBadge status={w.security} />
                {w.issues.length > 0 && (
                  <div className="mt-1 flex max-w-72 flex-wrap gap-1">
                    {w.issues.slice(0, 3).map((c) => <span key={c} className="rounded bg-sunken px-1.5 py-0.5 text-[11px] text-muted">{t(`ws.issue.${c}`, { defaultValue: c })}</span>)}
                    {w.issues.length > 3 && <span className="text-[11px] text-muted">+{w.issues.length - 3}</span>}
                  </div>
                )}
                {!w.inventory_at && <div className="mt-1 text-[11px] text-muted">{t("ws.no_inventory")}</div>}
              </Td>
              <Td>
                {w.encryption === "ok" ? <Badge tone="online" className="whitespace-nowrap"><LockKeyhole className="size-3" />{t("ws.encrypted")}</Badge>
                  : w.encryption ? <Badge tone="warning" className="whitespace-nowrap">{t("ws.not_encrypted")}</Badge> : <span className="text-muted">–</span>}
              </Td>
              <Td className="text-sm">
                {w.updates ? <Badge tone="accent">{w.updates}</Badge> : <span className="text-muted">0</span>}
                {w.reboot_required && <Badge tone="warning" className="ml-1">{t("ws.reboot")}</Badge>}
                <div className="mt-0.5 whitespace-nowrap text-[11px] text-muted">{t("ws.last_update")}: {w.last_update_installed ? timeAgo(w.last_update_installed, i18n.language) : "–"}</div>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

/* ------------------------------------------------------------------ list page */
export function WorkstationsPage() {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const envs = useEnvironments();
  const [env, setEnv] = useState("");
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "attention" | "offline">("all");
  const [add, setAdd] = useState(false);
  const list = useWorkstations(customerId || env || undefined);
  const tot = list.data?.totals;
  const rows = useMemo(() => (list.data?.workstations ?? []).filter((w) => {
    if (filter === "attention" && w.security !== "warning" && w.security !== "critical") return false;
    if (filter === "offline" && w.online) return false;
    const hay = `${w.name} ${w.user ?? ""} ${w.model ?? ""} ${w.customer ?? ""} ${w.serial ?? ""}`.toLowerCase();
    return !q || hay.includes(q.toLowerCase());
  }), [list.data, q, filter]);
  return (
    <>
      <PageHeader
        title={t("cust.ws_title")}
        description={t("ws.subtitle")}
        actions={<>
          {!customerId && (
            <Select value={env} onChange={(e) => setEnv(e.target.value)} className="h-9 w-auto py-0 text-[13px]" aria-label={t("px.environment")}>
              <option value="">{t("srv.all_envs")}</option>
              {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </Select>
          )}
          {admin && <Button variant="primary" onClick={() => setAdd(true)}><Plus /> {t("cust.ws_add")}</Button>}
        </>}
      />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("cust.workstations")} value={tot ? `${tot.online}/${tot.total}` : "…"} hint={t("ws.k_online_hint")} icon={<Laptop />} tone="info" />
        <Stat label={t("ws.k_security")} value={tot ? tot.critical + tot.warning : "…"} hint={tot ? t("ws.k_security_hint", { critical: tot.critical, warning: tot.warning }) : undefined} icon={<ShieldAlert />} tone={tot?.critical ? "offline" : tot?.warning ? "warning" : "online"} />
        <Stat label={t("ws.k_encryption")} value={tot?.not_encrypted ?? "…"} hint={t("ws.k_encryption_hint")} icon={<LockKeyhole />} tone={tot?.not_encrypted ? "warning" : "online"} />
        <Stat label={t("srv.k_updates")} value={tot?.updates ?? "…"} hint={t("ws.k_updates_hint")} icon={<ArrowUpCircle />} tone={tot?.updates ? "accent" : "online"} />
      </div>
      <div className="mt-6 grid gap-6 2xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="min-w-0">
          <div className="flex flex-wrap items-center gap-3 border-b border-line p-3">
            <div className="relative max-w-sm flex-1">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
              <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("ws.search")} aria-label={t("ws.search")} />
            </div>
            <Select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} className="h-10 w-auto" aria-label={t("ws.filter")}>
              <option value="all">{t("ws.f_all")}</option>
              <option value="attention">{t("ws.f_attention")}</option>
              <option value="offline">{t("ws.f_offline")}</option>
            </Select>
          </div>
          {list.isLoading ? <CardBody><Spinner /></CardBody> : !rows.length ? (
            <EmptyState icon={<Laptop />} title={t("cust.ws_empty")} action={admin && <Button variant="primary" onClick={() => setAdd(true)}><Plus /> {t("cust.ws_add")}</Button>}>{t("cust.ws_empty_text")}</EmptyState>
          ) : <WorkstationsTable rows={rows} />}
        </Card>
        <SoftwareSearchCard customerId={customerId || env || undefined} />
      </div>
      <AddServerDialog open={add} onOpenChange={setAdd} role="workstation" customerId={customerId || env || undefined} />
    </>
  );
}

function SoftwareSearchCard({ customerId }: { customerId?: string }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const res = useQuery({
    queryKey: ["ws-software", term, customerId ?? ""], enabled: term.length > 1,
    queryFn: () => get<SoftwareHit[]>(`api/workstations/software?q=${encodeURIComponent(term)}${customerId ? `&customer_id=${customerId}` : ""}`),
  });
  return (
    <Card className="self-start 2xl:sticky 2xl:top-6">
      <CardHeader icon={<Package />} title={t("ws.sw_title")} description={t("ws.sw_desc")} />
      <CardBody className="space-y-3">
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); setTerm(q.trim()); }}>
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("ws.sw_placeholder")} aria-label={t("ws.sw_title")} />
          <Button type="submit"><Search /></Button>
        </form>
        {res.isFetching && <Spinner />}
        {res.data && !res.data.length && <p className="text-sm text-muted">{t("ws.sw_none")}</p>}
        {res.data?.map((h) => (
          <div key={h.node_id} className="rounded-md border border-line p-2.5">
            <Link to={`/workstations/${h.node_id}`} className="flex items-center gap-2 text-sm font-bold hover:underline"><StatusDot online={h.online} />{h.machine}</Link>
            {h.customer && <div className="text-[11px] text-muted">{h.customer}</div>}
            <ul className="mt-1 space-y-0.5 text-xs">
              {h.software.slice(0, 6).map((s, i) => <li key={i} className="flex justify-between gap-2"><span className="truncate">{s.name}</span><span className="mono shrink-0 text-muted">{s.version}</span></li>)}
            </ul>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ detail page */
const checkTone = (s: WsCheck["status"]) => (s === "ok" ? "online" : s === "warning" ? "warning" : s === "critical" ? "offline" : "neutral") as "online" | "warning" | "offline" | "neutral";

function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-line py-1.5 text-sm last:border-0">
      <span className="text-muted">{label}</span><span className="min-w-0 text-right font-medium [overflow-wrap:anywhere]">{children ?? "–"}</span>
    </div>
  );
}

export function WorkstationDetailPage() {
  const { t, i18n } = useTranslation();
  const { id = "" } = useParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["workstation", id], queryFn: () => get<WorkstationDetail>(`api/workstations/${id}`), refetchInterval: 30_000 });
  const [msg, setMsg] = useState(false);
  const [logoff, setLogoff] = useState(false);
  const [swq, setSwq] = useState("");
  const refresh = useMutation({
    mutationFn: () => post<WorkstationDetail>(`api/workstations/${id}/inventory`),
    onSuccess: (d) => { qc.setQueryData(["workstation", id], d); qc.invalidateQueries({ queryKey: ["workstations"] }); toast.success(t("ws.inv_done")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const action = useMutation({
    mutationFn: (body: { action: string; message?: string; user?: string }) => post<{ ok: boolean; output: string }>(`api/workstations/${id}/actions`, body),
    onSuccess: (r) => (r.ok ? toast.success(t("ws.action_done"), { description: r.output?.slice(0, 200) }) : toast.error(t("ws.action_failed"), { description: r.output?.slice(0, 300) })),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <Alert tone="offline">{t("ws.not_found")}</Alert>;
  const w = q.data;
  const inv = w.inventory ?? {};
  const win = w.kind === "windows";
  const hw = inv.hardware ?? {};
  const os = inv.os ?? {};
  const d = inv.antivirus?.defender;
  const software = (inv.software ?? []).filter((s) => !swq || `${s.name} ${s.publisher ?? ""}`.toLowerCase().includes(swq.toLowerCase()));
  const order = { critical: 0, warning: 1, info: 2, ok: 3 } as const;
  const checks = [...w.checks].sort((a, b) => order[a.status] - order[b.status]);
  const bad = checks.filter((c) => c.status === "warning" || c.status === "critical");
  const actions: [string, ReactNode, string][] = win
    ? [["defender_quick_scan", <ShieldCheck key="q" className="size-4" />, t("ws.a_quick")], ["defender_full_scan", <ShieldCheck key="f" className="size-4" />, t("ws.a_full")],
      ["defender_update", <RefreshCw key="u" className="size-4" />, t("ws.a_update")], ["gpupdate", <Wrench key="g" className="size-4" />, t("ws.a_gpupdate")]]
    : [];
  return (
    <>
      <nav className="mb-1.5 text-[13px] text-muted" aria-label={t("cust.breadcrumb")}>
        <Link to="/workstations" className="hover:text-ink hover:underline">{t("cust.ws_title")}</Link>
        {w.customer && <> / <Link to={`/customers/${w.customer_id}?tab=workstations`} className="hover:text-ink hover:underline">{w.customer}</Link></>} / <span>{w.name}</span>
      </nav>
      <PageHeader
        title={<span className="flex items-center gap-3"><StatusDot online={w.online} /> {w.name}</span>}
        description={
          <span className="mt-1 flex flex-wrap items-center gap-2">
            <OsBadge kind={w.kind} label={[w.os, w.os_build].filter(Boolean).join(" · ")} />
            <SecurityBadge status={w.security} />
            {!w.online && <Badge>{t("ws.last_seen")} {timeAgo(w.last_seen_at, i18n.language)}</Badge>}
          </span>
        }
        actions={<>
          <Link to={`/servers/${w.id}`}><Button><Terminal /> {t("ws.machine_tools")}</Button></Link>
          {admin && <Button onClick={() => refresh.mutate()} disabled={!w.online || refresh.isPending}><RefreshCw className={cn(refresh.isPending && "animate-spin")} /> {t("ws.refresh")}</Button>}
          {admin && (
            <DM.Root>
              <DM.Trigger asChild><Button variant="primary" disabled={!w.online}><Wrench /> {t("ws.actions")} <ChevronDown /></Button></DM.Trigger>
              <DM.Portal>
                <DM.Content align="end" sideOffset={6} className="z-50 min-w-60 rounded-md border border-line bg-raised p-1 shadow-xl">
                  {actions.map(([k, icon, label]) => (
                    <DM.Item key={k} onSelect={() => action.mutate({ action: k })} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">{icon}{label}</DM.Item>
                  ))}
                  <DM.Item onSelect={() => setMsg(true)} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken"><MessageSquare className="size-4" />{t("ws.a_message")}</DM.Item>
                  <DM.Item onSelect={() => setLogoff(true)} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm text-offline outline-none data-[highlighted]:bg-sunken"><LogOut className="size-4" />{t("ws.a_logoff")}</DM.Item>
                </DM.Content>
              </DM.Portal>
            </DM.Root>
          )}
        </>}
      />
      {w.inventory_error && <Alert tone="warning" className="mb-4" title={t("ws.inv_error")}>{w.inventory_error}</Alert>}
      {!w.inventory_at ? (
        <Card><EmptyState icon={<ShieldQuestion />} title={t("ws.no_inventory")} action={admin && w.online && <Button variant="primary" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw /> {t("ws.refresh")}</Button>}>{t("ws.no_inventory_text")}</EmptyState></Card>
      ) : (
        <div className="grid gap-6 xl:grid-cols-3">
          <Card className="xl:col-span-2">
            <CardHeader icon={bad.length ? <ShieldAlert /> : <ShieldCheck />} title={t("ws.checks")} description={t("ws.checks_desc", { when: formatDate(w.inventory_at, i18n.language) })} />
            <CardBody className="grid gap-2 md:grid-cols-2">
              {checks.map((c, i) => (
                <div key={i} className="flex items-start gap-3 rounded-md border border-line px-3 py-2">
                  <Badge tone={checkTone(c.status)} className="mt-0.5 shrink-0">{t(`ws.st_${c.status}`)}</Badge>
                  <div className="min-w-0 text-sm"><div className="font-semibold">{t(`ws.key.${c.key}`, { defaultValue: c.key })}</div><div className="text-muted">{issueText(t, c.code, c)}</div></div>
                </div>
              ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<Cpu />} title={t("ws.hardware")} />
            <CardBody>
              <Row label={t("ws.model")}>{[hw.manufacturer, hw.model].filter(Boolean).join(" ") || "–"}</Row>
              <Row label={t("ws.serial")}><span className="mono">{hw.serial || "–"}</span></Row>
              <Row label="CPU">{hw.cpu}</Row>
              <Row label={t("ws.memory")}>{hw.memory ? formatBytes(hw.memory) : "–"}</Row>
              <Row label="BIOS">{hw.bios}</Row>
              {inv.tpm && <Row label="TPM">{inv.tpm.present ? (inv.tpm.ready ? t("ws.ready") : t("ws.present")) : t("ws.absent")}</Row>}
              <Row label="Secure Boot">{inv.secure_boot == null ? "–" : inv.secure_boot ? t("ws.on") : t("ws.off")}</Row>
              {inv.battery?.charge != null && <Row label={t("ws.battery")}>{inv.battery.charge}%</Row>}
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={win ? <AppWindow /> : <Terminal />} title={t("ws.os")} />
            <CardBody>
              <Row label={t("ws.os")}>{os.name}</Row>
              <Row label={t("ws.build")}>{[os.display_version, os.build].filter(Boolean).join(" · ") || "–"}</Row>
              {os.domain && <Row label={t("ws.domain")}>{os.domain}</Row>}
              <Row label={t("ws.installed_on")}>{os.install_date ? formatDate(os.install_date, i18n.language) : "–"}</Row>
              <Row label={t("ws.last_boot")}>{os.last_boot ? timeAgo(os.last_boot, i18n.language) : "–"}</Row>
              <Row label={t("ws.users")}>{(inv.users ?? []).join(", ") || (inv.last_user ? `${inv.last_user} (${t("ws.last")})` : "–")}</Row>
              <Row label={t("srv.col_agent")}><span className="mono">{w.agent_version ?? "–"}</span></Row>
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("ws.protection")} />
            <CardBody>
              <Row label={t("ws.antivirus")}>{w.antivirus ?? t("ws.none")}</Row>
              {d && <>
                <Row label={t("ws.realtime")}>{d.realtime ? t("ws.on") : t("ws.off")}</Row>
                <Row label={t("ws.signatures")}>{d.signature_age_days != null ? t("ws.days_old", { count: d.signature_age_days }) : "–"}</Row>
                <Row label={t("ws.quick_scan")}>{d.quick_scan_age_days != null ? t("ws.days_ago", { count: d.quick_scan_age_days }) : "–"}</Row>
                <Row label={t("ws.threats")}>{d.threats_30d ?? 0}</Row>
                <Row label={t("ws.tamper")}>{d.tamper ? t("ws.on") : t("ws.off")}</Row>
              </>}
              {(inv.antivirus?.products ?? []).filter((p) => !/defender/i.test(p.name ?? "")).map((p, i) => <Row key={i} label={p.name}>{p.enabled ? t("ws.on") : t("ws.off")}</Row>)}
              {(inv.firewall?.profiles ?? []).map((p) => <Row key={p.profile} label={`${t("ws.firewall")} ${p.profile}`}>{p.enabled ? t("ws.on") : t("ws.off")}</Row>)}
              {!win && <Row label={t("ws.firewall")}>{inv.firewall?.ufw ?? inv.firewall?.firewalld ?? (inv.firewall?.nft_rules ? `nftables (${inv.firewall.nft_rules})` : "–")}</Row>}
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<LockKeyhole />} title={t("ws.encryption")} />
            <CardBody>
              {win ? (inv.encryption?.volumes ?? []).map((v, i) => (
                <Row key={i} label={v.mount}>{v.protection === "On" ? <Badge tone="online">{t("ws.encrypted")}</Badge> : <Badge tone="warning">{t("ws.not_encrypted")}</Badge>}{v.method && v.method !== "None" && <span className="ml-1.5 text-xs text-muted">{v.method}</span>}</Row>
              )) : <Row label="LUKS">{inv.encryption?.luks ? <Badge tone="online">{t("ws.encrypted")}</Badge> : <Badge tone="warning">{t("ws.not_encrypted")}</Badge>}</Row>}
              {win && !(inv.encryption?.volumes ?? []).length && <p className="text-sm text-muted">{t("ws.no_bitlocker")}</p>}
            </CardBody>
          </Card>
          <Card className="xl:col-span-2">
            <CardHeader icon={<HardDrive />} title={t("ws.disks")} />
            <div className="overflow-x-auto">
              <Table>
                <thead><tr><Th>{t("ws.disk")}</Th><Th>{t("ws.type")}</Th><Th>{t("ws.size")}</Th><Th>{t("ws.health")}</Th><Th>{t("ws.wear")}</Th></tr></thead>
                <tbody>
                  {(inv.disks ?? []).map((x, i) => (
                    <Tr key={i}><Td className="text-sm font-semibold">{x.name}</Td><Td className="text-sm">{[x.media, x.bus].filter(Boolean).join(" · ")}</Td><Td className="text-sm">{formatBytes(x.size ?? 0)}</Td>
                      <Td><Badge tone={/healthy/i.test(x.health ?? "") ? "online" : /unknown/i.test(x.health ?? "") ? "neutral" : "offline"}>{x.health ?? "–"}</Badge></Td>
                      <Td className="text-sm">{x.wear != null ? `${x.wear}%` : "–"}{x.temperature ? <span className="ml-2 text-xs text-muted">{x.temperature} °C</span> : null}</Td></Tr>
                  ))}
                </tbody>
              </Table>
            </div>
            <CardBody className="space-y-3">
              {(inv.volumes ?? []).map((v, i) => (
                <UsageBar key={i} label={`${v.mount}${v.label && !v.label.startsWith("/dev") ? ` (${v.label})` : ""}`} used={(v.size ?? 0) - (v.free ?? 0)} total={v.size ?? 0} />
              ))}
            </CardBody>
          </Card>
          <Card className="xl:col-span-3">
            <CardHeader icon={<ArrowUpCircle />} title={t("ws.updates")} description={inv.updates?.last_installed ? t("ws.last_installed", { when: formatDate(inv.updates.last_installed, i18n.language) }) : undefined}
              action={<Link to={`/servers/${w.id}?tab=updates`} className="text-sm font-semibold text-accent-text hover:underline">{t("ws.manage_updates")}</Link>} />
            <div className="grid gap-0 lg:grid-cols-2">
              <div className="border-line lg:border-r">
                <h3 className="px-5 pt-3 text-[13px] font-bold text-muted">{t("ws.pending", { count: w.pending_updates.length })}</h3>
                <ul className="max-h-72 overflow-y-auto px-5 py-2 text-sm">
                  {w.pending_updates.length ? w.pending_updates.map((p, i) => <li key={i} className="flex justify-between gap-2 border-b border-line py-1 last:border-0"><span className="truncate">{p.name}</span>{p.security && <Badge tone="warning">{t("ws.security_update")}</Badge>}</li>) : <li className="text-muted">{t("ws.none_pending")}</li>}
                </ul>
              </div>
              <div>
                <h3 className="px-5 pt-3 text-[13px] font-bold text-muted">{t("ws.history")}</h3>
                <ul className="max-h-72 overflow-y-auto px-5 py-2 text-sm">
                  {(inv.updates?.history ?? []).slice(0, 25).map((h, i) => (
                    <li key={i} className="flex justify-between gap-3 border-b border-line py-1 last:border-0">
                      <span className="min-w-0 truncate" title={h.title}>{h.title}</span>
                      <span className="shrink-0 text-xs text-muted">{h.date ? formatDate(h.date, i18n.language) : ""} {h.result && h.result !== "Succeeded" && <Badge tone="offline">{h.result}</Badge>}</span>
                    </li>
                  ))}
                  {!(inv.updates?.history ?? []).length && <li className="text-muted">–</li>}
                </ul>
              </div>
            </div>
          </Card>
          <Card className="xl:col-span-3">
            <CardHeader icon={<Package />} title={t("ws.software", { count: inv.software?.length ?? 0 })}
              action={<div className="relative w-56"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input className="h-9 pl-9" value={swq} onChange={(e) => setSwq(e.target.value)} placeholder={t("ws.sw_filter")} aria-label={t("ws.sw_filter")} /></div>} />
            <div className="max-h-[480px] overflow-auto">
              <Table>
                <thead><tr><Th>{t("ws.sw_name")}</Th><Th>{t("ws.sw_version")}</Th><Th>{t("ws.sw_publisher")}</Th></tr></thead>
                <tbody>
                  {software.slice(0, 500).map((s, i) => <Tr key={i}><Td className="text-sm">{s.name}</Td><Td className="mono text-xs">{s.version}</Td><Td className="text-sm text-muted">{s.publisher ?? ""}</Td></Tr>)}
                </tbody>
              </Table>
            </div>
          </Card>
        </div>
      )}
      <MessageDialog open={msg} onOpenChange={setMsg} pending={action.isPending} onSend={(m) => action.mutate({ action: "message", message: m }, { onSuccess: () => setMsg(false) })} />
      <LogoffDialog open={logoff} onOpenChange={setLogoff} name={w.name} users={inv.users ?? []} pending={action.isPending}
        onConfirm={(u) => action.mutate({ action: "logoff", user: u }, { onSuccess: () => setLogoff(false) })} />
    </>
  );
}

function MessageDialog({ open, onOpenChange, onSend, pending }: { open: boolean; onOpenChange: (v: boolean) => void; onSend: (m: string) => void; pending: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("ws.a_message")} description={t("ws.message_desc")}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" disabled={!text.trim() || pending} onClick={() => onSend(text)}><MessageSquare /> {t("ws.send")}</Button></>}>
      <Field label={t("ws.message")}><Textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={500} className="font-sans" /></Field>
    </Dialog>
  );
}

function LogoffDialog({ open, onOpenChange, onConfirm, name, users, pending }: {
  open: boolean; onOpenChange: (v: boolean) => void; onConfirm: (u: string) => void; name: string; users: string[]; pending: boolean;
}) {
  const { t } = useTranslation();
  const [user, setUser] = useState("");
  const [confirm, setConfirm] = useState("");
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("ws.a_logoff")} description={t("ws.logoff_desc")}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="danger" disabled={!user || confirm !== name || pending} onClick={() => onConfirm(user)}><LogOut /> {t("ws.a_logoff")}</Button></>}>
      <div className="space-y-4">
        <Field label={t("ws.user")}>
          {users.length ? (
            <Select value={user} onChange={(e) => setUser(e.target.value)}><option value="">–</option>{users.map((u) => <option key={u} value={u}>{u}</option>)}</Select>
          ) : <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="piet" />}
        </Field>
        <Field label={t("ws.confirm_name", { name })}><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} /></Field>
        <Alert tone="warning">{t("ws.logoff_warn")}</Alert>
      </div>
    </Dialog>
  );
}

