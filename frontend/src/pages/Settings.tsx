import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Globe, Mail, Send, Settings as SettingsIcon, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Field, Input, Segmented, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner } from "@/components/ui/misc";
import { errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { MailSettings, ProxySettings } from "@/lib/types";

/* ------------------------------------------------------------------ mail */
export function MailPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["mail"], queryFn: () => get<MailSettings>("api/settings/mail") });
  const [f, setF] = useState<(MailSettings & { password: string; dkim_private_key: string }) | null>(null);
  const [to, setTo] = useState("");
  useEffect(() => {
    if (q.data) setF({ ...q.data, password: "", dkim_private_key: "" });
  }, [q.data]);
  useEffect(() => {
    if (user?.email) setTo(user.email);
  }, [user?.email]);

  const payload = () => {
    if (!f) return {};
    const { password_set: _a, dkim_key_set: _b, password, dkim_private_key, ...rest } = f;
    void _a;
    void _b;
    return { ...rest, reply_to: rest.reply_to || "", ...(password ? { password } : {}), ...(dkim_private_key ? { dkim_private_key } : {}) };
  };
  const save = useMutation({
    mutationFn: () => put("api/settings/mail", payload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["auth-status"] });
      toast.success(t("common.saved"));
    },
  });
  const test = useMutation({
    mutationFn: () => post<{ ok: boolean; error?: string }>("api/settings/mail/test", { to, settings: payload() }),
    onSuccess: (r) => (r.ok ? toast.success(t("mail.test_sent", { to })) : toast.error(t("mail.test_failed"), { description: r.error })),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!f) return <Spinner />;
  const set = (patch: Partial<typeof f>) => setF({ ...f, ...patch });
  const presetPort = (security: MailSettings["security"]) => set({ security, port: security === "tls" ? 465 : security === "starttls" ? 587 : 25 });

  return (
    <>
      <PageHeader title={t("mail.title")} description={t("mail.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader icon={<Mail />} title={t("mail.server")} action={<Switch checked={f.enabled} onCheckedChange={(v) => set({ enabled: v })} label={f.enabled ? t("common.on") : t("common.off")} />} />
          <CardBody className="space-y-5">
            {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label={t("mail.host")} className="sm:col-span-2"><Input value={f.host} onChange={(e) => set({ host: e.target.value })} placeholder="smtp.example.com" /></Field>
              <Field label={t("backups.port")}><Input type="number" value={f.port} onChange={(e) => set({ port: Number(e.target.value) })} /></Field>
            </div>
            <Field label={t("mail.security")} hint={t(`mail.security_${f.security}_hint`)}>
              <Segmented value={f.security} onChange={presetPort} options={[
                { value: "starttls", label: "STARTTLS (587)" },
                { value: "tls", label: "SSL/TLS (465)" },
                { value: "none", label: t("mail.security_none") },
              ]} />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("auth.username")} hint={t("mail.username_hint")}><Input value={f.username} autoComplete="off" onChange={(e) => set({ username: e.target.value })} /></Field>
              <Field label={t("auth.password")} hint={f.password_set ? t("common.secret_keep") : undefined}><Input type="password" autoComplete="new-password" value={f.password} onChange={(e) => set({ password: e.target.value })} /></Field>
              <Field label={t("mail.from_name")}><Input value={f.from_name} onChange={(e) => set({ from_name: e.target.value })} /></Field>
              <Field label={t("mail.from_address")}><Input type="email" value={f.from_address} onChange={(e) => set({ from_address: e.target.value })} placeholder="noreply@example.com" /></Field>
              <Field label={t("mail.reply_to")}><Input type="email" value={f.reply_to} onChange={(e) => set({ reply_to: e.target.value })} /></Field>
              <Field label={t("mail.timeout")}><Input type="number" min={3} max={120} value={f.timeout} onChange={(e) => set({ timeout: Number(e.target.value) })} /></Field>
            </div>
            {f.security !== "none" && (
              <div className="grid gap-4 rounded-md border border-line p-4 sm:grid-cols-2">
                <Switch checked={f.verify_tls} onCheckedChange={(v) => set({ verify_tls: v })} label={t("backups.verify_tls")} description={t("mail.verify_tls_hint")} />
                <Field label={t("mail.min_tls")}>
                  <Select value={f.min_tls} onChange={(e) => set({ min_tls: e.target.value as "1.2" | "1.3" })}>
                    <option value="1.2">TLS 1.2</option>
                    <option value="1.3">TLS 1.3</option>
                  </Select>
                </Field>
              </div>
            )}
            {f.security === "none" && <Alert tone="warning">{t("mail.no_tls_warning")}</Alert>}

            <div className="rounded-md border border-line p-4">
              <Switch checked={f.dkim_enabled} onCheckedChange={(v) => set({ dkim_enabled: v })} label={t("mail.dkim")} description={t("mail.dkim_hint")} />
              {f.dkim_enabled && (
                <div className="mt-4 grid gap-4 sm:grid-cols-2">
                  <Field label={t("mail.dkim_domain")}><Input value={f.dkim_domain} onChange={(e) => set({ dkim_domain: e.target.value })} placeholder="example.com" /></Field>
                  <Field label={t("mail.dkim_selector")}><Input value={f.dkim_selector} onChange={(e) => set({ dkim_selector: e.target.value })} placeholder="mmn" /></Field>
                  <Field label={t("mail.dkim_key")} className="sm:col-span-2" hint={f.dkim_key_set ? t("common.secret_keep") : t("mail.dkim_key_hint")}>
                    <Textarea rows={4} value={f.dkim_private_key} onChange={(e) => set({ dkim_private_key: e.target.value })} placeholder="-----BEGIN PRIVATE KEY-----" />
                  </Field>
                </div>
              )}
            </div>
            <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
          </CardBody>
        </Card>
        <Card className="h-fit">
          <CardHeader icon={<Send />} title={t("mail.test_title")} description={t("mail.test_hint")} />
          <CardBody className="space-y-3">
            <Field label={t("mail.test_to")}><Input type="email" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
            <Button className="w-full" loading={test.isPending} disabled={!to} onClick={() => test.mutate()}><Send /> {t("mail.send_test")}</Button>
          </CardBody>
        </Card>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ notifications */
export function NotificationsPage() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["notifications"], queryFn: () => get<Record<string, string>>("api/settings/notifications") });
  const [f, setF] = useState<Record<string, string>>({});
  useEffect(() => {
    if (q.data) setF(q.data);
  }, [q.data]);
  const save = useMutation({ mutationFn: () => put("api/settings/notifications", f), onSuccess: () => toast.success(t("common.saved")) });
  return (
    <>
      <PageHeader title={t("notifications.title")} description={t("notifications.subtitle")} />
      <Card className="max-w-2xl">
        <CardHeader icon={<Bell />} title={t("notifications.who")} />
        <CardBody className="space-y-4">
          {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
          {["backup_failure_to", "backup_success_to", "update_to"].map((k) => (
            <Field key={k} label={t(`notifications.${k}`)} hint={t(`notifications.${k}_hint`)}>
              <Input type="email" value={f[k] ?? ""} onChange={(e) => setF({ ...f, [k]: e.target.value })} placeholder="admin@example.com" />
            </Field>
          ))}
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
        </CardBody>
      </Card>
    </>
  );
}

/* ------------------------------------------------------------------ general */
export function GeneralPage() {
  const { t } = useTranslation();
  const { refresh } = useAuth();
  const q = useQuery({ queryKey: ["general"], queryFn: () => get<{ app_name: string; base_url: string; default_language: string }>("api/settings/general") });
  const [f, setF] = useState({ app_name: "", base_url: "", default_language: "en" });
  useEffect(() => {
    if (q.data) setF(q.data);
  }, [q.data]);
  const save = useMutation({ mutationFn: () => put("api/settings/general", f), onSuccess: async () => { await refresh(); toast.success(t("common.saved")); } });
  return (
    <>
      <PageHeader title={t("general.title")} description={t("general.subtitle")} />
      <Card className="max-w-2xl">
        <CardHeader icon={<SettingsIcon />} title={t("general.app")} />
        <CardBody className="space-y-4">
          {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
          <Field label={t("general.app_name")}><Input value={f.app_name} onChange={(e) => setF({ ...f, app_name: e.target.value })} /></Field>
          <Field label={t("general.base_url")} hint={t("general.base_url_hint")}><Input className="mono" value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} placeholder="https://mcp.example.com" /></Field>
          <Field label={t("general.default_language")}>
            <Select value={f.default_language} onChange={(e) => setF({ ...f, default_language: e.target.value })}>
              <option value="en">English</option>
              <option value="nl">Nederlands</option>
            </Select>
          </Field>
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
        </CardBody>
      </Card>
    </>
  );
}

/* ------------------------------------------------------------------ web server / Zoraxy */
export function WebServerPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["proxy"], queryFn: () => get<ProxySettings>("api/system/proxy"), refetchInterval: (s) => (["queued", "running"].includes(s.state.data?.apply_status.state ?? "") ? 1500 : false) });
  const [f, setF] = useState<ProxySettings | null>(null);
  const [proxies, setProxies] = useState("");
  useEffect(() => {
    if (q.data && !f) {
      setF(q.data);
      setProxies(q.data.trusted_proxies.join("\n"));
    }
  }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => {
      const { apply_available: _a, apply_status: _b, seen: _c, ...rest } = f!;
      void _a; void _b; void _c;
      return put<{ queued: boolean }>("api/system/proxy", { ...rest, trusted_proxies: proxies.split(/[\s,]+/).filter(Boolean) });
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["proxy"] });
      toast.success(r.queued ? t("web.applying") : t("common.saved"));
    },
  });
  if (!f || !q.data) return <Spinner />;
  const set = (p: Partial<ProxySettings>) => setF({ ...f, ...p });
  const seen = q.data.seen;
  const zoraxyIp = seen.client_ip;
  return (
    <>
      <PageHeader title={t("web.title")} description={t("web.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader icon={<Globe />} title={t("web.mode")} />
          <CardBody className="space-y-5">
            {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
            {q.data.apply_status.state === "failed" && <Alert tone="offline" title={t("web.apply_failed")}>{q.data.apply_status.error}</Alert>}
            <Segmented value={f.mode} onChange={(mode) => set({ mode })} options={[
              { value: "proxy", label: t("web.mode_proxy") },
              { value: "direct", label: t("web.mode_direct") },
            ]} />
            <p className="text-sm text-muted">{f.mode === "proxy" ? t("web.mode_proxy_hint") : t("web.mode_direct_hint")}</p>
            {f.mode === "proxy" ? (
              <>
                <Field label={t("web.trusted_proxies")} hint={t("web.trusted_proxies_hint")}>
                  <Textarea rows={3} value={proxies} onChange={(e) => setProxies(e.target.value)} placeholder="10.10.4.2" />
                </Field>
                <Switch checked={f.restrict_to_proxy} onCheckedChange={(v) => set({ restrict_to_proxy: v })} label={t("web.restrict")} description={t("web.restrict_hint")} />
                <Field label={t("web.http_port")}><Input type="number" className="max-w-40" value={f.http_port} onChange={(e) => set({ http_port: Number(e.target.value) })} /></Field>
              </>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label={t("web.https_port")}><Input type="number" value={f.https_port} onChange={(e) => set({ https_port: Number(e.target.value) })} /></Field>
                <Field label={t("web.http_port")}><Input type="number" value={f.http_port} onChange={(e) => set({ http_port: Number(e.target.value) })} /></Field>
                <Field label={t("web.cert")} className="sm:col-span-2"><Input className="mono" value={f.ssl_certificate} onChange={(e) => set({ ssl_certificate: e.target.value })} /></Field>
                <Field label={t("web.key")} className="sm:col-span-2" hint={t("web.cert_hint")}><Input className="mono" value={f.ssl_certificate_key} onChange={(e) => set({ ssl_certificate_key: e.target.value })} /></Field>
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label={t("web.server_name")} hint={t("web.server_name_hint")}><Input className="mono" value={f.server_name} onChange={(e) => set({ server_name: e.target.value })} /></Field>
              <Field label={t("web.base_path")} hint={t("web.base_path_hint")}><Input className="mono" value={f.base_path} onChange={(e) => set({ base_path: e.target.value })} /></Field>
            </div>
            {!q.data.apply_available && <Alert tone="info">{t("web.dev_note")}</Alert>}
            <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("web.save_apply")}</Button>
          </CardBody>
        </Card>
        <div className="space-y-6">
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("web.diagnostics")} description={t("web.diagnostics_hint")} />
            <CardBody className="space-y-2 text-sm">
              {[
                [t("web.seen_ip"), seen.client_ip],
                [t("web.seen_scheme"), seen.scheme],
                ["Host", seen.host],
                ["X-Forwarded-For", seen.forwarded_for ?? "–"],
                ["X-Forwarded-Proto", seen.forwarded_proto ?? "–"],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3"><span className="text-muted">{k}</span><span className="mono text-[13px]">{v}</span></div>
              ))}
              {f.mode === "proxy" && seen.scheme !== "https" && <Alert tone="warning" className="mt-3">{t("web.not_https")}</Alert>}
              {f.mode === "proxy" && seen.scheme === "https" && <Badge tone="online" dot>{t("web.proxy_ok")}</Badge>}
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={t("web.zoraxy_title")} />
            <CardBody>
              <ol className="list-decimal space-y-1.5 pl-5 text-sm">
                <li>{t("web.zoraxy_1")}</li>
                <li>{t("web.zoraxy_2", { ip: zoraxyIp })}</li>
                <li>{t("web.zoraxy_3")}</li>
                <li>{t("web.zoraxy_4")}</li>
                <li>{t("web.zoraxy_5")}</li>
              </ol>
              <div className="mt-3"><CopyButton value={window.location.host} label={t("web.copy_host")} /></div>
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}
