/** Calls of the customer portal: its own session cookie and CSRF token, never the customer switcher of staff. */
import { ApiError, apiUrl } from "@/lib/api";

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)sp_portal_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/* "View as customer": an admin opens /portal?preview=<customer id>; kept for this tab only. Read only on the server. */
const PREVIEW_KEY = "sp.portal.preview";
function readPreview(): string {
  try {
    const q = new URLSearchParams(window.location.search).get("preview");
    if (q) sessionStorage.setItem(PREVIEW_KEY, q);
    return q || sessionStorage.getItem(PREVIEW_KEY) || "";
  } catch {
    return "";
  }
}
let previewCustomer = readPreview();
export const getPreviewCustomer = () => previewCustomer;
export function endPreview() {
  previewCustomer = "";
  try {
    sessionStorage.removeItem(PREVIEW_KEY);
  } catch {
    /* private mode */
  }
}

let onUnauthorized: (() => void) | null = null;
export function setPortalUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function papi<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.json !== undefined) headers.set("Content-Type", "application/json");
  if (previewCustomer) headers.set("X-Portal-Preview", previewCustomer);
  const method = (init.method || "GET").toUpperCase();
  if (!["GET", "HEAD"].includes(method)) headers.set("X-Portal-CSRF", csrfToken());
  const res = await fetch(apiUrl(`api/portal/${path}`), {
    ...init, method, headers, credentials: "same-origin",
    body: init.json !== undefined ? JSON.stringify(init.json) : init.body,
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text */
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown })?.detail ?? data;
    if (res.status === 401 && detail === "not_authenticated" && onUnauthorized) onUnauthorized();
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const pget = <T,>(p: string) => papi<T>(p);
export const ppost = <T,>(p: string, json?: unknown) => papi<T>(p, { method: "POST", json: json ?? {} });
export const ppatch = <T,>(p: string, json?: unknown) => papi<T>(p, { method: "PATCH", json: json ?? {} });

export interface PortalStatus { enabled: boolean; authenticated: boolean; preview?: boolean; app_name: string; default_language: string; mail_enabled: boolean }
export interface PortalMe {
  id: string; email: string; full_name: string | null; phone: string | null; role: "member" | "manager"; language: "en" | "nl";
  customer_id: string; customer_name: string | null; welcome: string;
}
