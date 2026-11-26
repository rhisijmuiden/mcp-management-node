import * as DM from "@radix-ui/react-dropdown-menu";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRange, ChevronDown, Eye, Inbox, LifeBuoy, LogOut, Menu, Monitor, Moon, Plus, Sun, UserRound, X } from "lucide-react";
import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { Alert, Spinner } from "@/components/ui/misc";
import i18n from "@/i18n";
import { errorMessage } from "@/lib/api";
import { applyTheme, localStorageGet, localStorageSet } from "@/lib/auth";
import { appUrl, cn } from "@/lib/utils";
import { AuthShell } from "@/pages/AuthPages";
import { endPreview, pget, ppatch, ppost, setPortalUnauthorizedHandler, type PortalMe, type PortalStatus } from "./api";
import { PortalAccountPage, PortalChangesPage, PortalHomePage, PortalNewTicketPage, PortalTicketPage } from "./PortalPages";

/* ------------------------------------------------------------------ session */
interface Ctx { status?: PortalStatus; me?: PortalMe | null; loading: boolean; lang: string; refresh: () => Promise<void> }
const PortalCtx = createContext<Ctx>({ loading: true, lang: "en", refresh: async () => {} });
export const usePortal = () => useContext(PortalCtx);

function PortalProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  // The language is part of the context: the portal remounts when it changes, so no component keeps
  // texts of the previous language (the account's language is only known after signing in).
  const [lang, setLangState] = useState(i18n.language);
  useEffect(() => {
    const on = (l: string) => setLangState(l);
    i18n.on("languageChanged", on);
    return () => i18n.off("languageChanged", on);
  }, []);
  const status = useQuery({ queryKey: ["portal-status"], queryFn: () => pget<PortalStatus>("status") });
  const me = useQuery({
    queryKey: ["portal-me"], queryFn: () => pget<PortalMe>("me").catch(() => null), enabled: !!status.data?.authenticated,
  });
  useEffect(() => {
    setPortalUnauthorizedHandler(() => {
      qc.setQueryData(["portal-me"], null);
      qc.invalidateQueries({ queryKey: ["portal-status"] });
    });
  }, [qc]);
  const user = status.data && !status.data.authenticated ? null : me.data;
  const settled = !!status.data && (!status.data.authenticated || !me.isLoading);
  useEffect(() => {
    if (!settled) return; // wait for the account, so the language doesn't flip back and forth
    const lang = user?.language || localStorageGet("mmn-lang") || status.data?.default_language || "en";
    if (i18n.language !== lang) i18n.changeLanguage(lang);
    document.documentElement.lang = lang;
    applyTheme(localStorageGet("mmn-theme") || "system");
  }, [settled, user?.language, status.data?.default_language]);
  useEffect(() => {
    document.title = status.data ? `${i18n.t("portal.title")} · ${status.data.app_name}` : i18n.t("portal.title");
  }, [status.data]);
  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["portal-status"] });
    await qc.invalidateQueries({ queryKey: ["portal-me"] });
  };
  return (
    <PortalCtx.Provider value={{ status: status.data, me: user, lang, loading: status.isLoading || (!!status.data?.authenticated && me.isLoading), refresh }}>
      {children}
    </PortalCtx.Provider>
  );
}

function RequirePortal({ children }: { children: ReactNode }) {
  const { me, status, loading, lang } = usePortal();
  const loc = useLocation();
  if (loading) return <div className="grid h-full place-items-center"><Spinner /></div>;
  if (status && !status.enabled) return <Navigate to="/portal/login" replace />;
  if (!me) return <Navigate to={`/portal/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  return <div key={lang} className="h-full">{children}</div>;
}

export default function PortalApp() {
  return (
    <PortalProvider>
      <Routes>
        <Route path="login" element={<PortalLoginPage />} />
        <Route path="forgot-password" element={<PortalForgotPage />} />
        <Route path="set-password" element={<PortalSetPasswordPage />} />
        <Route element={<RequirePortal><PortalLayout /></RequirePortal>}>
          <Route index element={<PortalHomePage />} />
          <Route path="tickets/new" element={<PortalNewTicketPage />} />
          <Route path="tickets/:id" element={<PortalTicketPage />} />
          <Route path="changes" element={<PortalChangesPage />} />
          <Route path="account" element={<PortalAccountPage />} />
          <Route path="*" element={<Navigate to="/portal" replace />} />
        </Route>
      </Routes>
    </PortalProvider>
  );
}

/* ------------------------------------------------------------------ sign-in pages */
function Shell({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children: ReactNode }) {
  const { t } = useTranslation();
  const { status } = usePortal();
  return (
    <AuthShell title={title} subtitle={subtitle} heading={t("portal.title")} pill={status?.app_name || "Stackpilot"} tagline={t("portal.tagline")}>
      {children}
    </AuthShell>
  );
}

function PortalLoginPage() {
  const { t } = useTranslation();
  const { status, me, refresh } = usePortal();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [f, setF] = useState({ email: "", password: "", remember: false });
  const login = useMutation({
    mutationFn: () => ppost("login", f),
    onSuccess: async () => { await refresh(); navigate(params.get("next")?.startsWith("/portal") ? params.get("next")! : "/portal", { replace: true }); },
  });
  if (me) return <Navigate to="/portal" replace />;
  const submit = (e: FormEvent) => { e.preventDefault(); login.mutate(); };
  return (
    <Shell title={t("portal.login_title")} subtitle={t("portal.login_subtitle")}>
      {status && !status.enabled ? <Alert tone="warning">{t("portal.disabled")}</Alert> : (
        <form onSubmit={submit} className="space-y-4">
          {login.isError && <Alert tone="offline">{errorMessage(login.error, t)}</Alert>}
          <Field label={t("portal.email")} htmlFor="pe">
            <Input id="pe" type="email" autoComplete="username" autoFocus value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required />
          </Field>
          <Field label={t("auth.password")} htmlFor="pp">
            <Input id="pp" type="password" autoComplete="current-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required />
          </Field>
          <div className="flex items-center justify-between">
            <Checkbox checked={f.remember} onCheckedChange={(v) => setF({ ...f, remember: v })} label={t("auth.remember")} />
            {status?.mail_enabled && <Link to="/portal/forgot-password" className="text-sm font-semibold text-accent-text hover:underline">{t("auth.forgot")}</Link>}
          </div>
          <Button variant="primary" size="lg" className="w-full" loading={login.isPending}>{t("auth.login")}</Button>
          <p className="text-center text-xs text-muted">{t("portal.no_account")}</p>
        </form>
      )}
    </Shell>
  );
}

function PortalForgotPage() {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");
  const send = useMutation({ mutationFn: () => ppost("forgot", { email }) });
  return (
    <Shell title={t("auth.forgot_title")} subtitle={t("portal.forgot_subtitle")}>
      {send.isSuccess ? <Alert tone="online">{t("auth.forgot_sent")}</Alert> : (
        <form onSubmit={(e) => { e.preventDefault(); send.mutate(); }} className="space-y-4">
          {send.isError && <Alert tone="offline">{errorMessage(send.error, t)}</Alert>}
          <Field label={t("portal.email")}><Input type="email" autoFocus value={email} onChange={(e) => setEmail(e.target.value)} required /></Field>
          <Button variant="primary" size="lg" className="w-full" loading={send.isPending}>{t("auth.send_link")}</Button>
        </form>
      )}
      <div className="mt-6 text-sm"><Link to="/portal/login" className="font-semibold text-accent-text hover:underline">← {t("auth.back_to_login")}</Link></div>
    </Shell>
  );
}

function PortalSetPasswordPage() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { refresh } = usePortal();
  const [pw, setPw] = useState({ a: "", b: "" });
  const save = useMutation({
    mutationFn: () => ppost("set-password", { token: params.get("token") ?? "", password: pw.a }),
    onSuccess: async () => { await refresh(); navigate("/portal", { replace: true }); },
  });
  const mismatch = !!pw.b && pw.a !== pw.b;
  return (
    <Shell title={t("portal.set_title")} subtitle={t("portal.set_subtitle")}>
      <form onSubmit={(e) => { e.preventDefault(); if (!mismatch) save.mutate(); }} className="space-y-4">
        {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
        <Field label={t("auth.new_password")} hint={t("portal.password_hint")}><Input type="password" autoComplete="new-password" minLength={10} value={pw.a} onChange={(e) => setPw({ ...pw, a: e.target.value })} required /></Field>
        <Field label={t("portal.confirm_password")}><Input type="password" autoComplete="new-password" value={pw.b} onChange={(e) => setPw({ ...pw, b: e.target.value })} required /></Field>
        {mismatch && <Alert tone="warning">{t("auth.password_mismatch")}</Alert>}
        <Button variant="primary" size="lg" className="w-full" loading={save.isPending} disabled={mismatch}>{t("portal.set_button")}</Button>
      </form>
    </Shell>
  );
}

/* ------------------------------------------------------------------ layout */
function PortalLayout() {
  const { t } = useTranslation();
  const { me, status, refresh } = usePortal();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const items = [
    { to: "/portal", icon: <Inbox />, label: t("portal.nav.tickets"), end: true },
    { to: "/portal/tickets/new", icon: <Plus />, label: t("portal.nav.new") },
    { to: "/portal/changes", icon: <CalendarRange />, label: t("portal.nav.changes") },
    { to: "/portal/account", icon: <UserRound />, label: t("portal.nav.account") },
  ];
  const logout = async () => {
    if (status?.preview) return exitPreview();
    await ppost("logout");
    qc.removeQueries({ queryKey: ["portal-tickets"] });
    await refresh();
    navigate("/portal/login");
  };
  const setLang = async (lang: string) => {
    localStorageSet("mmn-lang", lang);
    i18n.changeLanguage(lang);
    if (!status?.preview) await ppatch("me", { language: lang });
    await refresh();
  };
  const setTheme = (th: string) => { localStorageSet("mmn-theme", th); applyTheme(th); };
  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex h-16 items-center gap-3 px-5">
        <img src="brand/rhis-icon.svg" alt="" className="size-8 rounded-[8px]" />
        <div className="min-w-0 leading-tight">
          <div className="display line-clamp-1 text-[14px]">{t("portal.title")}</div>
          <div className="truncate text-[11px] font-semibold uppercase tracking-wider text-muted">{status?.app_name || "Stackpilot"}</div>
        </div>
      </div>
      <div className="mx-3 mb-2 rounded-md bg-sunken px-3 py-2.5">
        <div className="text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t("portal.organisation")}</div>
        <div className="truncate text-sm font-semibold">{me?.customer_name}</div>
      </div>
      <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 pb-4 pt-2">
        {items.map((i) => (
          <NavLink key={i.to} to={i.to} end={i.end} onClick={() => setOpen(false)}
            className={({ isActive }) => cn("flex items-center gap-3 rounded-md px-3 py-2 text-[14px] font-medium transition-colors [&_svg]:size-[18px]",
              isActive ? "bg-nacht text-white dark:bg-lucht/15 dark:text-ink [&_svg]:text-koraal" : "text-ink hover:bg-sunken")}>
            {i.icon}<span className="flex-1 truncate">{i.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-line p-4 text-xs text-muted">
        <div className="flex items-center gap-2"><LifeBuoy className="size-4 text-koraal" /> {t("portal.help_hint")}</div>
      </div>
    </div>
  );
  const exitPreview = () => {
    const cid = me?.customer_id;
    endPreview();
    qc.clear();
    window.location.assign(appUrl(cid ? `/customers/${cid}` : "/"));
  };
  return (
    <div className="flex h-full flex-col">
      {status?.preview && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b-2 border-koraal bg-accent-soft px-4 py-2 text-[13px]" role="status">
          <Eye className="size-4 shrink-0 text-accent-text" />
          <span className="min-w-0 flex-1">
            <Trans i18nKey="portal.preview_band" values={{ name: me?.customer_name ?? "" }} components={{ b: <b /> }} />
          </span>
          <button type="button" onClick={exitPreview} className="cursor-pointer font-bold text-accent-text hover:underline">{t("portal.preview_exit")}</button>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
      <aside className="hidden w-[264px] shrink-0 border-r border-line bg-raised lg:block">{sidebar}</aside>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-nacht/50" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-[280px] bg-raised shadow-2xl">
            <button className="absolute right-3 top-4 rounded p-1 hover:bg-sunken" onClick={() => setOpen(false)} aria-label="Close menu"><X className="size-5" /></button>
            {sidebar}
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line bg-surface/85 px-4 backdrop-blur lg:px-8">
          <button className="rounded-md p-2 hover:bg-sunken lg:hidden" onClick={() => setOpen(true)} aria-label="Menu"><Menu className="size-5" /></button>
          <div className="flex-1" />
          <button onClick={() => setLang(i18n.language === "nl" ? "en" : "nl")} title={t("account.language")}
            className="cursor-pointer rounded-md border border-line bg-raised px-2.5 py-1.5 text-xs font-bold uppercase hover:bg-sunken">
            {i18n.language === "nl" ? "NL" : "EN"}
          </button>
          <DM.Root>
            <DM.Trigger className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-sunken focus:outline-none">
              <span className="grid size-8 place-items-center rounded-full bg-nacht text-sm font-bold text-white dark:bg-lucht dark:text-nacht">
                {(me?.full_name || me?.email || "?").slice(0, 1).toUpperCase()}
              </span>
              <span className="hidden text-left text-sm leading-tight sm:block">
                <span className="block font-semibold">{me?.full_name || me?.email}</span>
                <span className="block text-xs text-muted">{t(`portal.role.${me?.role ?? "member"}`)}</span>
              </span>
              <ChevronDown className="size-4 text-muted" />
            </DM.Trigger>
            <DM.Portal>
              <DM.Content align="end" sideOffset={6} className="z-50 min-w-56 rounded-md border border-line bg-raised p-1 shadow-xl">
                <DM.Item onSelect={() => navigate("/portal/account")} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
                  <UserRound className="size-4" /> {t("portal.nav.account")}
                </DM.Item>
                <DM.Separator className="my-1 h-px bg-line" />
                {(["system", "light", "dark"] as const).map((th) => (
                  <DM.Item key={th} onSelect={() => setTheme(th)} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
                    {th === "light" ? <Sun className="size-4" /> : th === "dark" ? <Moon className="size-4" /> : <Monitor className="size-4" />}
                    {t(`account.theme_${th}`)}
                  </DM.Item>
                ))}
                <DM.Separator className="my-1 h-px bg-line" />
                <DM.Item onSelect={logout} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm text-offline outline-none data-[highlighted]:bg-sunken">
                  <LogOut className="size-4" /> {t("auth.logout")}
                </DM.Item>
              </DM.Content>
            </DM.Portal>
          </DM.Root>
        </header>
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1080px] px-4 py-6 lg:px-8 lg:py-8"><Outlet /></div>
        </main>
      </div>
      </div>
    </div>
  );
}
