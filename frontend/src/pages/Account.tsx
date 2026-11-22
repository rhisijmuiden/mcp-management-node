import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Field, Input, Segmented } from "@/components/ui/form";
import { Alert, PageHeader } from "@/components/ui/misc";
import { errorMessage, patch, post } from "@/lib/api";
import { applyTheme, localStorageSet, useAuth } from "@/lib/auth";
import i18n from "@/i18n";
import { KeyRound, UserRound } from "lucide-react";

export function AccountPage() {
  const { t } = useTranslation();
  const { user, refresh } = useAuth();
  const [profile, setProfile] = useState({ full_name: "", email: "" });
  const [pw, setPw] = useState({ current_password: "", password: "", repeat: "" });
  useEffect(() => {
    if (user) setProfile({ full_name: user.full_name ?? "", email: user.email ?? "" });
  }, [user]);

  const save = useMutation({
    mutationFn: (data: Record<string, unknown>) => patch("api/auth/me", data),
    onSuccess: async () => {
      await refresh();
      toast.success(t("common.saved"));
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const changePw = useMutation({
    mutationFn: () => post("api/auth/change-password", { current_password: pw.current_password, password: pw.password }),
    onSuccess: () => {
      setPw({ current_password: "", password: "", repeat: "" });
      toast.success(t("account.password_changed"));
    },
  });
  if (!user) return null;
  const mismatch = pw.repeat.length > 0 && pw.password !== pw.repeat;

  return (
    <>
      <PageHeader title={t("account.title")} description={t("account.subtitle")} />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader icon={<UserRound />} title={t("account.profile")} />
          <CardBody className="space-y-4">
            <Field label={t("auth.username")}>
              <Input value={user.username} disabled />
            </Field>
            <Field label={t("users.full_name")}>
              <Input value={profile.full_name} onChange={(e) => setProfile({ ...profile, full_name: e.target.value })} />
            </Field>
            <Field label={t("users.email")} hint={t("account.email_hint")}>
              <Input type="email" value={profile.email} onChange={(e) => setProfile({ ...profile, email: e.target.value })} />
            </Field>
            <div className="flex flex-wrap gap-6">
              <Field label={t("account.language")}>
                <Segmented
                  value={user.language}
                  onChange={(v) => {
                    localStorageSet("mmn-lang", v);
                    i18n.changeLanguage(v);
                    save.mutate({ language: v });
                  }}
                  options={[{ value: "en", label: "English" }, { value: "nl", label: "Nederlands" }]}
                />
              </Field>
              <Field label={t("account.theme")}>
                <Segmented
                  value={user.theme}
                  onChange={(v) => {
                    localStorageSet("mmn-theme", v);
                    applyTheme(v);
                    save.mutate({ theme: v });
                  }}
                  options={[
                    { value: "system", label: t("account.theme_system") },
                    { value: "light", label: t("account.theme_light") },
                    { value: "dark", label: t("account.theme_dark") },
                  ]}
                />
              </Field>
            </div>
            <Button variant="primary" loading={save.isPending} onClick={() => save.mutate({ full_name: profile.full_name || null, email: profile.email || null })}>
              {t("common.save")}
            </Button>
          </CardBody>
        </Card>
        <Card>
          <CardHeader icon={<KeyRound />} title={t("account.change_password")} description={t("account.change_password_hint")} />
          <CardBody>
            <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); if (!mismatch) changePw.mutate(); }}>
              {changePw.isError && <Alert tone="offline">{errorMessage(changePw.error, t)}</Alert>}
              <Field label={t("account.current_password")}>
                <Input type="password" autoComplete="current-password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} required />
              </Field>
              <Field label={t("auth.new_password")} hint={t("auth.password_rules")}>
                <Input type="password" autoComplete="new-password" minLength={10} value={pw.password} onChange={(e) => setPw({ ...pw, password: e.target.value })} required />
              </Field>
              <Field label={t("auth.password_repeat")}>
                <Input type="password" autoComplete="new-password" value={pw.repeat} onChange={(e) => setPw({ ...pw, repeat: e.target.value })} required />
              </Field>
              {mismatch && <Alert tone="warning">{t("auth.password_mismatch")}</Alert>}
              <Button loading={changePw.isPending}>{t("account.change_password")}</Button>
            </form>
          </CardBody>
        </Card>
      </div>
    </>
  );
}
