import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";
import { ConsoleView } from "@/components/console-lazy";
import { Segmented } from "@/components/ui/form";
import { Alert, Spinner } from "@/components/ui/misc";
import { get, post } from "@/lib/api";
import type { ConsoleSession, GuestDetail } from "@/lib/types";

/** Full-window console, outside the normal layout (also opened in a new tab). */
export function ConsolePage({ kind }: { kind: "guest" | "node" | "session" }) {
  const { t } = useTranslation();
  const { node = "", vmid = "", sid = "" } = useParams();
  const [mode, setMode] = useState<"lxc-console" | "lxc-shell">("lxc-console");
  const guest = useQuery({
    queryKey: ["guest", node, vmid],
    queryFn: () => get<GuestDetail>(`api/guests/${node}/${vmid}`),
    enabled: kind === "guest",
  });
  const initial = useQuery({
    queryKey: ["console-session", sid],
    queryFn: () => get<ConsoleSession & { job_id?: string }>(`api/console/${sid}`),
    enabled: kind === "session",
    retry: false,
    staleTime: Infinity,
  });
  const title = kind === "guest" ? (guest.data ? `${guest.data.vmid} ${guest.data.name}` : vmid) : kind === "node" ? t("gx.shell") : initial.data?.label ?? "";
  useEffect(() => { document.title = `${title} · ${t("console.title")}`; }, [title, t]);

  const openGuest = useCallback(
    (size: { cols: number; rows: number }) => post<ConsoleSession>(`api/guests/${node}/${vmid}/console`, { mode: guest.data?.type === "lxc" ? mode : "vnc", ...size }),
    [node, vmid, mode, guest.data?.type],
  );
  const openNode = useCallback((size: { cols: number; rows: number }) => post<ConsoleSession>(`api/nodes/${node}/shell`, size), [node]);

  let body = null;
  if (kind === "guest") {
    if (guest.isLoading) body = <Spinner />;
    else if (!guest.data) body = <Alert tone="offline">{t("gx.not_found")}</Alert>;
    else if (guest.data.status !== "running") body = <Alert tone="warning">{t("gx.console_not_running")}</Alert>;
    else body = (
      <ConsoleView key={`${guest.data.type}-${mode}`} open={openGuest} className="h-full"
        toolbar={guest.data.type === "lxc" && (
          <Segmented value={mode} onChange={setMode} options={[{ value: "lxc-console", label: t("gx.mode_console") }, { value: "lxc-shell", label: t("gx.mode_shell") }]} />
        )} />
    );
  } else if (kind === "node") body = <ConsoleView open={openNode} className="h-full" />;
  else if (kind === "session") {
    if (initial.isLoading) body = <Spinner />;
    else if (!initial.data) body = <Alert tone="warning">{t("console.single_use")}</Alert>;
    else body = <ConsoleView initial={initial.data} className="h-full" />;
  }

  const back = kind === "guest" ? `/proxmox/guests/${node}/${vmid}` : kind === "node" ? `/proxmox/nodes/${node}` : "/proxmox/guests";
  return (
    <div className="flex h-full flex-col bg-nacht">
      <header className="flex h-12 shrink-0 items-center gap-3 px-3 text-white">
        <Link to={back} className="flex items-center gap-2 rounded-md px-2 py-1 text-sm font-semibold hover:bg-white/10"><ArrowLeft className="size-4" /> {t("common.back")}</Link>
        <img src="brand/rhis-icon.svg" alt="" className="size-6 rounded-[6px]" />
        <span className="truncate text-sm font-semibold">{title}</span>
      </header>
      <div className="min-h-0 flex-1 px-2 pb-2">{body}</div>
    </div>
  );
}
