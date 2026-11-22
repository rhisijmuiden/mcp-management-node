import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, Wifi } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/misc";
import { get } from "@/lib/api";
import type { UnifiOverview } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Small UniFi tile for the dashboard. */
export function UnifiDashboardCard() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["unifi-overview", ""], queryFn: () => get<UnifiOverview>("api/unifi/network/overview"), refetchInterval: 60_000 });
  const tot = q.data?.totals;
  if (!q.data?.connections.length) return null;
  return (
    <Card>
      <CardHeader icon={<Wifi />} title="UniFi" description={t("uf.dash_desc")} action={<Link to="/unifi/network" className="text-sm font-semibold text-accent-text hover:underline">{t("uf.open")}</Link>} />
      <CardBody className="grid grid-cols-3 gap-3 text-center">
        <div><div className="display text-2xl">{tot ? tot.devices - tot.offline : "…"}<span className="text-base text-muted">/{tot?.devices ?? ""}</span></div><div className="text-xs text-muted">{t("uf.online")}</div></div>
        <div><div className={cn("display text-2xl", tot?.offline ? "text-offline" : "")}>{tot?.offline ?? "…"}</div><div className="text-xs text-muted">{t("uf.offline")}</div></div>
        <div><div className="display text-2xl">{tot?.clients ?? "…"}</div><div className="text-xs text-muted">{t("uf.clients_l")}</div></div>
        {!!tot?.firmware && <div className="col-span-3"><Badge tone="warning"><ShieldAlert className="size-3" />{t("uf.dash_fw", { count: tot.firmware })}</Badge></div>}
      </CardBody>
    </Card>
  );
}
