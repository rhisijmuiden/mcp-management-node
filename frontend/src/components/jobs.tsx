import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Badge, Spinner } from "@/components/ui/misc";
import { get } from "@/lib/api";
import type { NodeJob } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

/** Human name of a job kind, e.g. "guest.snapshot.delete" -> px.job_guest_snapshot_delete. */
export function useJobLabel() {
  const { t } = useTranslation();
  return (kind: string) => t(`px.job_${kind.replaceAll(".", "_")}`, { defaultValue: kind });
}

export function Output({ text, className }: { text: string; className?: string }) {
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [text]);
  return <pre ref={ref} className={cn("max-h-[480px] overflow-auto whitespace-pre-wrap break-all rounded-md bg-nacht p-4 text-[12px] leading-5 text-[#e8edf7]", className)}>{text || " "}</pre>;
}

export function JobBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  return status === "success" ? <Badge tone="online"><CheckCircle2 className="size-3.5" /> {t("common.success")}</Badge>
    : status === "failed" ? <Badge tone="offline"><XCircle className="size-3.5" /> {t("common.failed")}</Badge>
      : status === "partial" ? <Badge tone="warning">{t("tasks.status_partial")}</Badge>
        : status === "waiting" ? <Badge tone="info" dot>{t("tasks.status_waiting")}</Badge>
          : <Badge tone="warning" dot>{t("common.running")}</Badge>;
}

export function JobView({ jobId, onDone }: { jobId: string; onDone?: (j: NodeJob) => void }) {
  const { t, i18n } = useTranslation();
  const label = useJobLabel();
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => get<NodeJob>(`api/jobs/${jobId}`),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2000 : false),
  });
  const status = job.data?.status;
  const done = useRef(false);
  useEffect(() => {
    if (job.data && status !== "running" && !done.current) {
      done.current = true;
      onDone?.(job.data);
    }
  }, [status, job.data, onDone]);
  if (!job.data) return <Spinner />;
  const j = job.data;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <JobBadge status={j.status} />
        <span className="font-semibold">{label(j.kind)}</span>
        <span className="text-muted">{j.requested_by} · {formatDate(j.started_at, i18n.language)}</span>
        {j.result?.reboot_required === true && <Badge tone="warning">{t("px.reboot_required")}</Badge>}
      </div>
      {j.error && <Alert tone="offline">{j.error}</Alert>}
      <Output text={j.output ?? ""} />
    </div>
  );
}
