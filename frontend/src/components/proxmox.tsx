import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpCircle, Cpu, KeyRound, RefreshCw, Server, Terminal } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { toast } from "sonner";
import { CodeBlock, CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Alert, Badge } from "@/components/ui/misc";
import { errorMessage, getAll, post, put } from "@/lib/api";
import type { ConnectInfo, Environment, NodeInfo, Pairing } from "@/lib/types";
import { cn, formatBytes, formatDuration, timeAgo } from "@/lib/utils";

export const useEnvironments = () => useQuery({ queryKey: ["environments"], queryFn: () => getAll<Environment[]>("api/environments") });

export const ENV_COLORS: Record<string, string> = {
  lucht: "bg-lucht",
  koraal: "bg-koraal",
  online: "bg-online",
  warning: "bg-warning",
  nacht: "bg-nacht dark:bg-white",
  info: "bg-info",
};

export function EnvDot({ color, className }: { color?: string; className?: string }) {
  return <span className={cn("inline-block size-2.5 shrink-0 rounded-full", ENV_COLORS[color || "lucht"] ?? "bg-lucht", className)} />;
}

export function StatusDot({ online }: { online: boolean }) {
  return (
    <span className="relative inline-flex size-2.5">
      {online && <span className="absolute inline-flex size-full animate-ping rounded-full bg-online opacity-40" />}
      <span className={cn("relative inline-flex size-2.5 rounded-full", online ? "bg-online" : "bg-offline")} />
    </span>
  );
}

export function UsageBar({ label, used, total, percent, detail }: { label: string; used?: number; total?: number; percent?: number; detail?: string }) {
  const pct = percent ?? (total ? ((used ?? 0) / total) * 100 : 0);
  const tone = pct >= 90 ? "bg-offline" : pct >= 75 ? "bg-warning" : "bg-online";
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
        <span className="font-semibold text-muted">{label}</span>
        <span className="mono text-[11.5px] text-ink">{detail ?? `${pct.toFixed(0)}%`}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-sunken">
        <div className={cn("h-full rounded-full transition-[width] duration-500", tone)} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
    </div>
  );
}

export function NodeMetricsBars({ n }: { n: NodeInfo }) {
  const { t } = useTranslation();
  const m = n.metrics || {};
  return (
    <div className="space-y-2.5">
      <UsageBar label={t("px.cpu")} percent={m.cpu_percent ?? 0} detail={m.cpu_percent != null ? `${m.cpu_percent.toFixed(0)}% · ${m.cpus ?? "?"} ${t("px.cores")}` : "–"} />
      <UsageBar label={t("px.memory")} used={m.mem_used} total={m.mem_total} detail={m.mem_total ? `${formatBytes(m.mem_used)} / ${formatBytes(m.mem_total)}` : "–"} />
      <UsageBar label={t("px.disk")} used={m.disk_used} total={m.disk_total} detail={m.disk_total ? `${formatBytes(m.disk_used)} / ${formatBytes(m.disk_total)}` : "–"} />
    </div>
  );
}

export function GovernorSelect({ n, disabled }: { n: NodeInfo; disabled?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const avail = n.governor?.available ?? [];
  const value = n.governor?.desired || n.governor?.current || "";
  const set = useMutation({
    mutationFn: (g: string) => put<{ queued: boolean }>(`api/nodes/${n.id}/governor`, { governor: g }),
    onSuccess: (r) => {
      toast.success(r.queued ? t("px.governor_queued") : t("px.governor_set"));
      qc.invalidateQueries({ queryKey: ["overview"] });
      qc.invalidateQueries({ queryKey: ["node", n.id] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (!n.capabilities.includes("governor") || !avail.length) return <span className="text-xs text-muted">{t("px.governor_na")}</span>;
  return (
    <Select
      value={value}
      disabled={disabled || set.isPending}
      onChange={(e) => set.mutate(e.target.value)}
      className="h-8 w-auto min-w-36 py-0 text-[13px]"
      aria-label={t("px.governor")}
      onClick={(e) => e.stopPropagation()}
    >
      {!avail.includes(value) && <option value={value}>{value || "–"}</option>}
      {avail.map((g) => (
        <option key={g} value={g}>{t(`px.gov_${g}`, { defaultValue: g })}</option>
      ))}
    </Select>
  );
}

export function NodeCard({ n, admin }: { n: NodeInfo; admin: boolean }) {
  const { t, i18n } = useTranslation();
  const g = n.metrics?.guests;
  return (
    <Card className="flex flex-col transition-shadow hover:shadow-lg">
      <Link to={`/proxmox/nodes/${n.id}`} className="flex items-start gap-3 border-b border-line px-5 py-4">
        <div className="grid size-10 shrink-0 place-items-center rounded-md bg-sunken text-ink"><Server className="size-5" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <StatusDot online={n.online} />
            <span className="truncate font-bold">{n.name}</span>
          </div>
          <div className="truncate text-xs text-muted">
            {n.environment_name}{n.cluster_name ? ` · ${n.cluster_name}` : ""}
            {n.info?.pve_version ? ` · PVE ${n.info.pve_version}` : ""}
          </div>
        </div>
        <div className="text-right text-xs text-muted">
          {n.online ? (n.metrics?.uptime_seconds ? `${t("px.up")} ${formatDuration(n.metrics.uptime_seconds)}` : t("px.online")) : `${t("px.offline")} · ${timeAgo(n.last_seen_at, i18n.language)}`}
        </div>
      </Link>
      <div className="flex-1 space-y-4 px-5 py-4">
        <NodeMetricsBars n={n} />
        <div className="flex flex-wrap items-center gap-2">
          {g && <Badge>{t("px.guests_short", { vms: `${g.vms_running}/${g.vms_total}`, cts: `${g.cts_running}/${g.cts_total}` })}</Badge>}
          {n.updates.count > 0 ? <Badge tone="accent"><ArrowUpCircle className="size-3.5" />{t("px.updates_count", { count: n.updates.count })}</Badge> : n.updates.checked_at ? <Badge tone="online" dot>{t("px.up_to_date")}</Badge> : null}
          {n.updates.reboot_required && <Badge tone="warning">{t("px.reboot_required")}</Badge>}
          {n.agent_update_available && <Badge tone="info">{t("px.agent_outdated")}</Badge>}
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
        <span className="flex items-center gap-1.5 text-xs font-semibold text-muted"><Cpu className="size-3.5" /> {t("px.governor")}</span>
        <GovernorSelect n={n} disabled={!admin || !n.online} />
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ add a node */
/** Shown wherever an install command is shown, when no agent address is configured. */
export function AgentAddressNotice({ info, onNavigate, here }: { info: ConnectInfo; onNavigate?: () => void; here?: boolean }) {
  const { t } = useTranslation();
  if (!info.endpoint_source || info.endpoint_source === "address") return null;
  const key = info.endpoint_source === "lan_ip" ? "agents.addr_lan" : info.proxy_mode ? "agents.addr_public_proxy" : "agents.addr_public";
  return (
    <Alert tone={info.endpoint_source === "lan_ip" ? "info" : "warning"} title={t("agents.addr_title")}>
      {t(key, { endpoint: info.endpoint, port: info.port })}{" "}
      {here ? t("agents.addr_fix_here") : <Link to="/admin/agents" onClick={onNavigate} className="font-semibold underline">{t("agents.addr_fix")}</Link>}
    </Alert>
  );
}

export function PairDialog({ open, onOpenChange, environmentId }: { open: boolean; onOpenChange: (v: boolean) => void; environmentId?: string }) {
  const { t } = useTranslation();
  const envs = useEnvironments();
  const qc = useQueryClient();
  const [env, setEnv] = useState(environmentId ?? "");
  const [label, setLabel] = useState("");
  const [result, setResult] = useState<(Pairing & { connect: ConnectInfo }) | null>(null);
  useEffect(() => {
    if (open) {
      setResult(null);
      setLabel("");
      setEnv(environmentId ?? envs.data?.[0]?.id ?? "");
    }
  }, [open, environmentId, envs.data]);
  const create = useMutation({
    mutationFn: () => post<Pairing & { connect: ConnectInfo }>("api/pairing", { environment_id: env, label: label || null }),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["pairing"] });
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      wide
      title={t("px.pair_title")}
      description={t("px.pair_desc")}
      footer={result ? <Button variant="primary" onClick={() => onOpenChange(false)}>{t("common.done")}</Button> : (
        <>
          <Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button>
          <Button variant="primary" loading={create.isPending} disabled={!env} onClick={() => create.mutate()}><KeyRound /> {t("px.pair_create")}</Button>
        </>
      )}
    >
      {!result ? (
        <div className="space-y-4">
          {create.isError && <Alert tone="offline">{errorMessage(create.error, t)}</Alert>}
          <Field label={t("px.environment")}>
            <Select value={env} onChange={(e) => setEnv(e.target.value)}>
              {envs.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </Select>
          </Field>
          <Field label={t("px.pair_label")} hint={t("px.pair_label_hint")}>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="pve01" />
          </Field>
          <Alert tone="info">{t("px.pair_how")}</Alert>
        </div>
      ) : (
        <div className="space-y-5">
          {!result.connect.packages_available && <Alert tone="warning">{t("px.no_packages")}</Alert>}
          <AgentAddressNotice info={result.connect} onNavigate={() => onOpenChange(false)} />
          <div className="flex flex-wrap items-center gap-3 rounded-md bg-sunken px-4 py-3">
            <div className="flex-1">
              <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("px.pair_code")}</div>
              <div className="mono text-2xl font-bold tracking-[0.12em]">{result.code}</div>
            </div>
            <div className="text-right text-xs text-muted">{t("px.pair_valid")}<br />{new Date(result.expires_at).toLocaleString()}</div>
            <CopyButton value={result.code} />
          </div>
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><Terminal className="size-4" /> {t("px.pair_run")}</div>
            <CodeBlock code={result.command ?? ""} />
          </div>
          <details className="rounded-md border border-line p-4">
            <summary className="cursor-pointer text-sm font-semibold">{t("px.pair_manual")}</summary>
            <p className="my-3 text-sm text-muted">{t("px.pair_manual_text")}</p>
            <CodeBlock code={result.manual ?? ""} />
          </details>
          <div className="grid gap-2 text-xs text-muted sm:grid-cols-2">
            <div><span className="font-semibold text-ink">{t("px.endpoint")}:</span> <span className="mono">{result.connect.endpoint}</span></div>
            <div className="break-all"><span className="font-semibold text-ink">{t("px.fingerprint")}:</span> <span className="mono">{result.connect.fingerprint.slice(0, 16)}…</span></div>
          </div>
          <Link to="/admin/agents" className="text-sm font-semibold text-accent-text hover:underline" onClick={() => onOpenChange(false)}>{t("px.endpoint_settings")} →</Link>
        </div>
      )}
    </Dialog>
  );
}

export function RefreshButton({ onClick, loading }: { onClick: () => void; loading?: boolean }) {
  const { t } = useTranslation();
  return <Button size="sm" onClick={onClick} loading={loading}>{!loading && <RefreshCw />} {t("px.refresh")}</Button>;
}
