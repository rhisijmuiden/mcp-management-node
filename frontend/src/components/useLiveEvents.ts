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
