import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Download, ExternalLink, FlaskConical, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { errorMessage, get, post, put } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SystemInfo, UpdateCheck, UpdateStatus } from "@/lib/types";
import { formatDate, formatDuration } from "@/lib/utils";
import { Button } from "./ui/button";
import { Dialog } from "./ui/dialog";
import { Segmented } from "./ui/form";
import { Alert, Badge, Spinner } from "./ui/misc";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className="mono text-right text-[13px]">{value}</span>
    </div>
  );
}

export function InfoDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const qc = useQueryClient();
  const isAdmin = user?.role === "admin";
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => get<SystemInfo>("api/system/info"), enabled: open });
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [confirming, setConfirming] = useState(false);
  const status = useQuery({
    queryKey: ["update-status"],
    queryFn: () => get<UpdateStatus>("api/system/updates/status"),
    enabled: open,
    refetchInterval: (q) => (["queued", "running"].includes(q.state.data?.state ?? "") ? 1500 : false),
  });
  const running = ["queued", "running"].includes(status.data?.state ?? "");

  const doCheck = useMutation({
    mutationFn: () => post<UpdateCheck>("api/system/updates/check"),
    onSuccess: (r) => {
      setCheck(r);
      qc.invalidateQueries({ queryKey: ["system-info"] });
    },
    onError: (e) => toast.error(t("info.check_failed"), { description: errorMessage(e, t) }),
  });
  const setChannel = useMutation({
    mutationFn: (channel: string) => put<UpdateCheck & { error?: string }>("api/system/updates/channel", { channel }),
    onSuccess: (r) => {
      if (r.latest !== undefined) setCheck(r);
      qc.invalidateQueries({ queryKey: ["system-info"] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const apply = useMutation({
    mutationFn: (tag: string) => post<UpdateStatus>("api/system/updates/apply", { tag }),
    onSuccess: () => {
      setConfirming(false);
      qc.invalidateQueries({ queryKey: ["update-status"] });
    },
    onError: (e) => toast.error(t("info.install_failed"), { description: errorMessage(e, t) }),
  });

  // When the update finished, the server restarted: reload to load the new interface.
  useEffect(() => {
    if (status.data?.state === "success" && status.data.tag && info.data && status.data.tag !== info.data.tag) {
      const id = window.setTimeout(() => window.location.reload(), 2500);
      return () => window.clearTimeout(id);
    }
  }, [status.data, info.data]);

  const d = info.data;
  const latest = check?.latest ?? d?.latest ?? null;
  const updateAvailable = check ? check.update_available : d?.update_available;

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("info.title")} description={t("info.subtitle")} wide>
      {!d ? (
        <Spinner />
      ) : (
        <div className="grid gap-6 md:grid-cols-2">
          <div>
            <div className="flex items-center gap-3">
              <img src="brand/rhis-icon.svg" alt="" className="size-12 rounded-[12px]" />
              <div>
                <div className="display text-xl">{d.name}</div>
                <div className="flex items-center gap-2">
                  <span className="mono text-sm">{d.tag}</span>
                  <Badge tone={d.channel === "test" ? "warning" : "online"}>{d.channel === "test" ? t("info.channel_test") : t("info.channel_production")}</Badge>
                  {d.dev && <Badge>dev</Badge>}
                </div>
              </div>
            </div>
            <div className="mt-4 divide-y divide-line rounded-md border border-line px-4">
              <Row label={t("info.commit")} value={d.commit.slice(0, 10)} />
              <Row label={t("info.built")} value={d.built_at ? formatDate(d.built_at, i18n.language) : "–"} />
              <Row label={t("info.uptime")} value={formatDuration(d.uptime_seconds)} />
              <Row label="Python" value={d.python} />
              <Row label="PostgreSQL" value={d.postgres.split(" ")[0]} />
              <Row label={t("info.schema")} value={d.db_revision ?? "–"} />
              <Row label={t("info.os")} value={d.os} />
              <Row label={t("info.repo")} value={d.repo} />
            </div>
          </div>

          <div className="space-y-4">
            <div>
              <div className="mb-2 text-[13px] font-semibold">{t("info.channel")}</div>
              <Segmented
                value={d.channel}
                disabled={!isAdmin || setChannel.isPending || running}
                onChange={(v) => setChannel.mutate(v)}
                options={[
                  { value: "production", label: <span className="inline-flex items-center gap-1.5"><ShieldCheck className="size-3.5" />{t("info.channel_production")}</span> },
                  { value: "test", label: <span className="inline-flex items-center gap-1.5"><FlaskConical className="size-3.5" />{t("info.channel_test")}</span> },
                ]}
              />
              <p className="mt-2 text-xs text-muted">{d.channel === "test" ? t("info.channel_test_hint") : t("info.channel_production_hint")}</p>
            </div>

            <div className="rounded-md border border-line p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold">{updateAvailable ? t("info.update_available") : t("info.up_to_date")}</div>
                  <div className="text-xs text-muted">
                    {t("info.last_check")}: {formatDate(check?.checked_at ?? d.last_check, i18n.language)}
                  </div>
                </div>
                <Button size="sm" onClick={() => doCheck.mutate()} loading={doCheck.isPending} disabled={!isAdmin}>
                  <RefreshCw /> {t("info.check")}
                </Button>
              </div>
              {latest && (
                <div className="mt-3 rounded-md bg-sunken p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-semibold">{latest.name}</div>
                    <a href={latest.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-text hover:underline">
                      GitHub <ExternalLink className="size-3" />
                    </a>
                  </div>
                  <div className="text-xs text-muted">{formatDate(latest.published_at, i18n.language)}</div>
                  {latest.notes && <pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-ink">{latest.notes}</pre>}
                </div>
              )}
              {check?.is_downgrade && <Alert tone="warning" className="mt-3">{t("info.downgrade_warning")}</Alert>}
              {isAdmin && latest && (updateAvailable || check?.is_downgrade) && !running && (
                <div className="mt-3">
                  {!d.updater_available ? (
                    <Alert tone="info">{t("info.updater_unavailable")}</Alert>
                  ) : confirming ? (
                    <div className="space-y-2">
                      <Alert tone="warning">{t("info.install_confirm", { tag: latest.tag })}</Alert>
                      <div className="flex gap-2">
                        <Button variant="primary" onClick={() => apply.mutate(latest.tag)} loading={apply.isPending}>
                          <Download /> {t("info.install_now")}
                        </Button>
                        <Button variant="ghost" onClick={() => setConfirming(false)}>{t("common.cancel")}</Button>
                      </div>
                    </div>
                  ) : (
                    <Button variant="primary" onClick={() => setConfirming(true)}>
                      <Download /> {t("info.install", { tag: latest.tag })}
                    </Button>
                  )}
                </div>
              )}
            </div>

            {status.data && status.data.state !== "idle" && (
              <div className="rounded-md border border-line p-4">
                <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
                  {running ? <Spinner className="size-4" /> : status.data.state === "success" ? <CheckCircle2 className="size-4 text-online" /> : null}
                  {t(`info.state_${status.data.state}`)} {status.data.tag && <span className="mono text-xs">{status.data.tag}</span>}
                </div>
                <ol className="space-y-1 text-xs text-muted">
                  {(status.data.steps ?? []).map((s, i) => (
                    <li key={i}>• {s.msg}</li>
                  ))}
                </ol>
                {status.data.error && <Alert tone="offline" className="mt-2">{status.data.error}</Alert>}
              </div>
            )}
          </div>
        </div>
      )}
    </Dialog>
  );
}
