/** Small fetch wrapper: relative to <base href>, sends the CSRF header, turns errors into ApiError. */

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)mmn_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export function apiUrl(path: string): string {
  return new URL(path.replace(/^\//, ""), document.baseURI).toString();
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/* The customer picked in the switcher: the backend then limits every query to that customer. */
let currentCustomer = "";
export function setCurrentCustomer(id: string) {
  currentCustomer = id;
}
export const getCurrentCustomer = () => currentCustomer;

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown; allCustomers?: boolean } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (currentCustomer && !init.allCustomers) headers.set("X-Stackpilot-Customer", currentCustomer);
  if (init.json !== undefined) headers.set("Content-Type", "application/json");
  const method = (init.method || "GET").toUpperCase();
  if (!["GET", "HEAD"].includes(method)) headers.set("X-CSRF-Token", csrfToken());
  const res = await fetch(apiUrl(path), {
    ...init,
    method,
    headers,
    credentials: "same-origin",
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

export const get = <T,>(p: string) => api<T>(p);
/** GET that ignores the customer switcher (e.g. the list of all customers). */
export const getAll = <T,>(p: string) => api<T>(p, { allCustomers: true });
export const post = <T,>(p: string, json?: unknown) => api<T>(p, { method: "POST", json: json ?? {} });
export const put = <T,>(p: string, json?: unknown) => api<T>(p, { method: "PUT", json: json ?? {} });
export const patch = <T,>(p: string, json?: unknown) => api<T>(p, { method: "PATCH", json: json ?? {} });
export const del = <T,>(p: string) => api<T>(p, { method: "DELETE" });

export function errorMessage(e: unknown, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (e instanceof ApiError) {
    const d = e.detail as unknown;
    if (typeof d === "string") {
      const key = `errors.${d}`;
      const translated = t(key);
      return translated === key ? d : translated;
    }
    if (d && typeof d === "object" && "errors" in d) {
      const errs = (d as { errors: { field: string; msg: string }[] }).errors;
      return errs.map((x) => `${x.field}: ${x.msg}`).join("\n");
    }
    return `HTTP ${e.status}`;
  }
  return e instanceof Error ? e.message : String(e);
}
