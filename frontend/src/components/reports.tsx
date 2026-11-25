import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Download, ExternalLink, FileText, Mail, Play, Send, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, Spinner } from "@/components/ui/misc";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { apiUrl, del, errorMessage, get, post, put } from "@/lib/api";
import type { Customer, CustomerReportRow, CustomerReports, ReportSettings } from "@/lib/types";
import { formatDate } from "@/lib/utils";

const statusTone = (s: string | null) => (s === "ok" ? "online" : s === "warning" ? "warning" : s === "critical" ? "offline" : "neutral") as "online" | "warning" | "offline" | "neutral";

function periodLabel(period: string, lang: string) {
  const [y, m] = period.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(lang === "nl" ? "nl-NL" : "en-GB", { month: "long", year: "numeric" });
}

/** The Reports tab of a customer: monthly report settings, generate on demand, the list of reports. */
export function ReportsTab({ customer, admin }: { customer: Customer; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const key = ["customer", customer.id, "reports"];
  const q = useQuery({ queryKey: key, queryFn: () => get<CustomerReports>(`api/customers/${customer.id}/reports`) });
  const [period, setPeriod] = useState("");
  const [sendTo, setSendTo] = useState<CustomerReportRow | null>(null);
  useEffect(() => {
    if (q.data && !period) setPeriod(q.data.previous_period);
  }, [q.data, period]);
  const gen = useMutation({
    mutationFn: (send: boolean) => post<CustomerReportRow>(`api/customers/${customer.id}/reports`, { period, send }),
    onSuccess: (_r, send) => { qc.invalidateQueries({ queryKey: key }); toast.success(send ? t("rep.generated_sent") : t("rep.generated")); },
    onError: (e) => { qc.invalidateQueries({ queryKey: key }); toast.error(errorMessage(e, t)); },
  });
  const remove = useMutation({
    mutationFn: (id: string) => del(`api/customers/${customer.id}/reports/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading) return <Spinner />;
  if (!q.data) return <Alert tone="offline">{t("rep.load_failed")}</Alert>;
  const { settings, reports } = q.data;
  const base = `api/customers/${customer.id}/reports`;
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="min-w-0 space-y-6">
        {admin && (
          <Card>
            <CardHeader icon={<Play />} title={t("rep.make_title")} description={t("rep.make_desc")} />
            <CardBody className="flex flex-wrap items-end gap-3">
              <Field label={t("rep.period")} htmlFor="rep-period"><Input id="rep-period" type="month" value={period} onChange={(e) => setPeriod(e.target.value)} className="w-48" max={q.data.current_period} /></Field>
              <Button variant="primary" onClick={() => gen.mutate(false)} disabled={!period || gen.isPending} loading={gen.isPending && !gen.variables}><FileText /> {t("rep.generate")}</Button>
              <Button onClick={() => gen.mutate(true)} disabled={!period || gen.isPending} loading={gen.isPending && !!gen.variables}><Send /> {t("rep.generate_send")}</Button>
            </CardBody>
          </Card>
        )}
        <Card className="min-w-0">
          <CardHeader icon={<FileText />} title={t("rep.list_title")} description={t("rep.list_desc")} />
          {!reports.length ? (
            <EmptyState icon={<FileText />} title={t("rep.empty")}>{t("rep.empty_text")}</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <thead><tr><Th>{t("rep.period")}</Th><Th>{t("rep.status")}</Th><Th>{t("rep.highlights")}</Th><Th>{t("rep.sent")}</Th><Th /></tr></thead>
                <tbody>
                  {reports.map((r) => (
                    <Tr key={r.id}>
                      <Td>
                        <div className="whitespace-nowrap font-bold capitalize">{periodLabel(r.period, r.lang)}</div>
                        <div className="whitespace-nowrap text-xs text-muted">{formatDate(r.created_at, i18n.language)}</div>
                        <div className="text-xs text-muted">{t(`rep.trigger_${r.trigger}`)} · {r.lang.toUpperCase()}</div>
                      </Td>
                      <Td><Badge tone={statusTone(r.status)} dot className="whitespace-nowrap">{t(`rep.st_${r.status ?? "ok"}`)}</Badge></Td>
                      <Td className="text-xs text-muted">
                        {r.summary && <>
                          {r.summary.availability != null && <div>{t("rep.h_availability", { pct: r.summary.availability.toFixed(2) })}</div>}
                          <div>{t("rep.h_line", { incidents: r.summary.incidents, updates: r.summary.updates_installed, security: r.summary.security })}</div>
                        </>}
                      </Td>
                      <Td className="text-xs">
                        {r.sent_at ? <><div>{formatDate(r.sent_at, i18n.language)}</div><div className="text-muted">{r.sent_to.join(", ")}</div></>
                          : r.send_error ? <Badge tone="offline" className="max-w-56 truncate">{r.send_error}</Badge> : <span className="text-muted">–</span>}
                      </Td>
                      <Td className="whitespace-nowrap text-right">
                        <a href={apiUrl(`${base}/${r.id}/html`)} target="_blank" rel="noreferrer"><Button size="sm" variant="ghost" title={t("rep.view")}><ExternalLink /> {t("rep.view")}</Button></a>
                        {r.has_pdf && <a href={apiUrl(`${base}/${r.id}/pdf`)}><Button size="sm" variant="ghost" title={t("rep.pdf")}><Download /> PDF</Button></a>}
                        {admin && <Button size="sm" variant="ghost" onClick={() => setSendTo(r)} title={t("rep.send")}><Mail /></Button>}
                        {admin && <Button size="sm" variant="ghost" onClick={() => remove.mutate(r.id)} title={t("common.delete")} aria-label={t("common.delete")}><Trash2 /></Button>}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </div>
          )}
        </Card>
      </div>
      <SettingsCard customer={customer} settings={settings} admin={admin} />
      <SendDialog customer={customer} report={sendTo} defaults={settings.recipients.length ? settings.recipients : settings.default_recipients ?? []} onClose={() => setSendTo(null)} />
    </div>
  );
}

function SettingsCard({ customer, settings, admin }: { customer: Customer; settings: ReportSettings; admin: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [s, setS] = useState(settings);
  const [rcpt, setRcpt] = useState(settings.recipients.join("\n"));
  useEffect(() => { setS(settings); setRcpt(settings.recipients.join("\n")); }, [settings]);
  const save = useMutation({
    mutationFn: () => put<ReportSettings>(`api/customers/${customer.id}/reports/settings`, {
      enabled: s.enabled, day: s.day, lang: s.lang, include_ai: s.include_ai, include_costs: !!s.include_costs,
      recipients: rcpt.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean),
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["customer", customer.id, "reports"] }); toast.success(t("common.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Card className="self-start">
      <CardHeader icon={<CalendarClock />} title={t("rep.settings")} description={t("rep.settings_desc")} />
      <CardBody className="space-y-4">
        <Switch checked={s.enabled} onCheckedChange={(v) => setS({ ...s, enabled: v })} disabled={!admin} label={t("rep.monthly")} description={t("rep.monthly_desc", { day: s.day })} />
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("rep.day")} htmlFor="rep-day">
            <Select id="rep-day" value={s.day} onChange={(e) => setS({ ...s, day: Number(e.target.value) })} disabled={!admin}>
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => <option key={d} value={d}>{d}</option>)}
            </Select>
          </Field>
          <Field label={t("rep.lang")} htmlFor="rep-lang">
            <Select id="rep-lang" value={s.lang} onChange={(e) => setS({ ...s, lang: e.target.value as "en" | "nl" })} disabled={!admin}>
              <option value="en">English</option><option value="nl">Nederlands</option>
            </Select>
          </Field>
        </div>
        <Switch checked={!!s.include_costs} onCheckedChange={(v) => setS({ ...s, include_costs: v })} disabled={!admin} label={t("rep.include_costs")} description={t("rep.include_costs_desc")} />
        <Field label={t("rep.recipients")} htmlFor="rep-rcpt" hint={settings.default_recipients?.length && !rcpt.trim() ? t("rep.recipients_default", { email: settings.default_recipients.join(", ") }) : t("rep.recipients_hint")}>
          <Textarea id="rep-rcpt" value={rcpt} onChange={(e) => setRcpt(e.target.value)} disabled={!admin} className="min-h-20" placeholder="it@customer.nl" />
        </Field>
        {admin && <Button variant="dark" className="w-full justify-center" onClick={() => save.mutate()} loading={save.isPending}>{t("common.save")}</Button>}
      </CardBody>
    </Card>
  );
}

function SendDialog({ customer, report, defaults, onClose }: { customer: Customer; report: CustomerReportRow | null; defaults: string[]; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [to, setTo] = useState("");
  useEffect(() => { if (report) setTo(defaults.join("\n")); }, [report, defaults]);
  const send = useMutation({
    mutationFn: () => post<{ sent_to: string[] }>(`api/customers/${customer.id}/reports/${report?.id}/send`, {
      to: to.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean),
    }),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["customer", customer.id, "reports"] }); toast.success(t("rep.sent_to", { to: r.sent_to.join(", ") })); onClose(); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <Dialog open={!!report} onOpenChange={(v) => !v && onClose()} title={t("rep.send_title")} description={report ? periodLabel(report.period, i18n.language) : undefined}
      footer={<><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={() => send.mutate()} loading={send.isPending} disabled={!to.trim()}><Send /> {t("rep.send")}</Button></>}>
      <Field label={t("rep.recipients")} hint={t("rep.send_hint")}><Textarea value={to} onChange={(e) => setTo(e.target.value)} className="min-h-20" /></Field>
    </Dialog>
  );
}
