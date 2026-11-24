import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Monitor, Pause, Play, Power, RotateCcw, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/misc";
import { errorMessage, get, post } from "@/lib/api";
import type { Guest, NodeJob, Overview } from "@/lib/types";
import { cn } from "@/lib/utils";

export const useAllNodes = () =>
  useQuery({ queryKey: ["overview", "all"], queryFn: () => get<Overview>("api/proxmox/overview"), refetchInterval: 30_000, select: (d) => d.nodes });

/** Every machine with an agent: Proxmox VE nodes, Linux and Windows servers and workstations. */
export const useAllMachines = (enabled = true) =>
  useQuery({ queryKey: ["overview", "machines"], queryFn: () => get<Overview>("api/proxmox/overview?kind=all"), enabled, refetchInterval: 30_000, select: (d) => d.nodes });

export function GuestIcon({ type, className }: { type: Guest["type"]; className?: string }) {
  const I = type === "qemu" ? Monitor : Box;
  return <I className={cn("size-4 text-muted", className)} />;
}

export function GuestStatus({ g }: { g: Pick<Guest, "status" | "lock" | "online"> }) {
  const { t } = useTranslation();
  if (!g.online) return <Badge tone="neutral" dot>{t("gx.node_offline")}</Badge>;
  const tone = g.status === "running" ? "online" : g.status === "paused" || g.status === "suspended" ? "warning" : "neutral";
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge tone={tone} dot>{t(`gx.status_${g.status}`, { defaultValue: g.status })}</Badge>
      {g.lock && <Badge tone="warning">{t("gx.locked", { lock: g.lock })}</Badge>}
    </span>
  );
}

export function pct(used?: number, total?: number) {
  return total ? Math.min(100, Math.round(((used ?? 0) / total) * 100)) : 0;
}

export function MiniBar({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cn("h-1.5 w-16 overflow-hidden rounded-full bg-sunken", className)}>
      <div className={cn("h-full rounded-full", value > 90 ? "bg-offline" : value > 75 ? "bg-warning" : "bg-online")} style={{ width: `${value}%` }} />
    </div>
  );
}

export type PowerAction = "start" | "stop" | "shutdown" | "reboot" | "suspend" | "resume";

export function usePower(g: Pick<Guest, "node_id" | "vmid" | "name">, onJob?: (j: NodeJob) => void) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (action: PowerAction) => post<NodeJob>(`api/guests/${g.node_id}/${g.vmid}/power`, { action }),
    onSuccess: (j, action) => {
      toast.success(t("gx.power_started", { action: t(`gx.power_${action}`), name: g.name || g.vmid }));
      onJob?.(j);
      qc.invalidateQueries({ queryKey: ["guests"] });
      qc.invalidateQueries({ queryKey: ["guest", g.node_id, String(g.vmid)] });
    },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
}

/** Start / shutdown / stop / reboot (and pause for VMs) for one guest. */
export function PowerButtons({ g, onJob, compact, disabled }: { g: Guest; onJob?: (j: NodeJob) => void; compact?: boolean; disabled?: boolean }) {
  const { t } = useTranslation();
  const power = usePower(g, onJob);
  const running = g.status === "running";
  const paused = g.status === "paused" || g.status === "suspended";
  const off = disabled || !g.online || !!g.lock || power.isPending;
  const btn = (action: PowerAction, icon: React.ReactNode, variant: "secondary" | "ghost" | "dark" | "danger" = compact ? "ghost" : "secondary") => (
    <Button key={action} size={compact ? "icon" : "sm"} variant={variant} disabled={off} title={t(`gx.power_${action}`)}
      className={compact ? "size-8" : undefined}
      loading={power.isPending && power.variables === action}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); power.mutate(action); }}>
      {!(power.isPending && power.variables === action) && icon}{!compact && t(`gx.power_${action}`)}
    </Button>
  );
  if (g.template) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {!running && !paused && btn("start", <Play />, compact ? "ghost" : "dark")}
      {paused && btn("resume", <Play />, compact ? "ghost" : "dark")}
      {running && btn("shutdown", <Power />)}
      {running && btn("reboot", <RotateCcw />)}
      {running && g.type === "qemu" && !compact && btn("suspend", <Pause />)}
      {(running || paused) && btn("stop", <Square />, compact ? "ghost" : "danger")}
    </div>
  );
}
