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
        qc.invalidateQueries({ queryKey: ["environments"] });
        for (const nid of pending) qc.invalidateQueries({ queryKey: ["node", nid] });
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
