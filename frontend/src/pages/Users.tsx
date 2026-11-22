import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2, Users as UsersIcon } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Switch } from "@/components/ui/form";
import { Alert, Badge, PageHeader } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, patch, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { User } from "@/lib/types";
import { timeAgo } from "@/lib/utils";

type FormState = { username: string; full_name: string; email: string; role: "admin" | "viewer"; language: "en" | "nl"; password: string; is_active: boolean };
const empty: FormState = { username: "", full_name: "", email: "", role: "viewer", language: "en", password: "", is_active: true };

export function UsersPage() {
  const { t, i18n } = useTranslation();
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => get<User[]>("api/users") });
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const [form, setForm] = useState<FormState>(empty);

  const open = (u: User | "new") => {
    setEditing(u);
    setForm(u === "new" ? empty : { username: u.username, full_name: u.full_name ?? "", email: u.email ?? "", role: u.role, language: u.language, password: "", is_active: u.is_active });
  };
  const save = useMutation({
    mutationFn: () => {
      const body = { full_name: form.full_name || null, email: form.email || null, role: form.role, language: form.language };
      if (editing === "new") return post("api/users", { ...body, username: form.username, password: form.password });
      return patch(`api/users/${(editing as User).id}`, { ...body, is_active: form.is_active, ...(form.password ? { password: form.password } : {}) });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setEditing(null);
      toast.success(t("common.saved"));
    },
  });
  const remove = useMutation({
    mutationFn: (u: User) => del(`api/users/${u.id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });

  return (
    <>
      <PageHeader title={t("users.title")} description={t("users.subtitle")} actions={<Button variant="primary" onClick={() => open("new")}><Plus /> {t("users.add")}</Button>} />
      <Card>
        <CardHeader icon={<UsersIcon />} title={t("users.count", { count: users.data?.length ?? 0 })} />
        <Table>
          <thead>
            <tr><Th>{t("auth.username")}</Th><Th>{t("users.email")}</Th><Th>{t("users.role")}</Th><Th>{t("users.last_login")}</Th><Th /></tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((u) => (
              <Tr key={u.id}>
                <Td>
                  <div className="font-semibold">{u.full_name || u.username}</div>
                  <div className="mono text-xs text-muted">{u.username}</div>
                </Td>
                <Td className="text-muted">{u.email ?? "–"}</Td>
                <Td>
                  <div className="flex gap-1.5">
                    <Badge tone={u.role === "admin" ? "accent" : "neutral"}>{t(`roles.${u.role}`)}</Badge>
                    {!u.is_active && <Badge tone="offline">{t("users.disabled")}</Badge>}
                  </div>
                </Td>
                <Td className="text-muted">{timeAgo(u.last_login_at, i18n.language)}</Td>
                <Td className="text-right">
                  <div className="flex justify-end gap-1">
                    <Button size="icon" variant="ghost" onClick={() => open(u)} aria-label={t("common.edit")}><Pencil /></Button>
                    {u.id !== me?.id && (
                      <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => { if (window.confirm(t("users.delete_confirm", { name: u.username }))) remove.mutate(u); }}>
                        <Trash2 className="text-offline" />
                      </Button>
                    )}
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </Card>

      <Dialog
        open={editing !== null}
        onOpenChange={(v) => !v && setEditing(null)}
        title={editing === "new" ? t("users.add") : t("users.edit")}
        footer={<><Button variant="ghost" onClick={() => setEditing(null)}>{t("common.cancel")}</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button></>}
      >
        <div className="space-y-4">
          {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
          <Field label={t("auth.username")}>
            <Input value={form.username} disabled={editing !== "new"} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={t("users.full_name")}><Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></Field>
            <Field label={t("users.email")}><Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
            <Field label={t("users.role")} hint={t(`users.role_${form.role}_hint`)}>
              <Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as FormState["role"] })}>
                <option value="viewer">{t("roles.viewer")}</option>
                <option value="admin">{t("roles.admin")}</option>
              </Select>
            </Field>
            <Field label={t("account.language")}>
              <Select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value as FormState["language"] })}>
                <option value="en">English</option>
                <option value="nl">Nederlands</option>
              </Select>
            </Field>
          </div>
          <Field label={editing === "new" ? t("auth.password") : t("users.new_password_optional")} hint={t("auth.password_rules")}>
            <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </Field>
          {editing !== "new" && <Switch checked={form.is_active} onCheckedChange={(v) => setForm({ ...form, is_active: v })} label={t("users.active")} description={t("users.active_hint")} />}
        </div>
      </Dialog>
    </>
  );
}
