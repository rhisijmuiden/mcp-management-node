import * as DM from "@radix-ui/react-dropdown-menu";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Check, ChevronsUpDown, Globe2, Home, Plus, Settings2 } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Segmented, Select, Textarea } from "@/components/ui/form";
import { Alert } from "@/components/ui/misc";
import { errorMessage, getAll, post, put, setCurrentCustomer } from "@/lib/api";
import type { Customer, CustomerSummary } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ENV_COLORS, EnvDot } from "./proxmox";

const KEY = "mmn.customer";

function readStored(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

// Set before the first query runs, so a reload keeps the chosen customer.
setCurrentCustomer(readStored());

interface Ctx {
  customerId: string;
  setCustomer: (id: string) => void;
}

const CustomerContext = createContext<Ctx>({ customerId: "", setCustomer: () => {} });

export function CustomerProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [customerId, setId] = useState(readStored);
  const setCustomer = useCallback(
    (id: string) => {
      setCurrentCustomer(id);
      setId(id);
      try {
        if (id) localStorage.setItem(KEY, id);
        else localStorage.removeItem(KEY);
      } catch {
        /* private mode */
      }
      // Every page reloads its data for the new customer.
      qc.invalidateQueries();
    },
    [qc],
  );
  return <CustomerContext.Provider value={{ customerId, setCustomer }}>{children}</CustomerContext.Provider>;
}

export const useCustomer = () => useContext(CustomerContext);

export const useCustomers = () =>
  useQuery({ queryKey: ["customers"], queryFn: () => getAll<CustomerSummary[]>("api/customers"), refetchInterval: 60_000 });

/** CSS colour for a customer colour name (the dot classes live in ENV_COLORS). */
export const CUSTOMER_CSS: Record<string, string> = {
  lucht: "var(--lucht)",
  koraal: "var(--koraal)",
  online: "var(--status-online)",
  warning: "var(--status-warning)",
  nacht: "var(--ink)",
  info: "var(--lucht)",
};

/** One line under a customer in the switcher: what is linked and what needs attention. */
function customerLine(c: CustomerSummary, t: (k: string, o?: Record<string, unknown>) => string): { text: string; tone: "muted" | "warning" | "offline" } {
  const offline = c.machines - c.online;
  if (offline > 0) return { text: t("top.cust_offline", { count: offline }), tone: "offline" };
  if (c.alerts > 0) return { text: t("top.cust_alerts", { count: c.alerts }), tone: "warning" };
  if (c.unifi.errors > 0) return { text: t("top.cust_unifi_error"), tone: "warning" };
  if (!c.machines && !c.unifi.connections) return { text: t("top.cust_empty"), tone: "muted" };
  return { text: t("top.cust_ok"), tone: "muted" };
}

/** Customer switcher in the top bar: limits the whole app to one customer. */
export function CustomerSwitcher({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();
  const { customerId, setCustomer } = useCustomer();
  const list = useCustomers();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const current = list.data?.find((c) => c.id === customerId);
  useEffect(() => {
    // The chosen customer was deleted: back to all customers.
    if (customerId && list.data && !current) setCustomer("");
  }, [customerId, list.data, current, setCustomer]);
  const shown = (list.data ?? []).filter((c) => !q || c.name.toLowerCase().includes(q.toLowerCase()));
  const item = "flex cursor-pointer items-center gap-3 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken";
  const chip = "rounded bg-sunken px-1.5 py-px text-[10.5px] font-bold text-muted";
  const pick = (id: string) => {
    setCustomer(id);
    setQ("");
    if (id) toast.success(t("cust.switched", { name: list.data?.find((c) => c.id === id)?.name ?? "" }));
  };
  return (
    <DM.Root onOpenChange={(o) => { if (!o) setQ(""); }}>
      <DM.Trigger
        className="flex min-w-0 cursor-pointer items-center gap-2.5 rounded-md border border-white/20 bg-white/[0.07] px-3 py-1.5 text-left text-white transition-colors hover:bg-white/[0.14] focus:outline-none focus-visible:ring-2 focus-visible:ring-lucht sm:min-w-[240px]"
        aria-label={t("cust.switch")}
      >
        {current ? <span className="size-3 shrink-0 rounded-full" style={{ background: CUSTOMER_CSS[current.color] ?? "var(--lucht)" }} /> : <Globe2 className="size-4 shrink-0 text-white/60" />}
        <span className="min-w-0 flex-1 leading-tight">
          <span className="hidden text-[10px] font-bold uppercase tracking-[0.08em] text-white/60 sm:block">{t("cust.customer")}</span>
          <span className="block truncate text-[13.5px] font-semibold">{current ? current.name : t("cust.all")}</span>
        </span>
        <ChevronsUpDown className="size-4 shrink-0 text-white/60" />
      </DM.Trigger>
      <DM.Portal>
        <DM.Content align="start" sideOffset={8} className="z-50 max-h-[75vh] w-[min(440px,calc(100vw-24px))] overflow-y-auto rounded-md border border-line bg-raised p-1.5 shadow-xl">
          {(list.data?.length ?? 0) > 5 && (
            <div className="p-1 pb-1.5">
              <Input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.stopPropagation()} placeholder={t("top.cust_search")} aria-label={t("top.cust_search")} className="h-9" />
            </div>
          )}
          <DM.Item onSelect={() => pick("")} className={item}>
            <Globe2 className="size-4 text-muted" />
            <span className="min-w-0 flex-1"><span className="block font-semibold">{t("cust.all")}</span><span className="block text-xs text-muted">{t("top.cust_all_hint")}</span></span>
            {!customerId && <Check className="size-4 text-koraal" />}
          </DM.Item>
          <DM.Separator className="my-1 h-px bg-line" />
          {shown.map((c) => {
            const line = customerLine(c, t);
            return (
              <DM.Item key={c.id} onSelect={() => pick(c.id)} className={cn(item, customerId === c.id && "bg-sunken")}>
                <span className="size-2.5 shrink-0 rounded-full" style={{ background: CUSTOMER_CSS[c.color] ?? "var(--lucht)" }} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-semibold">{c.name}</span>
                  <span className={cn("block truncate text-xs", line.tone === "offline" ? "text-offline" : line.tone === "warning" ? "text-warning" : "text-muted")}>{line.text}</span>
                </span>
                <span className="hidden shrink-0 gap-1 sm:flex">
                  {c.machines > 0 && <span className={chip}>{t("top.chip_machines", { count: c.machines })}</span>}
                  {c.unifi.connections > 0 && <span className={chip}>UniFi</span>}
                </span>
                {customerId === c.id && <Check className="size-4 shrink-0 text-koraal" />}
              </DM.Item>
            );
          })}
          <DM.Separator className="my-1 h-px bg-line" />
          {current && (
            <DM.Item onSelect={() => { navigate(`/customers/${current.id}`); onNavigate?.(); }} className={item}>
              <Building2 className="size-4" /> {t("cust.open_page", { name: current.name })}
            </DM.Item>
          )}
          <DM.Item onSelect={() => { navigate("/customers"); onNavigate?.(); }} className={item}>
            <Settings2 className="size-4" /> {t("cust.manage")}
          </DM.Item>
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );
}

/** "All customers" or a selection, for tokens and AI apps. */
export function CustomerScopePicker({ value, onChange }: { value: string[] | null; onChange: (v: string[] | null) => void }) {
  const { t } = useTranslation();
  const list = useCustomers();
  return (
    <div className="space-y-3">
      <Segmented
        value={value === null ? "all" : "some"}
        onChange={(v) => onChange(v === "all" ? null : value ?? [])}
        options={[{ value: "all", label: t("cust.scope_all") }, { value: "some", label: t("cust.scope_some") }]}
      />
      {value !== null && (
        <div className="max-h-56 space-y-2 overflow-y-auto rounded-md border border-line p-3">
          {list.data?.map((c) => (
            <Checkbox key={c.id} checked={value.includes(c.id)}
              onCheckedChange={(v) => onChange(v ? [...value, c.id] : value.filter((x) => x !== c.id))}
              label={<span className="inline-flex items-center gap-2"><EnvDot color={c.color} /> {c.name}</span>} />
          ))}
          {value.length === 0 && <p className="text-xs text-offline">{t("cust.scope_pick_one")}</p>}
        </div>
      )}
      <p className="text-xs text-muted">{value === null ? t("cust.scope_all_hint") : t("cust.scope_some_hint")}</p>
    </div>
  );
}

export function customerNames(ids: string[] | null | undefined, list?: CustomerSummary[]): string {
  if (!ids) return "";
  const names = ids.map((id) => list?.find((c) => c.id === id)?.name ?? "?");
  return names.join(", ");
}

/* ------------------------------------------------------------------ create / edit */
interface Form {
  name: string; kind: string; description: string; color: string;
  contact_name: string; contact_email: string; contact_phone: string; maintenance_window: string; sla: string;
}

const empty: Form = { name: "", kind: "customer", description: "", color: "koraal", contact_name: "", contact_email: "", contact_phone: "", maintenance_window: "", sla: "" };

export function CustomerDialog({ open, onOpenChange, customer, onSaved }: {
  open: boolean; onOpenChange: (v: boolean) => void; customer?: Customer | null; onSaved?: (c: Customer) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [f, setF] = useState<Form>(empty);
  useEffect(() => {
    if (!open) return;
    setF(customer ? {
      name: customer.name, kind: customer.kind, description: customer.description ?? "", color: customer.color,
      contact_name: customer.contact_name ?? "", contact_email: customer.contact_email ?? "", contact_phone: customer.contact_phone ?? "",
      maintenance_window: customer.maintenance_window ?? "", sla: customer.sla ?? "",
    } : empty);
  }, [open, customer]);
  const save = useMutation({
    mutationFn: () => (customer ? put<Customer>(`api/customers/${customer.id}`, f) : post<Customer>("api/customers", f)),
    onSuccess: (c) => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      qc.invalidateQueries({ queryKey: ["environments"] });
      qc.invalidateQueries({ queryKey: ["customer"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
      toast.success(t("common.saved"));
      onOpenChange(false);
      onSaved?.(c);
    },
  });
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Dialog open={open} onOpenChange={onOpenChange} wide title={customer ? t("cust.edit") : t("cust.add")} description={customer ? undefined : t("cust.add_desc")}
      footer={<><Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} disabled={!f.name.trim()} onClick={() => save.mutate()}>{t("common.save")}</Button></>}>
      <div className="space-y-4">
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("cust.name")}><Input value={f.name} onChange={set("name")} placeholder={t("cust.name_ph")} autoFocus /></Field>
          <Field label={t("cust.kind")}>
            <Select value={f.kind} onChange={set("kind")}>
              <option value="customer">{t("px.kind_customer")}</option>
              <option value="home">{t("px.kind_home")}</option>
            </Select>
          </Field>
        </div>
        <Field label={t("px.env_color")}>
          <div className="flex gap-2">
            {Object.keys(ENV_COLORS).map((c) => (
              <button key={c} type="button" onClick={() => setF({ ...f, color: c })} aria-label={c}
                className={cn("grid size-9 cursor-pointer place-items-center rounded-full border-2", f.color === c ? "border-ink" : "border-transparent")}>
                <EnvDot color={c} className="size-5" />
              </button>
            ))}
          </div>
        </Field>
        <Field label={t("px.env_description")}><Textarea className="min-h-16 font-sans text-sm" value={f.description} onChange={set("description")} /></Field>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label={t("cust.contact_name")}><Input value={f.contact_name} onChange={set("contact_name")} /></Field>
          <Field label={t("cust.contact_email")}><Input type="email" value={f.contact_email} onChange={set("contact_email")} /></Field>
          <Field label={t("cust.contact_phone")}><Input value={f.contact_phone} onChange={set("contact_phone")} /></Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("cust.maintenance")} hint={t("cust.maintenance_hint")}><Input value={f.maintenance_window} onChange={set("maintenance_window")} placeholder={t("cust.maintenance_ph")} /></Field>
          <Field label={t("cust.sla")} hint={t("cust.sla_hint")}><Input value={f.sla} onChange={set("sla")} placeholder={t("cust.sla_ph")} /></Field>
        </div>
      </div>
    </Dialog>
  );
}

export function CustomerKindIcon({ kind, className }: { kind: string; className?: string }) {
  return kind === "home" ? <Home className={className} /> : <Building2 className={className} />;
}

export function useCustomerName() {
  const list = useCustomers();
  return useMemo(() => (id?: string | null) => list.data?.find((c) => c.id === id)?.name ?? "", [list.data]);
}

export function NewCustomerButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}><Plus /> {t("cust.add")}</Button>
      <CustomerDialog open={open} onOpenChange={setOpen} onSaved={(c) => navigate(`/customers/${c.id}`)} />
    </>
  );
}
