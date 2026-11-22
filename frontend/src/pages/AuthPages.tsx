import { useMutation, useQuery } from "@tanstack/react-query";
import { Bot, ShieldCheck } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";
import { CustomerScopePicker } from "@/components/customers";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { Alert, Spinner } from "@/components/ui/misc";
import { errorMessage, get, post } from "@/lib/api";
import { localStorageSet, useAuth } from "@/lib/auth";
import i18n from "@/i18n";

function LangSwitch() {
  const set = (l: string) => {
    localStorageSet("mmn-lang", l);
    i18n.changeLanguage(l);
  };
  return (
    <div className="flex gap-1 text-xs font-bold">
      {["en", "nl"].map((l) => (
        <button key={l} onClick={() => set(l)} className={`cursor-pointer rounded px-2 py-1 uppercase ${i18n.language === l ? "bg-white/15 text-white" : "text-white/60 hover:text-white"}`}>
          {l}
        </button>
      ))}
    </div>
  );
}

export function AuthShell({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="grid min-h-full lg:grid-cols-[1fr_minmax(440px,560px)]">
      <div className="relative hidden overflow-hidden bg-nacht p-12 text-white lg:flex lg:flex-col">
        <div className="flex items-center justify-between">
          <img src="brand/rhis-logo-wit.svg" alt="RHIS" className="h-8" />
          <LangSwitch />
        </div>
        <div className="mt-auto max-w-lg">
          <div className="mb-5 inline-flex items-center gap-2 rounded-full bg-lucht/15 px-3 py-1 text-xs font-semibold text-lucht">
            <Bot className="size-3.5" /> {t("auth.pill")}
          </div>
          <h1 className="display text-[44px] leading-[48px]">Stackpilot</h1>
          <p className="mt-4 text-lg text-white/75">{t("auth.tagline")}</p>
        </div>
        <img src="brand/rhis-antenne.svg" alt="" className="pointer-events-none absolute -bottom-24 -right-24 size-[420px] opacity-[0.07]" />
      </div>
      <div className="flex items-center justify-center bg-surface px-4 py-10">
        <div className="w-full max-w-[400px]">
          <div className="mb-8 flex items-center justify-between lg:hidden">
            <img src="brand/rhis-logo.svg" alt="RHIS" className="h-7 dark:hidden" />
            <img src="brand/rhis-logo-wit.svg" alt="RHIS" className="hidden h-7 dark:block" />
          </div>
          <h2 className="display text-[28px] leading-[34px]">{title}</h2>
          {subtitle && <p className="mt-1 text-muted">{subtitle}</p>}
          <div className="mt-8">{children}</div>
        </div>
      </div>
    </div>
  );
}

export function LoginPage() {
  const { t } = useTranslation();
  const { status, refresh } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: "", password: "", remember: false });
  const login = useMutation({
    mutationFn: () => post("api/auth/login", form),
    onSuccess: async () => {
      await refresh();
      navigate(params.get("next") || "/", { replace: true });
    },
  });
  if (status?.setup_required) return <Navigate to="/setup" replace />;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate();
  };
  return (
    <AuthShell title={t("auth.welcome")} subtitle={t("auth.login_subtitle")}>
      <form onSubmit={submit} className="space-y-4">
        {login.isError && <Alert tone="offline">{errorMessage(login.error, t)}</Alert>}
        <Field label={t("auth.username")} htmlFor="u">
          <Input id="u" autoComplete="username" autoFocus value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required />
        </Field>
        <Field label={t("auth.password")} htmlFor="p">
          <Input id="p" type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
        </Field>
        <div className="flex items-center justify-between">
          <Checkbox checked={form.remember} onCheckedChange={(v) => setForm({ ...form, remember: v })} label={t("auth.remember")} />
          {status?.mail_enabled && (
            <Link to="/forgot-password" className="text-sm font-semibold text-accent-text hover:underline">
              {t("auth.forgot")}
            </Link>
          )}
        </div>
        <Button variant="primary" size="lg" className="w-full" loading={login.isPending}>
          {t("auth.login")}
        </Button>
      </form>
    </AuthShell>
  );
}

export function SetupPage() {
  const { t } = useTranslation();
  const { status, refresh } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ setup_token: "", username: "admin", email: "", full_name: "", password: "", password2: "" });
  const setup = useMutation({
    mutationFn: () =>
      post("api/auth/setup", {
        setup_token: form.setup_token,
        username: form.username,
        email: form.email || null,
        full_name: form.full_name || null,
        password: form.password,
        language: i18n.language === "nl" ? "nl" : "en",
      }),
    onSuccess: async () => {
      await refresh();
      navigate("/", { replace: true });
    },
  });
  if (status && !status.setup_required) return <Navigate to="/login" replace />;
  const mismatch = form.password2.length > 0 && form.password !== form.password2;
  return (
    <AuthShell title={t("setup.title")} subtitle={t("setup.subtitle")}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!mismatch) setup.mutate();
        }}
        className="space-y-4"
      >
        {setup.isError && <Alert tone="offline">{errorMessage(setup.error, t)}</Alert>}
        <Field label={t("setup.token")} hint={<span>{t("setup.token_hint")} <code className="mono">sudo mmn-ctl setup-token</code></span>}>
          <Input className="mono" value={form.setup_token} onChange={(e) => setForm({ ...form, setup_token: e.target.value })} required />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("auth.username")}>
            <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required />
          </Field>
          <Field label={t("users.full_name")}>
            <Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
          </Field>
        </div>
        <Field label={t("users.email")} hint={t("setup.email_hint")}>
          <Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        <Field label={t("auth.password")} hint={t("auth.password_rules")}>
          <Input type="password" autoComplete="new-password" minLength={10} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
        </Field>
        <Field label={t("auth.password_repeat")}>
          <Input type="password" autoComplete="new-password" value={form.password2} onChange={(e) => setForm({ ...form, password2: e.target.value })} required />
        </Field>
        {mismatch && <Alert tone="warning">{t("auth.password_mismatch")}</Alert>}
        <Button variant="primary" size="lg" className="w-full" loading={setup.isPending}>
          {t("setup.create")}
        </Button>
      </form>
    </AuthShell>
  );
}

export function ForgotPage() {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const m = useMutation({ mutationFn: () => post("api/auth/forgot", { username_or_email: value }) });
  return (
    <AuthShell title={t("auth.forgot_title")} subtitle={t("auth.forgot_subtitle")}>
      {m.isSuccess ? (
        <Alert tone="online">{t("auth.forgot_sent")}</Alert>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-4">
          {m.isError && <Alert tone="offline">{errorMessage(m.error, t)}</Alert>}
          <Field label={t("auth.username_or_email")}>
            <Input value={value} onChange={(e) => setValue(e.target.value)} required autoFocus />
          </Field>
          <Button variant="primary" size="lg" className="w-full" loading={m.isPending}>{t("auth.send_link")}</Button>
        </form>
      )}
      <Link to="/login" className="mt-6 inline-block text-sm font-semibold text-accent-text hover:underline">← {t("auth.back_to_login")}</Link>
    </AuthShell>
  );
}

export function ResetPage() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const [pw, setPw] = useState({ a: "", b: "" });
  const m = useMutation({ mutationFn: () => post("api/auth/reset", { token: params.get("token"), password: pw.a }) });
  const mismatch = pw.b.length > 0 && pw.a !== pw.b;
  return (
    <AuthShell title={t("auth.reset_title")}>
      {m.isSuccess ? (
        <>
          <Alert tone="online">{t("auth.reset_done")}</Alert>
          <Link to="/login" className="mt-6 inline-block text-sm font-semibold text-accent-text hover:underline">{t("auth.login")} →</Link>
        </>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); if (!mismatch) m.mutate(); }} className="space-y-4">
          {m.isError && <Alert tone="offline">{errorMessage(m.error, t)}</Alert>}
          <Field label={t("auth.new_password")} hint={t("auth.password_rules")}>
            <Input type="password" minLength={10} autoComplete="new-password" value={pw.a} onChange={(e) => setPw({ ...pw, a: e.target.value })} required />
          </Field>
          <Field label={t("auth.password_repeat")}>
            <Input type="password" autoComplete="new-password" value={pw.b} onChange={(e) => setPw({ ...pw, b: e.target.value })} required />
          </Field>
          {mismatch && <Alert tone="warning">{t("auth.password_mismatch")}</Alert>}
          <Button variant="primary" size="lg" className="w-full" loading={m.isPending}>{t("auth.save_password")}</Button>
        </form>
      )}
    </AuthShell>
  );
}

interface ClientInfoResp {
  client_id: string;
  client_name: string;
  client_uri: string | null;
  redirect_hosts: string[];
  scopes: { scope: string; description: string }[];
}

/** OAuth consent: an AI app (Claude, ChatGPT, Copilot) asks for access to the MCP server. */
export function ConsentPage() {
  const { t } = useTranslation();
  const { user, loading } = useAuth();
  const [params] = useSearchParams();
  const clientId = params.get("client_id") || "";
  const requested = (params.get("scope") || "read").split(/[ +]/).filter(Boolean);
  const info = useQuery({ queryKey: ["oauth-client", clientId], queryFn: () => get<ClientInfoResp>(`api/oauth/clients/${encodeURIComponent(clientId)}`), enabled: !!user });
  const [scopes, setScopes] = useState<string[] | null>(null);
  const [customerIds, setCustomerIds] = useState<string[] | null>(null);
  const chosen = scopes ?? requested.filter((s) => info.data?.scopes.some((x) => x.scope === s)).concat(["read"]);
  const decide = useMutation({
    mutationFn: (approve: boolean) =>
      post<{ redirect: string }>("api/oauth/approve", {
        client_id: clientId,
        redirect_uri: params.get("redirect_uri"),
        code_challenge: params.get("code_challenge"),
        state: params.get("state"),
        resource: params.get("resource"),
        scopes: Array.from(new Set(chosen)),
        customer_ids: customerIds,
        approve,
      }),
    onSuccess: (r) => window.location.assign(r.redirect),
  });
  if (loading) return <div className="grid h-full place-items-center"><Spinner /></div>;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(`/connect/consent?${params.toString()}`)}`} replace />;
  return (
    <AuthShell title={t("consent.title")} subtitle={t("consent.subtitle")}>
      {info.isLoading ? (
        <Spinner />
      ) : info.isError ? (
        <Alert tone="offline">{t("consent.unknown_client")}</Alert>
      ) : (
        <div className="space-y-5">
          <div className="flex items-center gap-3 rounded-lg border border-line bg-raised p-4">
            <div className="grid size-11 place-items-center rounded-md bg-nacht text-koraal"><Bot className="size-6" /></div>
            <div>
              <div className="font-bold">{info.data!.client_name}</div>
              <div className="text-xs text-muted">{t("consent.returns_to")} {info.data!.redirect_hosts.join(", ")}</div>
            </div>
          </div>
          <div>
            <div className="mb-2 text-sm font-semibold">{t("consent.permissions", { user: user.username })}</div>
            <div className="space-y-3 rounded-lg border border-line bg-raised p-4">
              {info.data!.scopes.map((s) => (
                <Checkbox
                  key={s.scope}
                  disabled={s.scope === "read"}
                  checked={chosen.includes(s.scope)}
                  onCheckedChange={(v) => setScopes(v ? [...chosen, s.scope] : chosen.filter((x) => x !== s.scope))}
                  label={t(`scopes.${s.scope}`)}
                  description={t(`scopes.${s.scope}_desc`)}
                />
              ))}
            </div>
          </div>
          <div>
            <div className="mb-2 text-sm font-semibold">{t("cust.consent_customers")}</div>
            <div className="rounded-lg border border-line bg-raised p-4"><CustomerScopePicker value={customerIds} onChange={setCustomerIds} /></div>
          </div>
          <Alert tone="info"><span className="inline-flex items-center gap-1"><ShieldCheck className="size-4" />{t("consent.logged")}</span></Alert>
          <div className="flex gap-2">
            <Button variant="primary" size="lg" className="flex-1" loading={decide.isPending && decide.variables} disabled={customerIds !== null && !customerIds.length} onClick={() => decide.mutate(true)}>{t("consent.allow")}</Button>
            <Button size="lg" onClick={() => decide.mutate(false)}>{t("consent.deny")}</Button>
          </div>
        </div>
      )}
    </AuthShell>
  );
}
