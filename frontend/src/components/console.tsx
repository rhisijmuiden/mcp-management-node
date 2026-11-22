import RFB from "@novnc/novnc/lib/rfb";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ClipboardPaste, ExternalLink, Keyboard, Maximize2, PlugZap, RotateCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Badge, Spinner } from "@/components/ui/misc";
import { apiUrl, del, errorMessage } from "@/lib/api";
import type { ConsoleSession } from "@/lib/types";
import { cn } from "@/lib/utils";

type State = "opening" | "connecting" | "connected" | "closed" | "error";

/** Rough terminal size for the space we have, before xterm can measure itself. */
export function guessSize(el?: HTMLElement | null) {
  const w = el?.clientWidth || window.innerWidth;
  const h = el?.clientHeight || window.innerHeight - 56;
  return { cols: Math.max(40, Math.min(400, Math.floor((w - 24) / 8.45))), rows: Math.max(10, Math.min(200, Math.floor((h - 16) / 17))) };
}

const wsUrl = (sid: string) => apiUrl(`api/console/ws/${sid}`).replace(/^http/, "ws");

/**
 * A console for one session: noVNC for VMs, xterm.js for container consoles and shells.
 * `open` creates a fresh session (sessions are single use); without it the view attaches
 * once to `initial` and cannot reconnect.
 */
export function ConsoleView({
  open,
  initial,
  className,
  toolbar,
  newTabUrl,
  onExit,
}: {
  open?: (size: { cols: number; rows: number }) => Promise<ConsoleSession>;
  initial?: ConsoleSession;
  className?: string;
  toolbar?: ReactNode;
  newTabUrl?: string;
  onExit?: (code: number | null) => void;
}) {
  const { t } = useTranslation();
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<State>("opening");
  const [error, setError] = useState<string | null>(null);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [session, setSession] = useState<ConsoleSession | null>(initial ?? null);
  const rfb = useRef<RFB | null>(null);
  const term = useRef<Terminal | null>(null);
  const onExitRef = useRef(onExit);
  onExitRef.current = onExit;

  useEffect(() => {
    let cancelled = false;
    let sid: string | null = null;
    let ws: WebSocket | null = null;
    let cleanup: (() => void) | null = null;
    const el = host.current!;
    setState("opening");
    setError(null);
    setExitCode(null);

    const start = async () => {
      let s: ConsoleSession;
      try {
        if (attempt === 0 && initial) s = initial;
        else if (open) s = await open(guessSize(el));
        else throw new Error(t("console.single_use"));
      } catch (e) {
        if (!cancelled) {
          setState("error");
          setError(errorMessage(e, t));
        }
        return;
      }
      sid = s.session;
      if (cancelled) {
        del(`api/console/${s.session}`).catch(() => undefined);
        return;
      }
      setSession(s);
      setState("connecting");
      el.innerHTML = "";

      if (s.kind === "vnc") {
        const r = new RFB(el, wsUrl(s.session), { credentials: { password: s.password ?? "" } });
        r.scaleViewport = true;
        r.resizeSession = false;
        r.focusOnClick = true;
        r.background = "#0b1224";
        r.addEventListener("connect", () => { setState("connected"); r.focus(); });
        r.addEventListener("credentialsrequired", () => r.sendCredentials({ password: s.password ?? "" }));
        r.addEventListener("disconnect", () => { if (!cancelled) setState("closed"); });
        r.addEventListener("securityfailure", () => { if (!cancelled) { setState("error"); setError(t("console.auth_failed")); } });
        rfb.current = r;
        cleanup = () => { try { r.disconnect(); } catch { /* already closed */ } rfb.current = null; };
        return;
      }

      const x = new Terminal({
        cursorBlink: true,
        fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 13.5,
        scrollback: 5000,
        theme: { background: "#0b1224", foreground: "#e8edf7", cursor: "#ff7a59", selectionBackground: "#3b4a6b" },
        allowProposedApi: false,
      });
      const fit = new FitAddon();
      x.loadAddon(fit);
      x.open(el);
      term.current = x;
      const enc = new TextEncoder();
      ws = new WebSocket(wsUrl(s.session));
      ws.binaryType = "arraybuffer";
      const sendSize = () => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols: x.cols, rows: x.rows }));
      };
      const refit = () => { try { fit.fit(); } catch { /* not visible */ } };
      ws.onopen = () => { setState("connected"); refit(); sendSize(); x.focus(); };
      ws.onmessage = (e) => {
        if (typeof e.data === "string") {
          try {
            const m = JSON.parse(e.data) as { type?: string; code?: number; message?: string };
            if (m.type === "exit") {
              const code = typeof m.code === "number" ? m.code : null;
              setExitCode(code);
              x.write(`\r\n\x1b[2m[${m.message || t("console.exited", { code: code ?? "?" })}]\x1b[0m\r\n`);
              onExitRef.current?.(code);
            }
          } catch { /* not ours */ }
        } else x.write(new Uint8Array(e.data as ArrayBuffer));
      };
      ws.onclose = () => { if (!cancelled) setState((p) => (p === "error" ? p : "closed")); };
      const d1 = x.onData((d) => { if (ws?.readyState === WebSocket.OPEN) ws.send(enc.encode(d)); });
      const d2 = x.onBinary((d) => {
        if (ws?.readyState !== WebSocket.OPEN) return;
        const b = new Uint8Array(d.length);
        for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 0xff;
        ws.send(b);
      });
      const d3 = x.onResize(sendSize);
      const ro = new ResizeObserver(() => refit());
      ro.observe(el);
      cleanup = () => { ro.disconnect(); d1.dispose(); d2.dispose(); d3.dispose(); ws?.close(); x.dispose(); term.current = null; };
    };
    start();
    return () => {
      cancelled = true;
      cleanup?.();
      if (sid) del(`api/console/${sid}`).catch(() => undefined);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const reconnect = useCallback(() => setAttempt((a) => a + 1), []);
  const canReconnect = !!open;
  const fullscreen = () => host.current?.parentElement?.requestFullscreen?.().catch(() => undefined);
  const paste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (rfb.current) rfb.current.clipboardPasteFrom(text);
      else term.current?.paste(text);
    } catch { /* clipboard blocked */ }
  };

  const badge = state === "connected" ? <Badge tone="online" dot>{t("console.connected")}</Badge>
    : state === "closed" ? <Badge tone="neutral" dot>{exitCode !== null ? t("console.exited", { code: exitCode }) : t("console.closed")}</Badge>
      : state === "error" ? <Badge tone="offline" dot>{t("console.error")}</Badge>
        : <Badge tone="warning" dot>{t("console.connecting")}</Badge>;

  return (
    <div className={cn("flex min-h-0 flex-col overflow-hidden rounded-lg border border-line bg-[#0b1224]", className)}>
      <div className="flex flex-wrap items-center gap-2 border-b border-white/10 bg-nacht px-3 py-2 text-white">
        {badge}
        {session && <span className="mono truncate text-xs text-white/70">{session.label}</span>}
        <div className="flex-1" />
        {toolbar}
        {session?.kind === "vnc" && (
          <Button size="sm" variant="ghost" className="text-white hover:bg-white/10" disabled={state !== "connected"} onClick={() => rfb.current?.sendCtrlAltDel()}>
            <Keyboard /> Ctrl+Alt+Del
          </Button>
        )}
        <Button size="sm" variant="ghost" className="text-white hover:bg-white/10" disabled={state !== "connected"} onClick={paste} title={t("console.paste")}><ClipboardPaste /></Button>
        <Button size="sm" variant="ghost" className="text-white hover:bg-white/10" onClick={fullscreen} title={t("console.fullscreen")}><Maximize2 /></Button>
        {newTabUrl && <Button size="sm" variant="ghost" className="text-white hover:bg-white/10" onClick={() => window.open(newTabUrl, "_blank", "noopener")} title={t("gx.new_tab")}><ExternalLink /></Button>}
        {canReconnect && (
          <Button size="sm" variant="ghost" className="text-white hover:bg-white/10" onClick={reconnect} disabled={state === "opening"}>
            {state === "connected" ? <RotateCw /> : <PlugZap />} {t("console.reconnect")}
          </Button>
        )}
      </div>
      <div className="relative min-h-0 flex-1">
        <div ref={host} className="absolute inset-0 overflow-hidden p-1 [&_.xterm]:h-full" />
        {(state === "opening" || state === "connecting") && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center"><Spinner className="text-white/70" /></div>
        )}
        {state === "error" && (
          <div className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-white">
            <div className="space-y-3">
              <p>{error}</p>
              {canReconnect && <Button size="sm" variant="primary" onClick={reconnect}>{t("console.reconnect")}</Button>}
            </div>
          </div>
        )}
        {state === "closed" && session?.kind === "vnc" && (
          <div className="absolute inset-0 grid place-items-center bg-nacht/70 p-6 text-center text-sm text-white">
            <div className="space-y-3">
              <p>{t("console.vnc_closed")}</p>
              {canReconnect && <Button size="sm" variant="primary" onClick={reconnect}>{t("console.reconnect")}</Button>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
