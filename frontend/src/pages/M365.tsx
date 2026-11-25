import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, BadgeCheck, CheckCircle2, CloudCog, Download, Fingerprint, Globe2, KeyRound, LogOut, Pencil, Plus,
  RefreshCw, RotateCcw, Search, ShieldAlert, ShieldCheck, ShieldOff, Trash2, UserCheck, UserX, Users,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { useCustomer, useCustomers } from "@/components/customers";
import { tabCls } from "@/components/net";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { apiUrl, del, errorMessage, get, patch, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatDate, timeAgo } from "@/lib/utils";

/* ------------------------------------------------------------------ types */
type Status = "unknown" | "ok" | "partial" | "error";
type Action = "block" | "unblock" | "revoke_sessions" | "reset_mfa" | "reset_password" | "confirm_compromised" | "dismiss_risk";
interface Feature { ok: boolean; reason?: "no_permission" | "needs_p1" | "needs_p2" | "not_found" | "error"; message?: string; permission?: string; source?: string }
interface Perm { permission: string; for: string; granted: boolean }
interface Settings {
  allowed_countries: string[]; stale_days: number; failed_threshold: number; secret_warn_days: number;
  alert_risky: boolean; alert_admin_mfa: boolean; alert_signins: boolean; alert_licences: boolean; alert_secret: boolean;
}
interface Tenant {
  id: string; customer_id: string; customer_name: string | null; name: string; tenant_id: string; client_id: string;
  auth_method: "secret" | "certificate"; vault_item_id: string | null; vault_item_name: string | null; enabled: boolean;
  status: Status; last_sync_at: string | null; last_error: string | null; settings: Settings;
  organization: { name?: string; domains?: string[]; default_domain?: string | null };
  counts: Partial<Record<"users" | "members" | "guests" | "enabled" | "disabled" | "licensed" | "admins" | "mfa_known" | "mfa_registered" | "no_mfa" | "admins_no_mfa" | "risky" | "high_risk" | "licences_paid" | "licences_used", number>>;
  features: Record<string, Feature>; security: { security_defaults?: boolean | null; ca_policies?: number | null; ca_enabled?: number | null };
  mfa_source: "registration" | "methods" | null; app_credential: { expires?: string; name?: string | null; app?: string } | null;
  credential_days: number | null; read: Perm[]; write: Perm[]; actions: Record<Action, boolean>;
}
interface M365User {
  id: string; name: string | null; upn: string | null; mail: string | null; enabled: boolean; type: "member" | "guest";
  licences: string[]; created: string | null; last_sign_in: string | null; last_activity: string | null; synced: boolean;
  roles: string[]; admin: boolean; stale?: boolean; job: string | null; department: string | null;
  mfa: { registered: boolean; methods: string[]; default: string | null } | null;
  risk: { level: string; state: string; detail?: string; updated?: string } | null;
  tenant_id: string; tenant: string; customer_id: string;
}
interface SignIn {
  id: string; tenant_id: string; at: string; upn: string | null; name: string | null; app: string | null; client_app: string | null;
  ip: string | null; country: string | null; city: string | null; success: boolean; error_code: number | null; failure_reason: string | null;
  risk_level: string | null; risk_state: string | null; flags: string[];
}
interface Detection { id: string; user_id: string; upn: string; type: string; level: string; state: string; at: string; ip: string | null; location: string | null; tenant?: string }
interface Licence { sku_id: string; sku: string; name: string; enabled: number; consumed: number; available: number; status: string; free: boolean; tenant: string; customer_name: string | null }
interface Overview { tenants: Tenant[]; totals: Tenant["counts"]; failed_24h: number; foreign_24h: number; risk_detections: Detection[] }
interface UserDetail extends M365User { signins: SignIn[]; risk_detections: Detection[]; actions: Record<Action, boolean> }
interface VaultItem { id: string; name: string; kind: string; customer_id: string }

const STATUS_TONE: Record<Status, "online" | "warning" | "offline" | "neutral"> = { ok: "online", partial: "warning", error: "offline", unknown: "neutral" };
const FILTERS = ["all", "admins", "no_mfa", "risky", "stale", "disabled_licensed", "guests", "disabled", "unlicensed"] as const;
type Filter = (typeof FILTERS)[number];
const FLAGS = ["", "risky", "foreign", "failed", "legacy"] as const;
const DANGER: Action[] = ["block", "reset_mfa", "reset_password", "confirm_compromised"];

function qs(path: string, params: Record<string, string | number | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  return q ? `${path}?${q}` : path;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["m365"] });
}

function MfaBadge({ u }: { u: M365User }) {
  const { t } = useTranslation();
  if (!u.mfa) return <Badge tone="neutral">{t("m365.mfa_unknown")}</Badge>;
  return u.mfa.registered ? <Badge tone="online" dot>{t("m365.mfa_on")}</Badge> : <Badge tone={u.admin ? "offline" : "warning"} dot>{t("m365.mfa_off")}</Badge>;
}

function RiskBadge({ level }: { level?: string | null }) {
  const { t } = useTranslation();
  if (!level || level === "none") return null;
  return <Badge tone={level === "high" ? "offline" : level === "medium" ? "warning" : "info"}>{t(`m365.risk.${level}`, { defaultValue: level })}</Badge>;
}

/* ------------------------------------------------------------------ page */
export function M365Page() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { customerId } = useCustomer();
  const [tab, setTab] = useState("tenants");
  const [edit, setEdit] = useState<Tenant | "new" | null>(null);
  const [openUser, setOpenUser] = useState<{ tenant: string; id: string } | null>(null);
  const q = useQuery({
    queryKey: ["m365", "overview", customerId],
    queryFn: () => get<Overview>(qs("api/m365/overview", { customer_id: customerId })),
    refetchInterval: 60_000,
  });
  const tenants = q.data?.tenants ?? [];
  const tot = q.data?.totals ?? {};
  const mfaPct = tot.mfa_known ? Math.round(((tot.mfa_registered ?? 0) / Math.max(1, (tot.mfa_registered ?? 0) + (tot.no_mfa ?? 0))) * 100) : null;

  return (
    <div>
      <PageHeader
        title={<span className="flex min-w-0 items-center gap-2"><CloudCog className="hidden size-6 shrink-0 text-koraal sm:block" /><span className="min-w-0 break-words">{t("m365.title")}</span></span>}
        description={t("m365.desc")}
        actions={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("m365.connect")}</Button>} />
      {q.isLoading ? <Spinner /> : q.isError ? <Alert tone="offline">{errorMessage(q.error, t)}</Alert> : !tenants.length ? (
        <Card><EmptyState icon={<CloudCog />} title={t("m365.empty")} action={admin && <Button variant="primary" onClick={() => setEdit("new")}><Plus /> {t("m365.connect")}</Button>}>{t("m365.empty_hint")}</EmptyState></Card>
      ) : (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <Stat label={t("m365.k_users")} value={tot.enabled ?? 0} hint={t("m365.k_users_hint", { guests: tot.guests ?? 0, admins: tot.admins ?? 0 })} tone="info" icon={<Users />} />
            <Stat label={t("m365.k_mfa")} value={mfaPct == null ? "–" : `${mfaPct}%`} tone={tot.admins_no_mfa ? "offline" : tot.no_mfa ? "warning" : "online"}
              hint={tot.admins_no_mfa ? t("m365.k_mfa_admins", { count: tot.admins_no_mfa }) : t("m365.k_mfa_hint", { count: tot.no_mfa ?? 0 })} icon={<Fingerprint />} />
            <Stat label={t("m365.k_risky")} value={tot.risky ?? 0} tone={tot.high_risk ? "offline" : tot.risky ? "warning" : "neutral"}
              hint={t("m365.k_risky_hint", { foreign: q.data?.foreign_24h ?? 0, failed: q.data?.failed_24h ?? 0 })} icon={<ShieldAlert />} />
            <Stat label={t("m365.k_licences")} value={`${tot.licences_used ?? 0} / ${tot.licences_paid ?? 0}`} tone={(tot.licences_used ?? 0) > (tot.licences_paid ?? 0) ? "offline" : "neutral"} hint={t("m365.k_licences_hint")} icon={<BadgeCheck />} />
          </div>
          <Tabs.Root value={tab} onValueChange={setTab}>
            <Tabs.List className="mb-5 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
              <Tabs.Trigger value="tenants" className={tabCls}>{t("m365.tab_tenants")}</Tabs.Trigger>
              <Tabs.Trigger value="users" className={tabCls}>{t("m365.tab_users")}</Tabs.Trigger>
              <Tabs.Trigger value="risk" className={tabCls}>{t("m365.tab_risk")}</Tabs.Trigger>
              <Tabs.Trigger value="signins" className={tabCls}>{t("m365.tab_signins")}</Tabs.Trigger>
              <Tabs.Trigger value="licences" className={tabCls}>{t("m365.tab_licences")}</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="tenants"><TenantsTab tenants={tenants} admin={admin} onEdit={setEdit} /></Tabs.Content>
            <Tabs.Content value="users"><UsersTab onOpen={setOpenUser} /></Tabs.Content>
            <Tabs.Content value="risk"><RiskTab detections={q.data?.risk_detections ?? []} onOpen={setOpenUser} /></Tabs.Content>
            <Tabs.Content value="signins"><SignInsTab tenants={tenants} /></Tabs.Content>
            <Tabs.Content value="licences"><LicencesTab /></Tabs.Content>
          </Tabs.Root>
        </>
      )}
      {admin && edit && <TenantDialog tenant={edit === "new" ? null : edit} onClose={() => setEdit(null)} />}
      {openUser && <UserDialog tenantId={openUser.tenant} userId={openUser.id} admin={admin} onClose={() => setOpenUser(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ tenants */
function TenantsTab({ tenants, admin, onEdit }: { tenants: Tenant[]; admin: boolean; onEdit: (t: Tenant) => void }) {
  return <div className="grid gap-4 xl:grid-cols-2">{tenants.map((x) => <TenantCard key={x.id} tenant={x} admin={admin} onEdit={() => onEdit(x)} />)}</div>;
}

function TenantCard({ tenant: x, admin, onEdit }: { tenant: Tenant; admin: boolean; onEdit: () => void }) {
  const { t, i18n } = useTranslation();
  const invalidate = useInvalidate();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const sync = useMutation({
    mutationFn: () => post<{ status: Status }>(`api/m365/tenants/${x.id}/sync`),
    onSuccess: (r) => { invalidate(); (r.status === "ok" ? toast.success : toast.warning)(t(`m365.sync_${r.status}`, { defaultValue: t("m365.sync_ok") })); },
    onError: (e) => { invalidate(); toast.error(errorMessage(e, t)); },
  });
  const test = useMutation({
    mutationFn: () => post<{ ok: boolean; organization?: string; error?: string }>(`api/m365/tenants/${x.id}/test`),
    onSuccess: (r) => (r.ok ? toast.success(t("m365.test_ok", { org: r.organization ?? "" })) : toast.error(t("m365.test_failed", { error: r.error ?? "" }))),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const remove = useMutation({
    mutationFn: () => del(`api/m365/tenants/${x.id}`),
    onSuccess: () => { invalidate(); setConfirmDelete(false); toast.success(t("m365.deleted")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const missing = Object.entries(x.features ?? {}).filter(([, f]) => !f.ok);
  const c = x.counts ?? {};
  const days = x.credential_days;
  return (
    <Card>
      <CardHeader icon={<CloudCog />} title={<span className="flex flex-wrap items-center gap-2">{x.name}<Badge tone={STATUS_TONE[x.status]} dot>{t(`m365.status.${x.status}`)}</Badge>{!x.enabled && <Badge tone="neutral">{t("m365.paused")}</Badge>}</span>}
        description={[x.customer_name, x.organization?.default_domain].filter(Boolean).join(" · ")}
        action={<Button size="sm" variant="secondary" loading={sync.isPending} onClick={() => sync.mutate()}><RefreshCw /> {t("m365.sync")}</Button>} />
      <CardBody className="space-y-4">
        {x.status === "error" && x.last_error && <Alert tone="offline" title={t("m365.error_title")}>{x.last_error}</Alert>}
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <Mini label={t("m365.c_users")} value={c.enabled ?? "–"} />
          <Mini label={t("m365.c_admins")} value={c.admins ?? "–"} tone={c.admins_no_mfa ? "offline" : undefined} hint={c.admins_no_mfa ? t("m365.c_admins_no_mfa", { count: c.admins_no_mfa }) : undefined} />
          <Mini label={t("m365.c_no_mfa")} value={c.no_mfa ?? "–"} tone={c.no_mfa ? "warning" : undefined} />
          <Mini label={t("m365.c_risky")} value={c.risky ?? "–"} tone={c.risky ? "offline" : undefined} />
        </div>
        <dl className="grid grid-cols-1 gap-y-2 text-sm">
          <Row k={t("m365.security_defaults")} v={x.security?.security_defaults == null ? "–" : x.security.security_defaults ? <Badge tone="online">{t("common.on", { defaultValue: "On" })}</Badge> : <Badge tone={x.security.ca_enabled ? "neutral" : "warning"}>{t("common.off", { defaultValue: "Off" })}</Badge>} />
          <Row k={t("m365.ca_policies")} v={x.security?.ca_policies == null ? "–" : t("m365.ca_count", { enabled: x.security.ca_enabled ?? 0, total: x.security.ca_policies })} />
          <Row k={t("m365.mfa_source")} v={x.mfa_source ? t(`m365.mfa_src.${x.mfa_source}`) : "–"} />
          <Row k={t("m365.credential")} v={<span className="flex flex-wrap items-center gap-1.5">{t(`m365.auth.${x.auth_method}`)}{days != null && <Badge tone={days < 7 ? "offline" : days <= x.settings.secret_warn_days ? "warning" : "neutral"}>{days < 0 ? t("m365.cred_expired") : t("m365.cred_days", { count: days })}</Badge>}</span>} />
          <Row k={t("m365.vault_item")} v={x.vault_item_name ?? <span className="text-offline">{t("m365.no_vault_item")}</span>} />
          <Row k={t("m365.last_sync")} v={x.last_sync_at ? timeAgo(x.last_sync_at, i18n.language) : t("m365.never")} />
        </dl>
        {missing.length > 0 && (
          <Alert tone="warning" title={t("m365.missing_title")}>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {missing.map(([k, f]) => <li key={k}><span className="font-semibold">{t(`m365.feature.${k}`, { defaultValue: k })}</span>: {t(`m365.reason.${f.reason ?? "error"}`, { permission: f.permission ?? "" })}</li>)}
            </ul>
          </Alert>
        )}
        {admin && (
          <div className="flex flex-wrap gap-2 border-t border-line pt-4">
            <Button size="sm" variant="secondary" loading={test.isPending} onClick={() => test.mutate()}><CheckCircle2 /> {t("m365.test")}</Button>
            <Button size="sm" variant="secondary" onClick={onEdit}><Pencil /> {t("m365.edit")}</Button>
            {x.auth_method === "certificate" && <a href={apiUrl(`api/m365/tenants/${x.id}/certificate`)} className="inline-flex h-8 items-center gap-2 rounded-md border border-line bg-raised px-3 text-[13px] font-semibold hover:bg-sunken"><Download className="size-4" /> {t("m365.download_cert")}</a>}
            <Button size="sm" variant="ghost" className="text-offline" onClick={() => setConfirmDelete(true)}><Trash2 /> {t("m365.delete")}</Button>
          </div>
        )}
      </CardBody>
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete} title={t("m365.delete_title", { name: x.name })} description={t("m365.delete_desc")}
        footer={<><Button onClick={() => setConfirmDelete(false)}>{t("common.cancel")}</Button><Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}><Trash2 /> {t("m365.delete")}</Button></>}>
        <p className="text-sm text-muted">{t("m365.delete_hint")}</p>
      </Dialog>
    </Card>
  );
}

function Mini({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: "offline" | "warning"; hint?: string }) {
  return (
    <div className="rounded-md border border-line bg-sunken/50 px-3 py-2">
      <div className="text-xs text-muted">{label}</div>
      <div className={cn("text-lg font-bold", tone === "offline" && "text-offline", tone === "warning" && "text-warning")}>{value}</div>
      {hint && <div className="text-xs text-offline">{hint}</div>}
    </div>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="flex items-baseline justify-between gap-3 border-b border-line/60 pb-1.5"><dt className="text-muted">{k}</dt><dd className="text-right font-medium">{v}</dd></div>;
}

/* ------------------------------------------------------------------ users */
function UsersTab({ onOpen }: { onOpen: (u: { tenant: string; id: string }) => void }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const q = useQuery({
    queryKey: ["m365", "users", customerId, filter, query],
    queryFn: () => get<{ users: M365User[]; total: number }>(qs("api/m365/users", { customer_id: customerId, filter, q: query.trim() || undefined })),
  });
  const users = q.data?.users ?? [];
  return (
    <Card>
      <CardHeader icon={<Users />} title={t("m365.users_title")} description={t("m365.users_desc")} />
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input className="pl-9" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("m365.search")} />
        </div>
        <Select className="w-auto" value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
          {FILTERS.map((f) => <option key={f} value={f}>{t(`m365.filter.${f}`)}</option>)}
        </Select>
      </div>
      {q.isLoading ? <div className="p-5"><Spinner /></div> : !users.length ? <EmptyState icon={<UserCheck />} title={t("m365.no_users")} /> : (
        <Table>
          <thead><Tr><Th>{t("m365.col_user")}</Th><Th>{t("m365.col_mfa")}</Th><Th>{t("m365.col_licences")}</Th><Th>{t("m365.col_last")}</Th><Th>{t("m365.col_status")}</Th></Tr></thead>
          <tbody>
            {users.map((u) => (
              <Tr key={`${u.tenant_id}:${u.id}`} className="cursor-pointer" onClick={() => onOpen({ tenant: u.tenant_id, id: u.id })}>
                <Td className="max-w-xs">
                  <div className="flex flex-wrap items-center gap-1.5 font-semibold">{u.name ?? u.upn}{u.admin && <Badge tone="accent">{t("m365.admin")}</Badge>}{u.type === "guest" && <Badge tone="neutral">{t("m365.guest")}</Badge>}<RiskBadge level={u.risk?.level} /></div>
                  <div className="truncate text-xs text-muted">{u.upn}{u.department ? ` · ${u.department}` : ""}</div>
                </Td>
                <Td><MfaBadge u={u} /></Td>
                <Td className="text-xs">{u.licences.length ? u.licences.map((l) => <div key={l}>{skuLabel(l)}</div>) : <span className="text-muted">–</span>}</Td>
                <Td className="text-xs">{u.last_activity || u.last_sign_in ? timeAgo(u.last_activity || u.last_sign_in, i18n.language) : "–"}{u.stale && <div><Badge tone="warning">{t("m365.stale")}</Badge></div>}</Td>
                <Td>{u.enabled ? <Badge tone="online" dot>{t("m365.enabled")}</Badge> : <Badge tone="offline" dot>{t("m365.blocked")}</Badge>}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {q.data && q.data.total > users.length && <p className="px-5 py-3 text-xs text-muted">{t("m365.more_users", { count: q.data.total - users.length })}</p>}
    </Card>
  );
}

const SKU: Record<string, string> = {
  SPB: "Business Premium", O365_BUSINESS_PREMIUM: "Business Standard", O365_BUSINESS_ESSENTIALS: "Business Basic",
  O365_BUSINESS: "Apps for business", ENTERPRISEPACK: "Office 365 E3", SPE_E3: "Microsoft 365 E3", SPE_E5: "Microsoft 365 E5",
  STANDARDPACK: "Office 365 E1", EXCHANGESTANDARD: "Exchange Online (Plan 1)", AAD_PREMIUM: "Entra ID P1", AAD_PREMIUM_P2: "Entra ID P2",
};
const skuLabel = (s: string) => SKU[s] ?? s.replace(/_/g, " ");

/* ------------------------------------------------------------------ user detail + actions */
function UserDialog({ tenantId, userId, admin, onClose }: { tenantId: string; userId: string; admin: boolean; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [action, setAction] = useState<Action | null>(null);
  const q = useQuery({ queryKey: ["m365", "user", tenantId, userId], queryFn: () => get<UserDetail>(`api/m365/users/${tenantId}/${encodeURIComponent(userId)}`) });
  const u = q.data;
  const actions: { a: Action; icon: ReactNode; show: boolean }[] = u ? [
    { a: "revoke_sessions", icon: <LogOut />, show: true },
    { a: "block", icon: <UserX />, show: u.enabled },
    { a: "unblock", icon: <UserCheck />, show: !u.enabled },
    { a: "reset_mfa", icon: <ShieldOff />, show: !!u.mfa?.registered || !u.mfa },
    { a: "reset_password", icon: <KeyRound />, show: true },
    { a: "confirm_compromised", icon: <AlertTriangle />, show: !!u.risk && u.risk.state !== "confirmedCompromised" },
    { a: "dismiss_risk", icon: <RotateCcw />, show: !!u.risk },
  ] : [];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={u ? (u.name ?? u.upn) : t("m365.user")} description={u ? `${u.upn} · ${u.tenant}` : undefined}>
      {q.isLoading || !u ? <Spinner /> : (
        <div className="space-y-5">
          <div className="flex flex-wrap gap-1.5">
            {u.enabled ? <Badge tone="online" dot>{t("m365.enabled")}</Badge> : <Badge tone="offline" dot>{t("m365.blocked")}</Badge>}
            <MfaBadge u={u} />{u.admin && <Badge tone="accent">{t("m365.admin")}</Badge>}<RiskBadge level={u.risk?.level} />
            {u.synced && <Badge tone="neutral">{t("m365.synced")}</Badge>}{u.type === "guest" && <Badge tone="neutral">{t("m365.guest")}</Badge>}
          </div>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Row k={t("m365.roles")} v={u.roles.length ? u.roles.join(", ") : "–"} />
            <Row k={t("m365.col_licences")} v={u.licences.length ? u.licences.map(skuLabel).join(", ") : "–"} />
            <Row k={t("m365.mfa_methods")} v={u.mfa?.methods?.length ? u.mfa.methods.join(", ") : "–"} />
            <Row k={t("m365.last_sign_in")} v={u.last_sign_in ? formatDate(u.last_sign_in, i18n.language) : "–"} />
            <Row k={t("m365.department")} v={[u.job, u.department].filter(Boolean).join(" · ") || "–"} />
            <Row k={t("m365.created")} v={u.created ? formatDate(u.created, i18n.language) : "–"} />
          </dl>
          {u.risk && <Alert tone={u.risk.level === "high" ? "offline" : "warning"} title={t("m365.risk_title", { level: t(`m365.risk.${u.risk.level}`, { defaultValue: u.risk.level }) })}>{t(`m365.risk_state.${u.risk.state}`, { defaultValue: u.risk.state })}{u.risk.detail && u.risk.detail !== "none" ? ` · ${u.risk.detail}` : ""}</Alert>}
          {admin && (
            <div>
              <h3 className="mb-2 text-sm font-bold">{t("m365.actions")}</h3>
              <div className="flex flex-wrap gap-2">
                {actions.filter((x) => x.show).map((x) => (
                  <Button key={x.a} size="sm" variant={DANGER.includes(x.a) ? "danger" : "secondary"} disabled={!u.actions[x.a]}
                    title={!u.actions[x.a] ? t("m365.no_permission_hint") : undefined} onClick={() => setAction(x.a)}>{x.icon} {t(`m365.action.${x.a}`)}</Button>
                ))}
              </div>
              {Object.values(u.actions).some((v) => !v) && <p className="mt-2 text-xs text-muted">{t("m365.actions_hint")}</p>}
            </div>
          )}
          <div>
            <h3 className="mb-2 text-sm font-bold">{t("m365.signins_title")}</h3>
            {u.signins.length ? <SignInTable rows={u.signins} compact /> : <p className="text-sm text-muted">{t("m365.no_signins_user")}</p>}
          </div>
          {u.risk_detections.length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-bold">{t("m365.detections")}</h3>
              <ul className="space-y-1 text-sm">{u.risk_detections.map((d) => <li key={d.id} className="flex flex-wrap items-center gap-2"><RiskBadge level={d.level} /><span>{t(`m365.det.${d.type}`, { defaultValue: d.type })}</span><span className="text-muted">{formatDate(d.at, i18n.language)}{d.location ? ` · ${d.location}` : ""}{d.ip ? ` · ${d.ip}` : ""}</span></li>)}</ul>
            </div>
          )}
        </div>
      )}
      {action && u && <ActionDialog tenantId={tenantId} user={u} action={action} onClose={() => setAction(null)} />}
    </Dialog>
  );
}

function ActionDialog({ tenantId, user, action, onClose }: { tenantId: string; user: M365User; action: Action; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const [reason, setReason] = useState("");
  const run = useMutation({
    mutationFn: () => post<{ ok: boolean; vault_item?: { name: string }; removed?: string[]; note?: string }>(`api/m365/users/${tenantId}/${encodeURIComponent(user.id)}/action`, { action, reason: reason.trim() || undefined }),
    onSuccess: (r) => {
      invalidate();
      onClose();
      if (r.vault_item) toast.success(t("m365.done_password", { item: r.vault_item.name }), { duration: 10_000 });
      else if (r.note) toast.warning(r.note);
      else toast.success(t(`m365.done.${action}`, { user: user.upn }));
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} title={t(`m365.action.${action}`)} description={user.upn ?? undefined}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant={DANGER.includes(action) ? "danger" : "primary"} loading={run.isPending} onClick={() => run.mutate()}>{t(`m365.action.${action}`)}</Button></>}>
      <div className="space-y-4">
        <p className="text-sm">{t(`m365.explain.${action}`)}</p>
        <Field label={t("m365.reason_label")} hint={t("m365.reason_hint")}><Textarea className="min-h-16 font-sans text-sm" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ risk */
function RiskTab({ detections, onOpen }: { detections: Detection[]; onOpen: (u: { tenant: string; id: string }) => void }) {
  const { t, i18n } = useTranslation();
  const { customerId } = useCustomer();
  const risky = useQuery({ queryKey: ["m365", "users", customerId, "risky", ""], queryFn: () => get<{ users: M365User[] }>(qs("api/m365/users", { customer_id: customerId, filter: "risky" })) });
  const nomfa = useQuery({ queryKey: ["m365", "users", customerId, "admins", ""], queryFn: () => get<{ users: M365User[] }>(qs("api/m365/users", { customer_id: customerId, filter: "admins" })) });
  const admins = (nomfa.data?.users ?? []).filter((u) => u.enabled && u.mfa && !u.mfa.registered);
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader icon={<ShieldAlert />} title={t("m365.risky_title")} description={t("m365.risky_desc")} />
        {!risky.data?.users.length ? <EmptyState icon={<ShieldCheck />} title={t("m365.no_risky")} /> : (
          <ul className="divide-y divide-line">{risky.data.users.map((u) => (
            <li key={u.id}><button type="button" className="flex w-full items-center justify-between gap-3 px-5 py-3 text-left hover:bg-sunken/60" onClick={() => onOpen({ tenant: u.tenant_id, id: u.id })}>
              <span><span className="block font-semibold">{u.name ?? u.upn}</span><span className="block text-xs text-muted">{u.upn} · {t(`m365.risk_state.${u.risk?.state}`, { defaultValue: u.risk?.state ?? "" })}</span></span>
              <RiskBadge level={u.risk?.level} /></button></li>))}</ul>
        )}
      </Card>
      <Card>
        <CardHeader icon={<Fingerprint />} title={t("m365.admins_no_mfa_title")} description={t("m365.admins_no_mfa_desc")} />
        {!admins.length ? <EmptyState icon={<ShieldCheck />} title={t("m365.all_admins_mfa")} /> : (
          <ul className="divide-y divide-line">{admins.map((u) => (
            <li key={u.id}><button type="button" className="flex w-full items-center justify-between gap-3 px-5 py-3 text-left hover:bg-sunken/60" onClick={() => onOpen({ tenant: u.tenant_id, id: u.id })}>
              <span><span className="block font-semibold">{u.name ?? u.upn}</span><span className="block text-xs text-muted">{u.roles.join(", ")}</span></span>
              <Badge tone="offline">{t("m365.mfa_off")}</Badge></button></li>))}</ul>
        )}
      </Card>
      <Card className="xl:col-span-2">
        <CardHeader icon={<AlertTriangle />} title={t("m365.detections")} description={t("m365.detections_desc")} />
        {!detections.length ? <EmptyState icon={<ShieldCheck />} title={t("m365.no_detections")} /> : (
          <Table>
            <thead><Tr><Th>{t("m365.col_when")}</Th><Th>{t("m365.col_user")}</Th><Th>{t("m365.col_detection")}</Th><Th>{t("m365.col_where")}</Th><Th>{t("m365.col_risk")}</Th></Tr></thead>
            <tbody>{detections.map((d) => (
              <Tr key={d.id}><Td className="whitespace-nowrap text-xs">{formatDate(d.at, i18n.language)}</Td><Td>{d.upn}<div className="text-xs text-muted">{d.tenant}</div></Td>
                <Td>{t(`m365.det.${d.type}`, { defaultValue: d.type })}</Td><Td className="text-xs">{[d.location, d.ip].filter(Boolean).join(" · ") || "–"}</Td><Td><RiskBadge level={d.level} /></Td></Tr>))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ sign-ins */
function SignInsTab({ tenants }: { tenants: Tenant[] }) {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const [flag, setFlag] = useState<(typeof FLAGS)[number]>("");
  const [hours, setHours] = useState("168");
  const q = useQuery({
    queryKey: ["m365", "signins", customerId, flag, hours],
    queryFn: () => get<{ signins: SignIn[] }>(qs("api/m365/signins", { customer_id: customerId, flag, hours })),
  });
  const noLog = tenants.filter((x) => x.features?.signins && !x.features.signins.ok);
  return (
    <Card>
      <CardHeader icon={<Globe2 />} title={t("m365.signins_title")} description={t("m365.signins_desc")}
        action={<Select className="w-auto" value={hours} onChange={(e) => setHours(e.target.value)}>
          {["24", "168", "720"].map((h) => <option key={h} value={h}>{t(`m365.hours_${h}`)}</option>)}
        </Select>} />
      <div className="border-b border-line px-5 py-3">
        <Segmented value={flag} onChange={setFlag} options={FLAGS.map((f) => ({ value: f, label: t(`m365.flag.${f || "all"}`) }))} />
      </div>
      {noLog.length > 0 && <div className="px-5 pt-4"><Alert tone="info">{t("m365.no_signin_log", { tenants: noLog.map((x) => x.name).join(", ") })}</Alert></div>}
      {q.isLoading ? <div className="p-5"><Spinner /></div> : !q.data?.signins.length ? <EmptyState icon={<ShieldCheck />} title={t("m365.no_signins")} /> : <SignInTable rows={q.data.signins} />}
    </Card>
  );
}

function SignInTable({ rows, compact }: { rows: SignIn[]; compact?: boolean }) {
  const { t, i18n } = useTranslation();
  return (
    <Table>
      <thead><Tr><Th>{t("m365.col_when")}</Th>{!compact && <Th>{t("m365.col_user")}</Th>}<Th>{t("m365.col_where")}</Th><Th>{t("m365.col_app")}</Th><Th>{t("m365.col_result")}</Th></Tr></thead>
      <tbody>{rows.map((s) => (
        <Tr key={s.id}>
          <Td className="whitespace-nowrap text-xs">{formatDate(s.at, i18n.language)}</Td>
          {!compact && <Td className="max-w-xs"><div className="truncate font-semibold">{s.name ?? s.upn}</div><div className="truncate text-xs text-muted">{s.upn}</div></Td>}
          <Td className="text-xs">{[s.city, s.country].filter(Boolean).join(", ") || "–"}<div className="text-muted">{s.ip}</div></Td>
          <Td className="text-xs">{s.app ?? "–"}<div className="text-muted">{s.client_app}</div></Td>
          <Td>
            <div className="flex flex-wrap gap-1">
              {s.success ? <Badge tone="online">{t("m365.success")}</Badge> : <Badge tone="offline">{t("m365.failed")}</Badge>}
              {s.flags.filter((f) => f !== "failed").map((f) => <Badge key={f} tone={f === "risky" ? "offline" : "warning"}>{t(`m365.flag.${f}`)}</Badge>)}
              <RiskBadge level={s.risk_level} />
            </div>
            {s.failure_reason && <div className="mt-1 text-xs text-muted">{s.failure_reason}</div>}
          </Td>
        </Tr>))}
      </tbody>
    </Table>
  );
}

/* ------------------------------------------------------------------ licences */
function LicencesTab() {
  const { t } = useTranslation();
  const { customerId } = useCustomer();
  const [showFree, setShowFree] = useState(false);
  const q = useQuery({ queryKey: ["m365", "licences", customerId], queryFn: () => get<{ licences: Licence[] }>(qs("api/m365/licences", { customer_id: customerId })) });
  const rows = (q.data?.licences ?? []).filter((l) => showFree || !l.free);
  return (
    <Card>
      <CardHeader icon={<BadgeCheck />} title={t("m365.licences_title")} description={t("m365.licences_desc")}
        action={<Switch checked={showFree} onCheckedChange={setShowFree} label={t("m365.show_free")} />} />
      {q.isLoading ? <div className="p-5"><Spinner /></div> : !rows.length ? <EmptyState icon={<BadgeCheck />} title={t("m365.no_licences")} /> : (
        <Table>
          <thead><Tr><Th>{t("m365.col_licence")}</Th><Th>{t("m365.col_tenant")}</Th><Th className="text-right">{t("m365.col_bought")}</Th><Th className="text-right">{t("m365.col_assigned")}</Th><Th className="text-right">{t("m365.col_free")}</Th><Th>{t("m365.col_status")}</Th></Tr></thead>
          <tbody>{rows.map((l) => (
            <Tr key={`${l.tenant}:${l.sku_id}`}>
              <Td><div className="font-semibold">{l.name}</div><div className="text-xs text-muted">{l.sku}</div></Td>
              <Td className="text-xs">{l.tenant}<div className="text-muted">{l.customer_name}</div></Td>
              <Td className="text-right tabular-nums">{l.free ? "∞" : l.enabled}</Td>
              <Td className="text-right tabular-nums">{l.consumed}</Td>
              <Td className={cn("text-right tabular-nums", l.available < 0 && "font-bold text-offline")}>{l.free ? "–" : l.available}</Td>
              <Td>{l.available < 0 && !l.free ? <Badge tone="offline">{t("m365.lic_short")}</Badge> : l.status === "Enabled" ? <Badge tone="online">{t("m365.lic_status.Enabled")}</Badge> : <Badge tone="warning">{t(`m365.lic_status.${l.status}`, { defaultValue: l.status })}</Badge>}</Td>
            </Tr>))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ connect / edit */
function TenantDialog({ tenant, onClose }: { tenant: Tenant | null; onClose: () => void }) {
  const { t } = useTranslation();
  const invalidate = useInvalidate();
  const customers = useCustomers();
  const { customerId } = useCustomer();
  const perms = useQuery({ queryKey: ["m365", "permissions"], queryFn: () => get<{ read: Perm[]; write: Perm[]; defaults: Settings }>("api/m365/permissions") });
  const [customer, setCustomer] = useState(tenant?.customer_id ?? customerId ?? "");
  const [name, setName] = useState(tenant?.name ?? "");
  const [tenantId, setTenantId] = useState(tenant?.tenant_id ?? "");
  const [clientId, setClientId] = useState(tenant?.client_id ?? "");
  const [method, setMethod] = useState<"secret" | "certificate">(tenant?.auth_method ?? "secret");
  const [source, setSource] = useState<"new" | "generate" | "paste" | "vault">(tenant ? "vault" : "new");
  const [secret, setSecret] = useState("");
  const [pem, setPem] = useState("");
  const [vaultId, setVaultId] = useState(tenant?.vault_item_id ?? "");
  const [enabled, setEnabled] = useState(tenant?.enabled ?? true);
  const s0 = tenant?.settings;
  const [countries, setCountries] = useState((s0?.allowed_countries ?? ["NL"]).join(", "));
  const [stale, setStale] = useState(String(s0?.stale_days ?? 90));
  const [failed, setFailed] = useState(String(s0?.failed_threshold ?? 20));
  const [warn, setWarn] = useState(String(s0?.secret_warn_days ?? 30));
  const [alerts, setAlerts] = useState({ alert_risky: s0?.alert_risky ?? true, alert_admin_mfa: s0?.alert_admin_mfa ?? true, alert_signins: s0?.alert_signins ?? true, alert_licences: s0?.alert_licences ?? true, alert_secret: s0?.alert_secret ?? true });
  const [cert, setCert] = useState<string | null>(null);
  const vault = useQuery({
    queryKey: ["m365", "vault-items", customer],
    queryFn: () => get<{ items: VaultItem[] }>(qs("api/vault/items", { customer_id: customer })),
    enabled: !!customer,
  });
  const vaultItems = useMemo(() => (vault.data?.items ?? []).filter((i) => i.customer_id === customer), [vault.data, customer]);

  const save = useMutation({
    mutationFn: () => {
      const cred: Record<string, unknown> = {};
      if (source === "vault" && vaultId && vaultId !== tenant?.vault_item_id) cred.vault_item_id = vaultId;
      if (source === "new" && method === "secret" && secret.trim()) cred.client_secret = secret.trim();
      if (source === "generate" && method === "certificate") cred.generate_certificate = true;
      if (source === "paste" && method === "certificate" && pem.trim()) cred.certificate_pem = pem.trim();
      const body = {
        name: name.trim(), tenant_id: tenantId.trim(), client_id: clientId.trim(), auth_method: method, enabled, ...cred,
        settings: { allowed_countries: countries, stale_days: Number(stale), failed_threshold: Number(failed), secret_warn_days: Number(warn), ...alerts },
      };
      return tenant ? patch<Tenant & { certificate?: string }>(`api/m365/tenants/${tenant.id}`, body)
        : post<Tenant & { certificate?: string }>("api/m365/tenants", { ...body, customer_id: customer });
    },
    onSuccess: (r) => {
      invalidate();
      toast.success(tenant ? t("m365.saved") : t("m365.connected"));
      if (r.certificate) setCert(r.certificate);
      else onClose();
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });

  if (cert) {
    const file = URL.createObjectURL(new Blob([cert], { type: "application/x-pem-file" }));
    return (
      <Dialog open onOpenChange={(v) => !v && onClose()} title={t("m365.cert_title")} description={t("m365.cert_desc")}
        footer={<Button variant="primary" onClick={onClose}>{t("common.done", { defaultValue: "Done" })}</Button>}>
        <div className="space-y-3 text-sm">
          <ol className="list-decimal space-y-1 pl-5"><li>{t("m365.cert_step1")}</li><li>{t("m365.cert_step2")}</li><li>{t("m365.cert_step3")}</li></ol>
          <a href={file} download="stackpilot.cer" className="inline-flex h-10 items-center gap-2 rounded-md bg-koraal px-4 font-semibold text-nacht"><Download className="size-4" /> {t("m365.download_cert")}</a>
        </div>
      </Dialog>
    );
  }

  const needCred = !tenant || method !== tenant.auth_method;
  const credOk = source === "vault" ? !!vaultId : source === "new" ? !!secret.trim() || (!needCred && method === "secret") : source === "paste" ? !!pem.trim() : true;
  const ok = name.trim() && tenantId.trim() && clientId.trim() && customer && credOk;
  const permText = (perms.data?.read ?? []).concat(perms.data?.write ?? []).map((p) => p.permission).join("\n");

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={tenant ? t("m365.edit_title", { name: tenant.name }) : t("m365.connect_title")} description={t("m365.connect_desc")}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={!ok} loading={save.isPending} onClick={() => save.mutate()}>{tenant ? t("common.save") : t("m365.connect")}</Button></>}>
      <div className="space-y-5">
        {!tenant && (
          <details className="rounded-md border border-line bg-sunken/50 px-4 py-3 text-sm" open>
            <summary className="cursor-pointer font-semibold">{t("m365.howto_title")}</summary>
            <ol className="mt-2 list-decimal space-y-1.5 pl-5">
              {[1, 2, 3, 4, 5, 6].map((n) => <li key={n}>{t(`m365.howto_${n}`)}</li>)}
            </ol>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div><div className="mb-1 text-xs font-semibold uppercase text-muted">{t("m365.perm_read")}</div>
                <ul className="space-y-0.5 text-xs">{perms.data?.read.map((p) => <li key={p.permission}><code className="mono">{p.permission}</code> <span className="text-muted">· {p.for}</span></li>)}</ul></div>
              <div><div className="mb-1 text-xs font-semibold uppercase text-muted">{t("m365.perm_write")}</div>
                <ul className="space-y-0.5 text-xs">{perms.data?.write.map((p) => <li key={p.permission}><code className="mono">{p.permission}</code> <span className="text-muted">· {p.for}</span></li>)}</ul></div>
            </div>
            <div className="mt-3"><CopyButton value={permText} label={t("m365.copy_permissions")} /></div>
          </details>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("m365.f_customer")}>
            <Select value={customer} disabled={!!tenant} onChange={(e) => setCustomer(e.target.value)}>
              <option value="">{t("m365.choose_customer")}</option>
              {(customers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
          <Field label={t("m365.f_name")} hint={t("m365.f_name_hint")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label={t("m365.f_tenant")} hint={t("m365.f_tenant_hint")}><Input className="mono" value={tenantId} onChange={(e) => setTenantId(e.target.value)} placeholder="00000000-0000-0000-0000-000000000000" /></Field>
          <Field label={t("m365.f_client")} hint={t("m365.f_client_hint")}><Input className="mono" value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="00000000-0000-0000-0000-000000000000" /></Field>
        </div>
        <Field label={t("m365.f_method")} hint={t("m365.f_method_hint")}>
          <Segmented value={method} onChange={(v) => { setMethod(v); setSource(v === "secret" ? "new" : "generate"); }}
            options={[{ value: "secret", label: t("m365.auth.secret") }, { value: "certificate", label: t("m365.auth.certificate") }]} />
        </Field>
        <Field label={t("m365.f_credential")} hint={t("m365.f_credential_hint")}>
          <Segmented value={source} onChange={setSource} options={(method === "secret"
            ? [{ value: "new" as const, label: t("m365.src.new_secret") }, { value: "vault" as const, label: t("m365.src.vault") }]
            : [{ value: "generate" as const, label: t("m365.src.generate") }, { value: "paste" as const, label: t("m365.src.paste") }, { value: "vault" as const, label: t("m365.src.vault") }])} />
        </Field>
        {source === "new" && method === "secret" && <Field label={t("m365.f_secret")} hint={tenant ? t("m365.f_secret_keep") : t("m365.f_secret_hint")}><Input type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>}
        {source === "paste" && method === "certificate" && <Field label={t("m365.f_pem")} hint={t("m365.f_pem_hint")}><Textarea value={pem} onChange={(e) => setPem(e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" /></Field>}
        {source === "generate" && method === "certificate" && <Alert tone="info">{t("m365.generate_hint")}</Alert>}
        {source === "vault" && (
          <Field label={t("m365.f_vault")} hint={t("m365.f_vault_hint")}>
            <Select value={vaultId} onChange={(e) => setVaultId(e.target.value)}>
              <option value="">{t("m365.choose_vault")}</option>
              {vaultItems.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </Select>
          </Field>
        )}
        <div className="rounded-md border border-line p-4">
          <h3 className="mb-3 text-sm font-bold">{t("m365.settings_title")}</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("m365.s_countries")} hint={t("m365.s_countries_hint")}><Input value={countries} onChange={(e) => setCountries(e.target.value.toUpperCase())} /></Field>
            <Field label={t("m365.s_stale")}><Input type="number" min={7} max={730} value={stale} onChange={(e) => setStale(e.target.value)} /></Field>
            <Field label={t("m365.s_failed")} hint={t("m365.s_failed_hint")}><Input type="number" min={3} value={failed} onChange={(e) => setFailed(e.target.value)} /></Field>
            <Field label={t("m365.s_warn")}><Input type="number" min={1} max={365} value={warn} onChange={(e) => setWarn(e.target.value)} /></Field>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {(Object.keys(alerts) as (keyof typeof alerts)[]).map((k) => (
              <Switch key={k} checked={alerts[k]} onCheckedChange={(v) => setAlerts({ ...alerts, [k]: v })} label={t(`m365.s_${k}`)} />
            ))}
            <Switch checked={enabled} onCheckedChange={setEnabled} label={t("m365.s_enabled")} description={t("m365.s_enabled_hint")} />
          </div>
        </div>
      </div>
    </Dialog>
  );
}
