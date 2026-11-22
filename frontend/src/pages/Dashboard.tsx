import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, Boxes, CheckCircle2, DatabaseBackup, Mail, Monitor, Rocket, ShieldCheck, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Badge, PageHeader, Stat } from "@/components/ui/misc";
import { get } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { AuditEntry, BackupRun, Connection, MailSettings, Overview, SystemInfo } from "@/lib/types";
import { StatusDot } from "@/components/proxmox";
import { formatBytes, timeAgo } from "@/lib/utils";

export function Dashboard() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => get<SystemInfo>("api/system/info") });
  const runs = useQuery({ queryKey: ["backup-runs", "dash"], queryFn: () => get<BackupRun[]>("api/backups/runs?limit=20"), enabled: admin });
  const mail = useQuery({ queryKey: ["mail"], queryFn: () => get<MailSettings>("api/settings/mail"), enabled: admin });
  const conns = useQuery({ queryKey: ["connections"], queryFn: () => get<Connection[]>("api/tokens/connections") });
  const ov = useQuery({ queryKey: ["overview", "all"], queryFn: () => get<Overview>("api/proxmox/overview"), refetchInterval: 30_000 });
  const audit = useQuery({ queryKey: ["audit", "dash"], queryFn: () => get<AuditEntry[]>("api/settings/audit?limit=8"), enabled: admin });

  const lastBackup = runs.data?.find((r) => r.kind !== "restore" && r.status !== "running");
  const lastOk = runs.data?.find((r) => r.kind !== "restore" && r.status === "success");
  const greeting = new Date().getHours() < 12 ? t("dashboard.morning") : new Date().getHours() < 18 ? t("dashboard.afternoon") : t("dashboard.evening");

  return (
    <>
      <PageHeader title={`${greeting}, ${user?.full_name?.split(" ")[0] || user?.username}`} description={t("dashboard.subtitle")} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label={t("dashboard.version")}
          value={<span className="mono text-[22px]">{info.data?.tag ?? "…"}</span>}
          hint={info.data?.update_available ? t("info.update_available") : info.data?.channel === "test" ? t("info.channel_test") : t("info.up_to_date")}
          tone={info.data?.update_available ? "accent" : "online"}
          icon={<Rocket />}
        />
        {admin && (
          <Stat
            label={t("dashboard.backups")}
            value={lastOk ? timeAgo(lastOk.finished_at, i18n.language) : t("dashboard.never")}
            hint={lastBackup ? (lastBackup.status === "success" ? t("dashboard.last_backup_ok") : t("dashboard.last_backup_failed")) : t("dashboard.no_target")}
            tone={!lastBackup ? "warning" : lastBackup.status === "success" ? "online" : "offline"}
            icon={<DatabaseBackup />}
          />
        )}
        {admin && (
          <Stat
            label={t("dashboard.mail")}
            value={mail.data?.enabled ? t("common.on") : t("common.off")}
            hint={mail.data?.enabled ? `${mail.data.host}:${mail.data.port} · ${mail.data.security.toUpperCase()}` : t("dashboard.mail_off")}
            tone={mail.data?.enabled ? "online" : "warning"}
            icon={<Mail />}
          />
        )}
        <Stat
          label={t("dashboard.ai")}
          value={conns.data?.length ?? 0}
          hint={conns.data?.length ? conns.data.map((c) => c.client_name).join(", ") : t("dashboard.ai_none")}
          tone="info"
          icon={<Bot />}
        />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <div className="grid gap-4 md:grid-cols-2">
            <Link to="/proxmox">
              <Card className="relative h-full overflow-hidden transition-shadow hover:shadow-lg">
                <CardBody className="py-5">
                  <div className="flex items-center gap-2"><Boxes className="size-5 text-koraal" /><span className="font-bold">{t("nav.section.proxmox")}</span>
                    {ov.data && <Badge tone={ov.data.totals.online < ov.data.totals.nodes ? "offline" : "online"}>{ov.data.totals.online}/{ov.data.totals.nodes} {t("px.online").toLowerCase()}</Badge>}
                    {!!ov.data?.totals.updates && <Badge tone="accent">{t("px.updates_count", { count: ov.data.totals.updates })}</Badge>}
                  </div>
                  {ov.data?.nodes.length ? (
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-sm">
                      {ov.data.nodes.slice(0, 8).map((n) => <span key={n.id} className="flex items-center gap-1.5"><StatusDot online={n.online} /> {n.name}</span>)}
                    </div>
                  ) : <p className="mt-2 text-sm text-muted">{t("dashboard.proxmox_empty")}</p>}
                </CardBody>
              </Card>
            </Link>
            <Card className="relative overflow-hidden">
              <CardBody className="py-5">
                <div className="flex items-center gap-2"><Monitor className="size-5 text-koraal" /><span className="font-bold">{t("nav.section.servers")}</span><Badge>{t("nav.soon")}</Badge></div>
                <p className="mt-2 text-sm text-muted">{t("dashboard.servers_soon")}</p>
              </CardBody>
            </Card>
          </div>

          {admin && (
            <Card>
              <CardHeader icon={<DatabaseBackup />} title={t("dashboard.recent_backups")} action={<Link to="/admin/backups" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
              <div className="divide-y divide-line">
                {(runs.data ?? []).slice(0, 5).map((r) => (
                  <div key={r.id} className="flex items-center gap-3 px-5 py-3 text-sm">
                    {r.status === "success" ? <CheckCircle2 className="size-4 text-online" /> : r.status === "failed" ? <XCircle className="size-4 text-offline" /> : <span className="size-4 animate-pulse rounded-full bg-warning" />}
                    <span className="font-semibold">{t(`backups.kind_${r.kind}`)}</span>
                    <span className="text-muted">{r.target_name ?? "–"}</span>
                    <span className="ml-auto text-muted">{formatBytes(r.size_bytes)}</span>
                    <span className="w-28 text-right text-muted">{timeAgo(r.started_at, i18n.language)}</span>
                  </div>
                ))}
                {!runs.data?.length && (
                  <div className="px-5 py-6 text-sm text-muted">
                    {t("dashboard.no_backups")} <Link to="/admin/backups" className="font-semibold text-accent-text hover:underline">{t("dashboard.setup_backups")}</Link>
                  </div>
                )}
              </div>
            </Card>
          )}
        </div>

        <div className="space-y-6">
          <Card className="bg-nacht text-white">
            <CardBody className="py-5">
              <div className="flex items-center gap-2 text-lucht"><Bot className="size-5" /><span className="text-sm font-bold uppercase tracking-wider">MCP</span></div>
              <div className="display mt-3 text-xl">{t("dashboard.mcp_title")}</div>
              <p className="mt-2 text-sm text-white/75">{t("dashboard.mcp_text")}</p>
              <Link to="/help/mcp" className="mt-4 inline-flex items-center gap-2 rounded-md bg-koraal px-4 py-2 text-sm font-semibold text-nacht hover:brightness-95">
                {t("dashboard.mcp_cta")} <ArrowRight className="size-4" />
              </Link>
            </CardBody>
          </Card>
          {admin && (
            <Card>
              <CardHeader icon={<ShieldCheck />} title={t("dashboard.activity")} action={<Link to="/admin/audit" className="text-sm font-semibold text-accent-text hover:underline">{t("common.view_all")}</Link>} />
              <div className="divide-y divide-line">
                {(audit.data ?? []).map((a) => (
                  <div key={a.id} className="px-5 py-2.5 text-sm">
                    <div className="flex items-center gap-2">
                      <span className="mono text-[12.5px]">{a.action}</span>
                      {a.source === "mcp" && <Badge tone="info">{a.detail.client?.name || "AI"}</Badge>}
                      {!a.success && <Badge tone="offline">{t("common.failed")}</Badge>}
                    </div>
                    <div className="text-xs text-muted">{a.username ?? t("audit.system")} · {timeAgo(a.at, i18n.language)}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
