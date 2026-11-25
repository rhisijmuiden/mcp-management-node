import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlarmClock, ArrowLeft, Building2, ChevronLeft, ChevronRight, Cpu, Download, FileSpreadsheet, FileText, HardDriveDownload,
  Lock, LockOpen, Pencil, Plus, Receipt, RefreshCw, Settings2, Tags, Trash2, TrendingUp, Wallet,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { apiUrl, del, errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
type Kind = "licence" | "contract" | "hardware" | "service" | "other";
type Billing = "monthly" | "quarterly" | "yearly" | "once" | "none";
type ItemStatus = "ok" | "notice" | "expiring" | "expired" | "renews" | "none" | "inactive";
const KINDS: Kind[] = ["licence", "contract", "hardware", "service", "other"];
const BILLINGS: Billing[] = ["monthly", "quarterly", "yearly", "once", "none"];
const STATUS_TONE: Record<ItemStatus, "online" | "warning" | "offline" | "info" | "neutral"> = {
  ok: "online", notice: "warning", expiring: "warning", expired: "offline", renews: "info", none: "neutral", inactive: "neutral",
};
const STANDARD_KEYS = ["resource.cpu", "resource.ram", "resource.storage", "device.server", "device.workstation", "device.proxmox", "m365.user"];

interface Line {
  group: string; key: string; std: string | null; name: string | null; detail: string | null; quantity: number; unit: string | null;
  unit_price: number | null; amount: number | null; cost: number | null; billing: Billing | null; item_id: string | null; missing_price: boolean;
}
interface Totals { subtotal: number; discount_pct: number; discount: number; net: number; vat_pct: number; vat: number; total: number; cost: number | null; margin: number | null; missing_prices: number }
interface Usage { cpu: number; ram_gb: number; storage_gb: number; guests: number; devices: Record<string, number>; m365_users: number; basis: string; days: number; estimated: boolean }
interface GuestUse { vmid: number; name: string; type: string; status: string; node: string; cpu: number; ram_gb: number; storage_gb: number }
interface Statement {
  customer: { id: string; name: string; debtor: string | null }; period: string; status: "draft" | "final"; currency: string; lines: Line[];
  totals: Totals; monthly_recurring: number | null; usage: Usage; guests?: GuestUse[]; billable: boolean; note: string | null;
  finalized_at?: string; finalized_by?: string | null; final_note?: string | null;
}
interface OverviewRow {
  customer_id: string; customer_name: string; kind: string; billable: boolean; debtor: string | null; status: "draft" | "final"; lines: number;
  net: number; vat: number; total: number; cost: number | null; margin: number | null; missing_prices: number; monthly_recurring: number | null;
  estimated: boolean; expiring: number;
}
interface Overview {
  period: string; currency: string; customers: OverviewRow[]; totals: { net: number; total: number; cost: number; monthly_recurring: number };
  expiring: number; final: number; current_period: string; previous_period: string;
}
interface Item {
  id: string; customer_id: string; customer_name: string | null; kind: Kind; name: string; vendor: string | null; reference: string | null;
  serial: string | null; node_id: string | null; node_name: string | null; quantity: number; unit_price: number | null; cost_price: number | null;
  billing: Billing; start_date: string | null; end_date: string | null; auto_renew: boolean; renew_months: number | null; notice_days: number | null;
  warn_days: number | null; active: boolean; notes: string | null; monthly: number | null; status: ItemStatus; days_left: number | null;
  notice_by: string | null; notice_days_left: number | null; history: { at: string; from: string; to: string; by: string }[];
}
interface ItemList { items: Item[]; counts: Record<ItemStatus, number>; warn_days: number; monthly: number | null }
interface Rate { id: string; customer_id: string | null; key: string; name: string | null; unit: string | null; price: number; cost: number | null; updated_at: string; updated_by: string | null }
interface Rates { defaults: Rate[]; customer: Rate[]; standard_keys: string[]; sku_names: Record<string, string>; unpriced_skus: { key: string; name: string }[] }
interface CustomerSettings { billable: boolean; discount_pct: number; debtor: string; bill_resources: boolean; bill_devices: boolean; bill_m365: boolean; note: string }
interface Settings { currency: string; vat_pct: number; warn_days: number; alert_expiring: boolean; auto_renew: boolean; usage_basis: "average" | "peak"; include_stopped: boolean; m365_quantity: "enabled" | "consumed" }

const TABS = ["overview", "items", "rates", "settings"] as const;
type Tab = (typeof TABS)[number];

function useMoney() {
  const { i18n } = useTranslation();
  return (v: number | null | undefined, currency = "EUR") =>
    v == null ? "–" : new Intl.NumberFormat(i18n.language === "nl" ? "nl-NL" : "en-GB", { style: "currency", currency }).format(v);
}

function useNum() {
  const { i18n } = useTranslation();
  return (v: number | null | undefined, digits = 2) =>
    v == null ? "–" : new Intl.NumberFormat(i18n.language === "nl" ? "nl-NL" : "en-GB", { maximumFractionDigits: digits }).format(v);
}

function day(v: string, lang: string) {
  const [y, m, d] = v.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(lang === "nl" ? "nl-NL" : "en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function shiftPeriod(p: string, by: number): string {
  const [y, m] = p.split("-").map(Number);
  const d = new Date(y, m - 1 + by, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function periodLabel(period: string, lang: string) {
  const [y, m] = period.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(lang === "nl" ? "nl-NL" : "en-GB", { month: "long", year: "numeric" });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["costs"] });
}

function lineName(x: Line, t: (k: string, o?: Record<string, unknown>) => string) {
  if (x.name) return x.name;
  if (x.std) return t(`costs.std.${x.std}`, { defaultValue: x.std });
  return x.key;
}

function rateLabel(key: string, t: (k: string, o?: Record<string, unknown>) => string, name?: string | null) {
  if (STANDARD_KEYS.includes(key)) return t(`costs.std.${key}`);
  if (key.startsWith("m365.sku.")) return name || key.slice(9).replaceAll("_", " ");
  return name || key;
}

/* ------------------------------------------------------------------ page */
export function CostsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const params = useParams();
  const navigate = useNavigate();
  const tab: Tab = (TABS as readonly string[]).includes(params.tab ?? "") ? (params.tab as Tab) : "overview";
  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><Wallet className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("costs.title")}</span></span>}
        description={t("costs.desc")} />
      <Tabs.Root value={tab} onValueChange={(v) => navigate(`/costs/${v}`)}>
        <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
          {TABS.filter((x) => x !== "settings" || admin).map((x) => <Tabs.Trigger key={x} value={x} className={tabCls}>{t(`costs.tab.${x}`)}</Tabs.Trigger>)}
        </Tabs.List>
        <Tabs.Content value="overview"><OverviewTab admin={admin} /></Tabs.Content>
        <Tabs.Content value="items"><ItemsTab admin={admin} /></Tabs.Content>
        <Tabs.Content value="rates"><RatesTab admin={admin} /></Tabs.Content>
        {admin && <Tabs.Content value="settings"><SettingsTab /></Tabs.Content>}
      </Tabs.Root>
    </div>
  );
}

/* ------------------------------------------------------------------ overview */
function PeriodPicker({ value, onChange }: { value: string; onChange: (p: string) => void }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="flex items-center gap-1">
      <Button size="sm" variant="ghost" onClick={() => onChange(shiftPeriod(value, -1))} title={t("costs.prev_month")}><ChevronLeft /></Button>
      <Input type="month" className="w-auto" value={value} onChange={(e) => e.target.value && onChange(e.target.value)} aria-label={t("costs.period")} />
      <Button size="sm" variant="ghost" onClick={() => onChange(shiftPeriod(value, 1))} title={t("costs.next_month")}><ChevronRight /></Button>
      <span className="ml-1 hidden text-sm font-semibold capitalize text-muted sm:inline">{periodLabel(value, i18n.language)}</span>
    </div>
  );
}

function OverviewTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const money = useMoney();
  const { customerId } = useCustomer();
  const [period, setPeriod] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["costs", "overview", period, customerId],
    queryFn: () => get<Overview>(`api/costs/overview${period ? `?period=${period}` : ""}`),
  });
  useEffect(() => { if (q.data && !period) setPeriod(q.data.period); }, [q.data, period]);
  const shown = customerId || open;
  if (q.isLoading || !period) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const d = q.data!;
  const rows = d.customers;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PeriodPicker value={period} onChange={setPeriod} />
        {!shown && (
          <a href={apiUrl(`api/costs/export?period=${period}&lang=${i18n.language === "nl" ? "nl" : "en"}`)}>
            <Button variant="secondary"><FileSpreadsheet /> {t("costs.export_all")}</Button>
          </a>
        )}
      </div>
      {shown ? (
        <StatementView customerId={shown} period={period} admin={admin} onBack={!customerId ? () => setOpen(null) : undefined} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <Stat label={t("costs.k_net")} value={money(d.totals.net, d.currency)} tone="info" icon={<Receipt />} hint={t("costs.k_net_hint", { total: money(d.totals.total, d.currency) })} />
            <Stat label={t("costs.k_mrr")} value={money(d.totals.monthly_recurring, d.currency)} tone="online" icon={<TrendingUp />} hint={t("costs.k_mrr_hint")} />
            <Stat label={t("costs.k_final")} value={`${d.final} / ${rows.length}`} tone={d.final === rows.length ? "online" : "neutral"} icon={<Lock />} hint={t("costs.k_final_hint")} />
            <Stat label={t("costs.k_expiring")} value={d.expiring} tone={d.expiring ? "warning" : "online"} icon={<AlarmClock />} hint={t("costs.k_expiring_hint")} />
          </div>
          <Card>
            <CardHeader icon={<Building2 />} title={t("costs.per_customer")} description={t("costs.per_customer_desc")} />
            {!rows.length ? <EmptyState icon={<Wallet />} title={t("costs.no_customers")} /> : (
              <Table>
                <thead><Tr><Th>{t("costs.col_customer")}</Th><Th>{t("costs.col_status")}</Th><Th className="text-right">{t("costs.col_net")}</Th><Th className="text-right">{t("costs.col_mrr")}</Th><Th className="text-right">{t("costs.col_margin")}</Th><Th>{t("costs.col_attention")}</Th></Tr></thead>
                <tbody>
                  {rows.map((r) => (
                    <Tr key={r.customer_id} className="cursor-pointer" onClick={() => setOpen(r.customer_id)}>
                      <Td>
                        <div className="font-semibold">{r.customer_name}</div>
                        <div className="text-xs text-muted">{r.debtor ? `${t("costs.debtor")} ${r.debtor} · ` : ""}{t("costs.n_lines", { count: r.lines })}</div>
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
                          <Badge tone={r.status === "final" ? "online" : "neutral"} dot>{t(`costs.st_${r.status}`)}</Badge>
                          {!r.billable && <Badge tone="neutral">{t("costs.not_billable")}</Badge>}
                        </div>
                      </Td>
                      <Td className="text-right font-semibold tabular-nums">{money(r.net, d.currency)}</Td>
                      <Td className="text-right tabular-nums">{money(r.monthly_recurring, d.currency)}</Td>
                      <Td className={cn("text-right tabular-nums", (r.margin ?? 0) < 0 && "text-offline")}>{money(r.margin, d.currency)}</Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
                          {r.missing_prices > 0 && <Badge tone="warning">{t("costs.missing_n", { count: r.missing_prices })}</Badge>}
                          {r.expiring > 0 && <Badge tone="warning">{t("costs.expiring_n", { count: r.expiring })}</Badge>}
                          {r.estimated && <Badge tone="neutral">{t("costs.estimated")}</Badge>}
                        </div>
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function StatementView({ customerId, period, admin, onBack }: { customerId: string; period: string; admin: boolean; onBack?: () => void }) {
  const { t, i18n } = useTranslation();
  const money = useMoney();
  const num = useNum();
  const invalidate = useInvalidate();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [finalOpen, setFinalOpen] = useState(false);
  const [showGuests, setShowGuests] = useState(false);
  const q = useQuery({ queryKey: ["costs", "statement", customerId, period], queryFn: () => get<Statement>(`api/costs/statement/${customerId}?period=${period}`) });
  const reopen = useMutation({
    mutationFn: () => del(`api/costs/statement/${customerId}/${period}`),
    onSuccess: () => { toast.success(t("costs.reopened")); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const s = q.data!;
  const cur = s.currency;
  const lang = i18n.language === "nl" ? "nl" : "en";
  const base = `api/costs/statement/${customerId}/export?period=${period}&lang=${lang}`;
  let group = "";
  const u = s.usage;
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Receipt />}
          title={<span className="flex flex-wrap items-center gap-2">{onBack && <Button size="sm" variant="ghost" onClick={onBack} title={t("common.back")}><ArrowLeft /></Button>}{s.customer.name}<Badge tone={s.status === "final" ? "online" : "neutral"} dot>{t(`costs.st_${s.status}`)}</Badge>{!s.billable && <Badge tone="neutral">{t("costs.not_billable")}</Badge>}</span>}
          description={s.status === "final" ? t("costs.final_desc", { date: formatDate(s.finalized_at ?? null, i18n.language), by: s.finalized_by ?? "?" }) : t("costs.draft_desc")}
          action={
            <div className="flex flex-wrap gap-2">
              <a href={apiUrl(`${base}&format=csv`)}><Button size="sm" variant="ghost"><FileSpreadsheet /> CSV</Button></a>
              <a href={apiUrl(`${base}&format=pdf`)}><Button size="sm" variant="ghost"><Download /> PDF</Button></a>
              {admin && <Button size="sm" variant="ghost" onClick={() => setSettingsOpen(true)}><Settings2 /> {t("costs.customer_settings")}</Button>}
              {admin && (s.status === "final"
                ? <Button size="sm" variant="secondary" loading={reopen.isPending} onClick={() => reopen.mutate()}><LockOpen /> {t("costs.reopen")}</Button>
                : <Button size="sm" variant="primary" onClick={() => setFinalOpen(true)}><Lock /> {t("costs.finalize")}</Button>)}
            </div>
          } />
        {s.totals.missing_prices > 0 && <div className="px-5 pt-4"><Alert tone="warning">{t("costs.missing_alert", { count: s.totals.missing_prices })}</Alert></div>}
        {!s.lines.length ? <EmptyState icon={<Receipt />} title={t("costs.no_lines")}>{t("costs.no_lines_hint")}</EmptyState> : (
          <Table>
            <thead><Tr><Th>{t("costs.col_description")}</Th><Th className="text-right">{t("costs.col_quantity")}</Th><Th className="text-right">{t("costs.col_price")}</Th><Th className="text-right">{t("costs.col_amount")}</Th></Tr></thead>
            <tbody>
              {s.lines.map((x) => {
                const head = x.group !== group ? (group = x.group) : null;
                return [
                  head && <Tr key={`g-${head}`}><Td colSpan={4} className="bg-sunken py-1.5 text-xs font-bold uppercase tracking-wide text-muted">{t(`costs.group.${head}`, { defaultValue: head })}</Td></Tr>,
                  <Tr key={x.key}>
                    <Td>
                      <div className="font-medium">{lineName(x, t)}{x.billing && x.billing !== "monthly" && <span className="ml-1.5 text-xs text-muted">({t(`costs.billing.${x.billing}`)})</span>}</div>
                      {x.detail && <div className="text-xs text-muted">{x.detail}</div>}
                    </Td>
                    <Td className="text-right tabular-nums">{num(x.quantity)} {x.unit ? t(`costs.unit.${x.unit}`, { defaultValue: x.unit }) : ""}</Td>
                    <Td className="text-right tabular-nums">{x.unit_price == null ? <Badge tone="warning">{t("costs.no_price")}</Badge> : money(x.unit_price, cur)}</Td>
                    <Td className="text-right font-semibold tabular-nums">{money(x.amount, cur)}</Td>
                  </Tr>,
                ];
              })}
            </tbody>
          </Table>
        )}
        <CardBody>
          <dl className="ml-auto grid max-w-sm gap-1.5 text-sm">
            <TotalRow k={t("costs.subtotal")} v={money(s.totals.subtotal, cur)} />
            {s.totals.discount > 0 && <TotalRow k={t("costs.discount", { pct: num(s.totals.discount_pct) })} v={money(-s.totals.discount, cur)} />}
            <TotalRow k={t("costs.net")} v={money(s.totals.net, cur)} strong />
            <TotalRow k={t("costs.vat", { pct: num(s.totals.vat_pct) })} v={money(s.totals.vat, cur)} />
            <TotalRow k={t("costs.total")} v={money(s.totals.total, cur)} strong />
            {s.totals.cost != null && <TotalRow k={t("costs.margin")} v={`${money(s.totals.margin, cur)} (${t("costs.purchase")} ${money(s.totals.cost, cur)})`} muted />}
            {s.monthly_recurring != null && <TotalRow k={t("costs.mrr")} v={money(s.monthly_recurring, cur)} muted />}
          </dl>
          {(s.final_note || s.note) && <p className="mt-3 text-sm italic text-muted">{s.final_note || s.note}</p>}
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<Cpu />} title={t("costs.usage_title")}
          description={u.estimated ? t("costs.usage_estimated") : t("costs.usage_desc", { basis: t(`costs.basis.${u.basis}`), count: u.days })}
          action={s.guests?.length ? <Button size="sm" variant="ghost" onClick={() => setShowGuests(!showGuests)}>{showGuests ? t("costs.hide_guests") : t("costs.show_guests", { count: s.guests.length })}</Button> : undefined} />
        <CardBody>
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Mini k={t("costs.u_cpu")} v={num(u.cpu)} />
            <Mini k={t("costs.u_ram")} v={`${num(u.ram_gb)} GB`} />
            <Mini k={t("costs.u_storage")} v={`${num(u.storage_gb)} GB`} />
            <Mini k={t("costs.u_guests")} v={num(u.guests, 0)} />
            <Mini k={t("costs.u_servers")} v={num(u.devices.server ?? 0, 0)} />
            <Mini k={t("costs.u_workstations")} v={num(u.devices.workstation ?? 0, 0)} />
            <Mini k={t("costs.u_nodes")} v={num(u.devices.proxmox ?? 0, 0)} />
            <Mini k={t("costs.u_m365")} v={num(u.m365_users, 0)} />
          </div>
        </CardBody>
        {showGuests && s.guests && (
          <Table>
            <thead><Tr><Th>{t("costs.col_guest")}</Th><Th>{t("costs.col_node")}</Th><Th className="text-right">vCPU</Th><Th className="text-right">{t("costs.u_ram")}</Th><Th className="text-right">{t("costs.u_storage")}</Th></Tr></thead>
            <tbody>
              {s.guests.map((g) => (
                <Tr key={`${g.node}-${g.vmid}`}>
                  <Td><span className="font-medium">{g.vmid} · {g.name}</span> {g.status !== "running" && <Badge tone="neutral">{g.status}</Badge>}</Td>
                  <Td className="text-xs text-muted">{g.node}</Td>
                  <Td className="text-right tabular-nums">{g.cpu}</Td>
                  <Td className="text-right tabular-nums">{num(g.ram_gb)} GB</Td>
                  <Td className="text-right tabular-nums">{num(g.storage_gb)} GB</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {admin && settingsOpen && <CustomerSettingsDialog customerId={customerId} onClose={() => setSettingsOpen(false)} />}
      {admin && finalOpen && <FinalizeDialog customerId={customerId} period={period} net={money(s.totals.net, cur)} onClose={() => setFinalOpen(false)} />}
    </div>
  );
}

function TotalRow({ k, v, strong, muted }: { k: string; v: string; strong?: boolean; muted?: boolean }) {
  return (
    <div className={cn("flex items-baseline justify-between gap-4", strong && "border-t border-line pt-1.5 font-bold", muted && "text-xs text-muted")}>
      <dt>{k}</dt><dd className="tabular-nums">{v}</dd>
    </div>
  );
}

function Mini({ k, v }: { k: string; v: ReactNode }) {
  return <div className="rounded-md border border-line bg-sunken px-3 py-2"><div className="text-xs text-muted">{k}</div><div className="font-semibold tabular-nums">{v}</div></div>;
}

function FinalizeDialog({ customerId, period, net, onClose }: { customerId: string; period: string; net: string; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const [note, setNote] = useState("");
  const go = useMutation({
    mutationFn: () => post(`api/costs/statement/${customerId}/finalize`, { period, note: note || null }),
    onSuccess: () => { toast.success(t("costs.finalized")); invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("costs.finalize_title", { period: periodLabel(period, i18n.language) })} description={t("costs.finalize_desc", { net })}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={go.isPending} onClick={() => go.mutate()}><Lock /> {t("costs.finalize")}</Button></>}>
      <Field label={t("costs.final_note")} hint={t("costs.final_note_hint")}><Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} placeholder={t("costs.final_note_ph")} /></Field>
    </Dialog>
  );
}

function CustomerSettingsDialog({ customerId, onClose }: { customerId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const q = useQuery({ queryKey: ["costs", "customer-settings", customerId], queryFn: () => get<CustomerSettings>(`api/costs/customers/${customerId}/settings`) });
  const [f, setF] = useState<CustomerSettings | null>(null);
  useEffect(() => { if (q.data && !f) setF(q.data); }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => put(`api/costs/customers/${customerId}/settings`, f),
    onSuccess: () => { toast.success(t("common.saved")); invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t("costs.customer_settings")} description={t("costs.customer_settings_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!f} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      {!f ? <Spinner /> : (
        <div className="space-y-4">
          <Switch checked={f.billable} onCheckedChange={(v) => setF({ ...f, billable: v })} label={t("costs.cs_billable")} description={t("costs.cs_billable_hint")} />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("costs.cs_debtor")} hint={t("costs.cs_debtor_hint")}><Input value={f.debtor} onChange={(e) => setF({ ...f, debtor: e.target.value })} maxLength={100} /></Field>
            <Field label={t("costs.cs_discount")}><Input type="number" min={0} max={100} step="0.5" value={f.discount_pct} onChange={(e) => setF({ ...f, discount_pct: Number(e.target.value) })} /></Field>
          </div>
          <Switch checked={f.bill_resources} onCheckedChange={(v) => setF({ ...f, bill_resources: v })} label={t("costs.cs_resources")} description={t("costs.cs_resources_hint")} />
          <Switch checked={f.bill_devices} onCheckedChange={(v) => setF({ ...f, bill_devices: v })} label={t("costs.cs_devices")} />
          <Switch checked={f.bill_m365} onCheckedChange={(v) => setF({ ...f, bill_m365: v })} label={t("costs.cs_m365")} />
          <Field label={t("costs.cs_note")} hint={t("costs.cs_note_hint")}><Textarea rows={2} value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} maxLength={500} /></Field>
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ items */
function ItemsTab({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const money = useMoney();
  const num = useNum();
  const { customerId } = useCustomer();
  const invalidate = useInvalidate();
  const [kind, setKind] = useState<Kind | "">("");
  const [status, setStatus] = useState<ItemStatus | "soon" | "">("");
  const [edit, setEdit] = useState<Item | "new" | null>(null);
  const q = useQuery({
    queryKey: ["costs", "items", customerId, kind, status],
    queryFn: () => {
      const p = new URLSearchParams();
      if (customerId) p.set("customer_id", customerId);
      if (kind) p.set("kind", kind);
      if (status === "soon") p.set("within_days", "60");
      else if (status) p.set("status", status);
      return get<ItemList>(`api/costs/items?${p}`);
    },
  });
  const imp = useMutation({
    mutationFn: () => post<{ created: number }>("api/costs/items/import-hardware", { customer_id: customerId }),
    onSuccess: (r) => { toast.success(t("costs.imported", { count: r.created })); invalidate(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const list = q.data?.items ?? [];
  const c = q.data?.counts;
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label={t("costs.k_items")} value={list.length} tone="info" icon={<Tags />} />
        <Stat label={t("costs.k_items_mrr")} value={money(q.data?.monthly ?? 0)} tone="online" icon={<TrendingUp />} hint={t("costs.k_items_mrr_hint")} />
        <Stat label={t("costs.k_notice")} value={c?.notice ?? 0} tone={c?.notice ? "warning" : "online"} icon={<AlarmClock />} hint={t("costs.k_notice_hint")} />
        <Stat label={t("costs.k_ended")} value={(c?.expired ?? 0) + (c?.expiring ?? 0)} tone={c?.expired ? "offline" : c?.expiring ? "warning" : "online"} icon={<AlarmClock />} hint={t("costs.k_ended_hint", { expired: c?.expired ?? 0, days: q.data?.warn_days ?? 60 })} />
      </div>
      <Card>
        <CardHeader icon={<Tags />} title={t("costs.items_title")} description={t("costs.items_desc")}
          action={admin && (
            <div className="flex flex-wrap gap-2">
              {customerId && <Button size="sm" variant="ghost" loading={imp.isPending} onClick={() => imp.mutate()} title={t("costs.import_hw_hint")}><HardDriveDownload /> {t("costs.import_hw")}</Button>}
              <Button size="sm" variant="primary" onClick={() => setEdit("new")}><Plus /> {t("costs.add_item")}</Button>
            </div>
          )} />
        <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
          <Select className="w-auto" value={kind} onChange={(e) => setKind(e.target.value as Kind | "")}>
            <option value="">{t("costs.all_kinds")}</option>
            {KINDS.map((k) => <option key={k} value={k}>{t(`costs.kind.${k}`)}</option>)}
          </Select>
          <Select className="w-auto" value={status} onChange={(e) => setStatus(e.target.value as ItemStatus | "soon" | "")}>
            <option value="">{t("costs.all_statuses")}</option>
            <option value="soon">{t("costs.soon", { days: q.data?.warn_days ?? 60 })}</option>
            {(["notice", "expiring", "expired", "renews", "ok", "none", "inactive"] as ItemStatus[]).map((s) => <option key={s} value={s}>{t(`costs.status.${s}`)} ({c?.[s] ?? 0})</option>)}
          </Select>
        </div>
        {q.isLoading ? <div className="p-5"><Spinner /></div> : q.isError ? <div className="p-5"><Alert tone="offline">{errorMessage(q.error, t)}</Alert></div> : !list.length ? (
          <EmptyState icon={<Tags />} title={t("costs.no_items")}>{t("costs.no_items_hint")}</EmptyState>
        ) : (
          <Table>
            <thead><Tr><Th>{t("costs.col_item")}</Th><Th>{t("costs.col_kind")}</Th><Th className="text-right">{t("costs.col_price")}</Th><Th>{t("costs.col_end")}</Th><Th>{t("costs.col_status")}</Th>{admin && <Th />}</Tr></thead>
            <tbody>
              {list.map((x) => (
                <Tr key={x.id} className={cn(!x.active && "opacity-60")}>
                  <Td className="max-w-xs">
                    <div className="truncate font-semibold">{x.name}</div>
                    <div className="truncate text-xs text-muted">{[x.vendor, x.reference, x.serial, x.node_name, !customerId && x.customer_name].filter(Boolean).join(" · ")}</div>
                  </Td>
                  <Td><Badge tone="neutral">{t(`costs.kind.${x.kind}`)}</Badge></Td>
                  <Td className="text-right text-xs tabular-nums">
                    {x.billing === "none" ? <span className="text-muted">{t("costs.billing.none")}</span> : x.unit_price == null ? <Badge tone="warning">{t("costs.no_price")}</Badge> : (
                      <><div className="font-semibold">{x.quantity !== 1 ? `${num(x.quantity)} × ` : ""}{money(x.unit_price)}</div><div className="text-muted">{t(`costs.billing.${x.billing}`)}</div></>
                    )}
                  </Td>
                  <Td className="text-xs">
                    {x.end_date ? (
                      <>
                        <div className={cn("font-semibold", (x.days_left ?? 0) < 0 && "text-offline")}>{day(x.end_date, i18n.language)}{x.auto_renew && <RefreshCw className="ml-1 inline size-3 text-muted" />}</div>
                        <div className="text-muted">{(x.days_left ?? 0) < 0 ? t("costs.ended_ago", { count: -(x.days_left ?? 0) }) : t("costs.days_left", { count: x.days_left ?? 0 })}</div>
                        {x.notice_by && (x.notice_days_left ?? -1) >= 0 && <div className={cn(x.status === "notice" ? "font-semibold text-warning" : "text-muted")}>{t("costs.notice_by", { date: day(x.notice_by, i18n.language) })}</div>}
                      </>
                    ) : <span className="text-muted">–</span>}
                  </Td>
                  <Td><Badge tone={STATUS_TONE[x.status]} dot>{t(`costs.status.${x.status}`)}</Badge></Td>
                  {admin && <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => setEdit(x)}><Pencil /></Button></Td>}
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {admin && edit && <ItemDialog item={edit === "new" ? null : edit} onClose={() => setEdit(null)} />}
    </div>
  );
}

function ItemDialog({ item, onClose }: { item: Item | null; onClose: () => void }) {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const invalidate = useInvalidate();
  const s = (v: number | null | undefined) => (v == null ? "" : String(v));
  const [f, setF] = useState({
    customer_id: item?.customer_id ?? customerId ?? "", kind: item?.kind ?? ("licence" as Kind), name: item?.name ?? "", vendor: item?.vendor ?? "",
    reference: item?.reference ?? "", serial: item?.serial ?? "", node_id: item?.node_id ?? "", quantity: s(item?.quantity ?? 1),
    unit_price: s(item?.unit_price), cost_price: s(item?.cost_price), billing: item?.billing ?? ("monthly" as Billing),
    start_date: item?.start_date ?? "", end_date: item?.end_date ?? "", auto_renew: item?.auto_renew ?? false, renew_months: s(item?.renew_months ?? 12),
    notice_days: s(item?.notice_days), warn_days: s(item?.warn_days), active: item?.active ?? true, notes: item?.notes ?? "",
  });
  const machines = useQuery({
    queryKey: ["costs", "machines", f.customer_id],
    queryFn: () => get<{ id: string; name: string }[]>(`api/uptime/probes?customer_id=${f.customer_id}`),
    enabled: !!f.customer_id,
  });
  const n = (v: string) => (v.trim() === "" ? null : Number(v.replace(",", ".")));
  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...f, vendor: f.vendor || null, reference: f.reference || null, serial: f.serial || null, node_id: f.node_id || null,
        quantity: n(f.quantity) ?? 1, unit_price: n(f.unit_price), cost_price: n(f.cost_price), start_date: f.start_date || null,
        end_date: f.end_date || null, renew_months: f.auto_renew ? n(f.renew_months) ?? 12 : null, notice_days: n(f.notice_days),
        warn_days: n(f.warn_days), notes: f.notes || null,
      };
      return item ? put(`api/costs/items/${item.id}`, body) : post("api/costs/items", body);
    },
    onSuccess: () => { toast.success(t("common.saved")); invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/costs/items/${item!.id}`),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const endLabel = f.kind === "hardware" ? t("costs.f_warranty_end") : f.kind === "licence" ? t("costs.f_licence_end") : t("costs.f_end");
  return (
    <Dialog open wide onOpenChange={(v) => !v && onClose()} title={item ? t("costs.edit_item") : t("costs.add_item")} description={t("costs.item_dialog_desc")}
      footer={<>
        {item && <Button variant="ghost" className="mr-auto text-offline" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 /> {t("costs.delete")}</Button>}
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!f.customer_id || !f.name.trim()} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="grid gap-4 sm:grid-cols-6">
        {!item && !customerId && (
          <Field label={t("costs.col_customer")} className="sm:col-span-6">
            <Select value={f.customer_id} onChange={set("customer_id")}>
              <option value="">–</option>
              {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
        )}
        <Field label={t("costs.f_kind")} className="sm:col-span-2">
          <Select value={f.kind} onChange={set("kind")}>{KINDS.map((k) => <option key={k} value={k}>{t(`costs.kind.${k}`)}</option>)}</Select>
        </Field>
        <Field label={t("costs.f_name")} className="sm:col-span-4"><Input value={f.name} onChange={set("name")} maxLength={200} placeholder={t(`costs.ph.${f.kind}`)} /></Field>
        <Field label={t("costs.f_vendor")} className="sm:col-span-2"><Input value={f.vendor} onChange={set("vendor")} maxLength={200} /></Field>
        <Field label={t("costs.f_reference")} hint={t("costs.f_reference_hint")} className="sm:col-span-2"><Input value={f.reference} onChange={set("reference")} maxLength={200} /></Field>
        <Field label={t("costs.f_serial")} className="sm:col-span-2"><Input value={f.serial} onChange={set("serial")} maxLength={120} /></Field>
        <Field label={t("costs.f_machine")} className="sm:col-span-6">
          <Select value={f.node_id} onChange={set("node_id")} disabled={!f.customer_id}>
            <option value="">{t("costs.f_no_machine")}</option>
            {(machines.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </Select>
        </Field>
        <Field label={t("costs.f_billing")} className="sm:col-span-2">
          <Select value={f.billing} onChange={set("billing")}>{BILLINGS.map((b) => <option key={b} value={b}>{t(`costs.billing.${b}`)}</option>)}</Select>
        </Field>
        <Field label={t("costs.f_quantity")} className="sm:col-span-1"><Input inputMode="decimal" value={f.quantity} onChange={set("quantity")} /></Field>
        <Field label={t("costs.f_price")} hint={t("costs.f_price_hint")} className="sm:col-span-2"><Input inputMode="decimal" value={f.unit_price} onChange={set("unit_price")} disabled={f.billing === "none"} /></Field>
        <Field label={t("costs.f_cost")} className="sm:col-span-1"><Input inputMode="decimal" value={f.cost_price} onChange={set("cost_price")} /></Field>
        <Field label={t("costs.f_start")} hint={f.billing === "once" ? t("costs.f_start_once") : f.billing === "yearly" || f.billing === "quarterly" ? t("costs.f_start_cycle") : undefined} className="sm:col-span-3"><Input type="date" value={f.start_date} onChange={set("start_date")} /></Field>
        <Field label={endLabel} className="sm:col-span-3"><Input type="date" value={f.end_date} onChange={set("end_date")} /></Field>
        <Field label={t("costs.f_notice")} hint={t("costs.f_notice_hint")} className="sm:col-span-2"><Input type="number" min={0} max={730} value={f.notice_days} onChange={set("notice_days")} /></Field>
        <Field label={t("costs.f_warn")} hint={t("costs.f_warn_hint")} className="sm:col-span-2"><Input type="number" min={0} max={730} value={f.warn_days} onChange={set("warn_days")} /></Field>
        <Field label={t("costs.f_renew_months")} className="sm:col-span-2"><Input type="number" min={1} max={120} value={f.renew_months} onChange={set("renew_months")} disabled={!f.auto_renew} /></Field>
        <div className="space-y-3 sm:col-span-6">
          <Switch checked={f.auto_renew} onCheckedChange={(v) => setF({ ...f, auto_renew: v })} label={t("costs.f_auto_renew")} description={t("costs.f_auto_renew_hint")} />
          <Switch checked={f.active} onCheckedChange={(v) => setF({ ...f, active: v })} label={t("costs.f_active")} description={t("costs.f_active_hint")} />
        </div>
        <Field label={t("costs.f_notes")} hint={t("costs.f_notes_hint")} className="sm:col-span-6"><Textarea rows={2} value={f.notes} onChange={set("notes")} maxLength={4000} /></Field>
        {item && item.history.length > 0 && (
          <div className="text-xs text-muted sm:col-span-6">
            <div className="font-semibold">{t("costs.history")}</div>
            {item.history.slice(-5).reverse().map((h) => <div key={h.at}>{t("costs.history_line", { from: h.from, to: h.to, by: h.by })}</div>)}
          </div>
        )}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ price list */
function RatesTab({ admin }: { admin: boolean }) {
  const { t } = useTranslation();
  const money = useMoney();
  const { customerId } = useCustomer();
  const customers = useCustomers();
  const [edit, setEdit] = useState<{ rateKey: string; customer_id: string | null; rate: Rate | null; name?: string } | null>(null);
  const q = useQuery({ queryKey: ["costs", "rates", customerId], queryFn: () => get<Rates>(`api/costs/rates${customerId ? `?customer_id=${customerId}` : ""}`) });
  if (q.isLoading) return <Spinner />;
  if (q.isError) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const d = q.data!;
  const def = new Map(d.defaults.map((r) => [r.key, r]));
  const own = new Map(d.customer.map((r) => [r.key, r]));
  const keys = [...new Set([...STANDARD_KEYS, ...d.defaults.map((r) => r.key), ...d.customer.map((r) => r.key), ...d.unpriced_skus.map((x) => x.key)])];
  const skuNames = new Map(Object.entries(d.sku_names ?? {}));
  const cname = customers.data?.find((c) => c.id === customerId)?.name;
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Tags />} title={t("costs.rates_title")} description={customerId ? t("costs.rates_desc_customer", { name: cname ?? "" }) : t("costs.rates_desc")} />
        <Table>
          <thead><Tr><Th>{t("costs.col_rate")}</Th><Th className="text-right">{t("costs.col_default")}</Th>{customerId && <Th className="text-right">{t("costs.col_own", { name: cname ?? "" })}</Th>}<Th className="text-right">{t("costs.col_purchase")}</Th></Tr></thead>
          <tbody>
            {keys.map((k) => {
              const r = def.get(k);
              const o = own.get(k);
              const unit = (o ?? r)?.unit ?? (k.startsWith("m365.sku.") ? "licence" : null);
              return (
                <Tr key={k}>
                  <Td>
                    <div className="font-medium">{rateLabel(k, t, (o ?? r)?.name || skuNames.get(k))}</div>
                    <div className="font-mono text-[11px] text-muted">{k}{unit ? ` · ${t("costs.per_unit", { unit: t(`costs.unit1.${unit}`, { defaultValue: unit }) })}` : ""}</div>
                  </Td>
                  <Td className="text-right tabular-nums">
                    <PriceCell value={r ? money(r.price) : null} admin={admin} onEdit={() => setEdit({ rateKey: k, customer_id: null, rate: r ?? null, name: skuNames.get(k) })} />
                  </Td>
                  {customerId && (
                    <Td className="text-right tabular-nums">
                      <PriceCell value={o ? money(o.price) : null} admin={admin} empty={t("costs.uses_default")} onEdit={() => setEdit({ rateKey: k, customer_id: customerId, rate: o ?? null, name: skuNames.get(k) })} />
                    </Td>
                  )}
                  <Td className="text-right text-xs tabular-nums text-muted">{(o ?? r)?.cost != null ? money((o ?? r)!.cost) : "–"}</Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
        <CardBody><p className="text-xs text-muted">{t("costs.rates_hint")}</p></CardBody>
      </Card>
      {admin && edit && <RateDialog {...edit} onClose={() => setEdit(null)} />}
    </div>
  );
}

function PriceCell({ value, admin, onEdit, empty }: { value: string | null; admin: boolean; onEdit: () => void; empty?: string }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1">
      {value ?? <span className="text-xs text-muted">{empty ?? t("costs.not_billed")}</span>}
      {admin && <Button size="sm" variant="ghost" onClick={onEdit} title={t("costs.edit_price")}><Pencil /></Button>}
    </span>
  );
}

function RateDialog({ rateKey: rkey, customer_id, rate, name, onClose }: { rateKey: string; customer_id: string | null; rate: Rate | null; name?: string; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const [f, setF] = useState({ price: rate ? String(rate.price) : "", cost: rate?.cost != null ? String(rate.cost) : "", name: rate?.name ?? "" });
  const n = (v: string) => (v.trim() === "" ? null : Number(v.replace(",", ".")));
  const save = useMutation({
    mutationFn: () => put("api/costs/rates", { key: rkey, customer_id, price: n(f.price), cost: n(f.cost), name: f.name || null }),
    onSuccess: () => { toast.success(t("common.saved")); invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/costs/rates/${rate!.id}`),
    onSuccess: () => { invalidate(); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={rateLabel(rkey, t, rate?.name || name)} description={customer_id ? t("costs.rate_customer_desc") : t("costs.rate_default_desc")}
      footer={<>
        {rate && <Button variant="ghost" className="mr-auto text-offline" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 /> {customer_id ? t("costs.use_default") : t("costs.stop_billing")}</Button>}
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" loading={save.isPending} disabled={n(f.price) == null || Number.isNaN(n(f.price))} onClick={() => save.mutate()}>{t("common.save")}</Button>
      </>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("costs.f_price_month")}><Input inputMode="decimal" value={f.price} onChange={(e) => setF({ ...f, price: e.target.value })} autoFocus /></Field>
        <Field label={t("costs.f_cost")} hint={t("costs.f_cost_hint")}><Input inputMode="decimal" value={f.cost} onChange={(e) => setF({ ...f, cost: e.target.value })} /></Field>
        <Field label={t("costs.f_rate_name")} hint={t("costs.f_rate_name_hint")} className="sm:col-span-2"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxLength={200} /></Field>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ settings */
function SettingsTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["costs", "settings"], queryFn: () => get<Settings>("api/costs/settings") });
  const [f, setF] = useState<Settings | null>(null);
  useEffect(() => { if (q.data && !f) setF(q.data); }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => put<Settings>("api/costs/settings", f),
    onSuccess: (r) => { toast.success(t("common.saved")); setF(r); qc.invalidateQueries({ queryKey: ["costs"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const sample = useMutation({
    mutationFn: () => post<{ customers: number }>("api/costs/sample"),
    onSuccess: (r) => { toast.success(t("costs.sampled", { count: r.customers })); qc.invalidateQueries({ queryKey: ["costs"] }); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading || !f) return <Spinner />;
  const sw = (k: keyof Settings, label: string, desc?: string) => <Switch checked={!!f[k]} onCheckedChange={(v) => setF({ ...f, [k]: v })} label={label} description={desc} />;
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader icon={<Receipt />} title={t("costs.set_invoice")} />
        <CardBody className="grid gap-4 sm:grid-cols-3">
          <Field label={t("costs.set_currency")} hint={t("costs.set_currency_hint")}><Input value={f.currency} maxLength={3} onChange={(e) => setF({ ...f, currency: e.target.value.toUpperCase() })} /></Field>
          <Field label={t("costs.set_vat")}><Input type="number" min={0} max={100} step="0.5" value={f.vat_pct} onChange={(e) => setF({ ...f, vat_pct: Number(e.target.value) })} /></Field>
          <Field label={t("costs.set_m365")}>
            <Select value={f.m365_quantity} onChange={(e) => setF({ ...f, m365_quantity: e.target.value as Settings["m365_quantity"] })}>
              <option value="enabled">{t("costs.set_m365_enabled")}</option>
              <option value="consumed">{t("costs.set_m365_consumed")}</option>
            </Select>
          </Field>
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<Cpu />} title={t("costs.set_usage")} description={t("costs.set_usage_desc")}
          action={<Button size="sm" variant="ghost" loading={sample.isPending} onClick={() => sample.mutate()}><RefreshCw /> {t("costs.sample_now")}</Button>} />
        <CardBody className="space-y-4">
          <Field label={t("costs.set_basis")}>
            <Select className="w-auto" value={f.usage_basis} onChange={(e) => setF({ ...f, usage_basis: e.target.value as Settings["usage_basis"] })}>
              <option value="average">{t("costs.basis.average")}</option>
              <option value="peak">{t("costs.basis.peak")}</option>
            </Select>
          </Field>
          {sw("include_stopped", t("costs.set_stopped"), t("costs.set_stopped_hint"))}
        </CardBody>
      </Card>
      <Card>
        <CardHeader icon={<AlarmClock />} title={t("costs.set_warnings")} />
        <CardBody className="space-y-4">
          <Field label={t("costs.set_warn_days")} hint={t("costs.set_warn_days_hint")}><Input type="number" className="w-32" min={1} max={365} value={f.warn_days} onChange={(e) => setF({ ...f, warn_days: Number(e.target.value) })} /></Field>
          {sw("alert_expiring", t("costs.set_alert"), t("costs.set_alert_hint"))}
          {sw("auto_renew", t("costs.set_auto_renew"), t("costs.set_auto_renew_hint"))}
        </CardBody>
      </Card>
      <div className="flex justify-end"><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}><FileText /> {t("common.save")}</Button></div>
    </div>
  );
}
