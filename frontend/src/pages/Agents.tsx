import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Network, Plug, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { toast } from "sonner";
import { CopyButton } from "@/components/CopyButton";
import { AgentAddressNotice } from "@/components/proxmox";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Field, Input, Switch } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { del, errorMessage, get, put } from "@/lib/api";
import type { ConnectInfo, Pairing } from "@/lib/types";
import { timeAgo } from "@/lib/utils";

const b = <strong className="font-semibold" />;
const c = <code className="mono rounded bg-sunken px-1 py-0.5 text-[12.5px]" />;

export function AgentsPage() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["agents-connect"], queryFn: () => get<ConnectInfo>("api/agents/connect") });
  const codes = useQuery({ queryKey: ["pairing"], queryFn: () => get<Pairing[]>("api/pairing") });
  const [f, setF] = useState({ address: "", port: 8443, auto_update: true, updates_check_hours: 6 });
  useEffect(() => {
    if (info.data) setF({ address: info.data.address, port: info.data.port, auto_update: info.data.auto_update, updates_check_hours: info.data.updates_check_hours });
  }, [info.data]);
  const save = useMutation({
    mutationFn: () => put<ConnectInfo & { nginx_queued: boolean }>("api/agents/settings", f),
    onSuccess: (r) => {
      toast.success(t("common.saved"), { description: r.nginx_queued ? t("agents.nginx_queued") : undefined });
      qc.invalidateQueries({ queryKey: ["agents-connect"] });
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => del(`api/pairing/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["pairing"] }) });
  if (info.isLoading) return <Spinner />;
  const d = info.data;
  return (
    <>
      <PageHeader title={t("agents.title")} description={t("agents.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card>
            <CardHeader icon={<Plug />} title={t("agents.endpoint_title")} description={t("agents.endpoint_desc")} />
            <CardBody className="space-y-4">
              {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
              {d && !d.packages_available && <Alert tone="warning">{t("px.no_packages")}</Alert>}
              {d && <AgentAddressNotice info={d} here />}
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md bg-sunken px-4 py-3">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("agents.agents_connect_to")}</div>
                  <div className="mono text-lg font-bold">{d?.endpoint}</div>
                </div>
                <CopyButton value={d?.endpoint ?? ""} />
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label={t("agents.address")} hint={t("agents.address_hint")} className="sm:col-span-2">
                  <Input value={f.address} onChange={(e) => setF({ ...f, address: e.target.value })} placeholder="mmn.example.nl" />
                </Field>
                <Field label={t("agents.port")} hint={t("agents.port_hint")}>
                  <Input type="number" value={f.port} onChange={(e) => setF({ ...f, port: Number(e.target.value) })} />
                </Field>
              </div>
              <Switch checked={f.auto_update} onCheckedChange={(v) => setF({ ...f, auto_update: v })} label={t("agents.auto_update")} description={t("agents.auto_update_hint", { version: d?.agent_version ?? "?" })} />
              <Field label={t("agents.check_hours")} hint={t("agents.check_hours_hint")} className="max-w-xs">
                <Input type="number" min={1} max={168} value={f.updates_check_hours} onChange={(e) => setF({ ...f, updates_check_hours: Number(e.target.value) })} />
              </Field>
              <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<KeyRound />} title={t("agents.codes_title")} description={t("agents.codes_desc")} />
            {!codes.data?.length ? <EmptyState icon={<KeyRound />} title={t("agents.codes_empty")} /> : (
              <Table>
                <thead><tr><Th>{t("px.pair_code")}</Th><Th>{t("px.environment")}</Th><Th>{t("px.pair_label")}</Th><Th>{t("agents.expires")}</Th><Th /></tr></thead>
                <tbody>
                  {codes.data.map((p) => (
                    <Tr key={p.id}>
                      <Td className="mono font-semibold">{p.code}</Td>
                      <Td>{p.environment_name}</Td>
                      <Td>{p.label ?? "–"}</Td>
                      <Td className="text-muted">{timeAgo(p.expires_at, i18n.language)}</Td>
                      <Td className="text-right"><Button size="sm" variant="ghost" onClick={() => revoke.mutate(p.id)} aria-label={t("common.delete")}><Trash2 /></Button></Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </div>
        <div className="space-y-6">
          <Card>
            <CardHeader icon={<Network />} title={t("agents.zoraxy_title")} />
            <CardBody className="space-y-3 text-sm">
              <p><Trans i18nKey="agents.zoraxy_1" components={{ b, c }} values={{ port: d?.port }} /></p>
              <p><Trans i18nKey="agents.zoraxy_2" components={{ b, c }} values={{ port: d?.port }} /></p>
              <p className="text-muted"><Trans i18nKey="agents.zoraxy_3" components={{ b, c }} /></p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("agents.security_title")} />
            <CardBody className="space-y-3 text-sm">
              <p className="text-muted">{t("agents.security_text")}</p>
              <div>
                <div className="text-xs font-semibold text-muted">{t("px.fingerprint")} (SHA-256)</div>
                <div className="mono mt-1 break-all text-[11.5px]">{d?.fingerprint_pretty}</div>
              </div>
              <Badge tone="info">{t("agents.bundled", { version: d?.agent_version ?? "–" })}</Badge>
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}
