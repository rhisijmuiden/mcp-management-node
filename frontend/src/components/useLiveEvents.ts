import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { apiUrl } from "@/lib/api";

/** Listens to /api/ws and refreshes the relevant queries. Reconnects with backoff. */
export function useLiveEvents() {
  const qc = useQueryClient();
  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let delay = 1000;
    let timer: number | undefined;
    // Metrics arrive every 15 s per node: batch the refreshes.
    const pending = new Set<string>();
    let nodeTimer: number | undefined;
    const scheduleNodes = (id: string) => {
      pending.add(id);
      if (nodeTimer) return;
      nodeTimer = window.setTimeout(() => {
        nodeTimer = undefined;
        qc.invalidateQueries({ queryKey: ["overview"] });
        qc.invalidateQueries({ queryKey: ["servers-overview"] });
        qc.invalidateQueries({ queryKey: ["environments"] });
        for (const nid of pending) qc.invalidateQueries({ queryKey: ["node", nid] });
        qc.invalidateQueries({ queryKey: ["guests"] });
        pending.clear();
      }, 1500);
    };
    const jobTimers = new Map<string, number>();
    const scheduleJob = (jid: string) => {
      if (jobTimers.has(jid)) return;
      jobTimers.set(jid, window.setTimeout(() => {
        jobTimers.delete(jid);
        qc.invalidateQueries({ queryKey: ["job", jid] });
      }, 1000));
    };
    const connect = () => {
      const url = apiUrl("api/ws").replace(/^http/, "ws");
      ws = new WebSocket(url);
      ws.onopen = () => (delay = 1000);
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data) as { event: string; data: Record<string, unknown> };
          if (msg.event === "backup.progress") {
            qc.invalidateQueries({ queryKey: ["backup-runs"] });
            if (msg.data.step === "finished") qc.invalidateQueries({ queryKey: ["dashboard"] });
          }
          if (msg.event === "maintenance") qc.invalidateQueries();
          if (msg.event === "node.status") scheduleNodes(String(msg.data.node_id ?? ""));
          if (msg.event === "task.run") {
            const rid = String(msg.data.run_id ?? "");
            qc.invalidateQueries({ queryKey: ["task-run", rid] });
            if (msg.data.started || msg.data.done) {
              qc.invalidateQueries({ queryKey: ["task-runs"] });
              qc.invalidateQueries({ queryKey: ["tasks"] });
              qc.invalidateQueries({ queryKey: ["templates"] });
            }
          }
          if (msg.event === "template.download" && (msg.data.finished || msg.data.error)) {
            qc.invalidateQueries({ queryKey: ["templates"] });
            if (msg.data.error) window.dispatchEvent(new CustomEvent("mmn:template-error", { detail: msg.data.error }));
          }
          if (msg.event === "monitoring.alerts") {
            qc.invalidateQueries({ queryKey: ["mon-alerts"] });
            qc.invalidateQueries({ queryKey: ["monitoring"] });
          }
          if (msg.event === "deploy.run") {
            qc.invalidateQueries({ queryKey: ["deployments"] });
            qc.invalidateQueries({ queryKey: ["deployment", String(msg.data.deployment_id ?? "")] });
          }
          if (msg.event === "transfer") {
            qc.invalidateQueries({ queryKey: ["transfers"] });
            qc.invalidateQueries({ queryKey: ["transfer", String(msg.data.id ?? "")] });
          }
          if (msg.event === "approval.created" || msg.event === "approval.changed") {
            for (const k of ["approvals", "approvals-count", "approval", "gateway-overview"]) qc.invalidateQueries({ queryKey: [k] });
            if (msg.event === "approval.created") window.dispatchEvent(new CustomEvent("mmn:approval", { detail: msg.data }));
          }
          if (msg.event === "gateway.paused" || msg.event === "gateway.policy") {
            for (const k of ["gateway-status", "gateway-overview", "gateway-policies"]) qc.invalidateQueries({ queryKey: [k] });
          }
          if (msg.event === "node.job") {
            const jid = String(msg.data.job_id ?? "");
            if (msg.data.line === undefined) {
              qc.invalidateQueries({ queryKey: ["node-jobs"] });
              qc.invalidateQueries({ queryKey: ["job", jid] });
            } else scheduleJob(jid);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (stopped) return;
        timer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 30000);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [qc]);
}
