import * as D from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { Building2, CornerDownLeft, FileText, Laptop, Monitor, Search, Server } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { getAll } from "@/lib/api";
import type { NodeInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CUSTOMER_CSS, useCustomer, useCustomers } from "./customers";

export interface PageEntry {
  to: string;
  label: string;
  group: string;
}

interface Result {
  key: string;
  kind: "page" | "customer" | "machine";
  label: string;
  hint: string;
  icon: ReactNode;
  run: () => void;
}

/** Lower-case, accent-free text for matching ("Back-ups" matches "backups"). */
const norm = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[-_ ]/g, "");

function matches(text: string, q: string) {
  return norm(text).includes(q);
}

export function machineLink(n: Pick<NodeInfo, "id" | "kind" | "role">) {
  if (n.role === "workstation") return `/workstations/${n.id}`;
  return n.kind === "proxmox" ? `/proxmox/nodes/${n.id}` : `/servers/${n.id}`;
}

/** Ctrl+K search: jump to a page, a customer or a machine. */
export function CommandPalette({ open, onOpenChange, pages }: { open: boolean; onOpenChange: (v: boolean) => void; pages: PageEntry[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { setCustomer } = useCustomer();
  const customers = useCustomers();
  const nodes = useQuery({ queryKey: ["nodes", "palette"], queryFn: () => getAll<NodeInfo[]>("api/nodes"), enabled: open, staleTime: 60_000 });
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQ("");
      setActive(0);
    }
  }, [open]);

  const results = useMemo<Result[]>(() => {
    const nq = norm(q.trim());
    const done = (fn: () => void) => () => {
      onOpenChange(false);
      fn();
    };
    const out: Result[] = [];
    const pageHits = pages.filter((p) => !nq || matches(p.label, nq) || matches(p.group, nq));
    for (const p of pageHits.slice(0, nq ? 8 : 6)) {
      out.push({ key: `p:${p.to}`, kind: "page", label: p.label, hint: p.group, icon: <FileText />, run: done(() => navigate(p.to)) });
    }
    for (const c of (customers.data ?? []).filter((c) => !nq || matches(c.name, nq)).slice(0, nq ? 6 : 4)) {
      out.push({
        key: `c:${c.id}`, kind: "customer", label: c.name, hint: t("top.search_customer_hint"),
        icon: <span className="size-2.5 rounded-full" style={{ background: CUSTOMER_CSS[c.color] ?? "var(--lucht)" }} />,
        run: done(() => { setCustomer(c.id); navigate(`/customers/${c.id}`); }),
      });
    }
    if (nq) {
      const hits = (nodes.data ?? []).filter((n) => matches(n.name, nq) || (n.hostname && matches(n.hostname, nq)));
      for (const n of hits.slice(0, 8)) {
        out.push({
          key: `m:${n.id}`, kind: "machine", label: n.name,
          hint: [n.environment_name, n.role === "workstation" ? t("cust.workstations") : n.kind === "proxmox" ? "Proxmox" : t("nav.servers")].filter(Boolean).join(" · "),
          icon: n.role === "workstation" ? <Laptop /> : n.kind === "proxmox" ? <Server /> : <Monitor />,
          run: done(() => navigate(machineLink(n))),
        });
      }
    }
    return out;
  }, [q, pages, customers.data, nodes.data, navigate, onOpenChange, setCustomer, t]);

  useEffect(() => setActive(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); results[active]?.run(); }
  };

  const groups: { kind: Result["kind"]; title: string }[] = [
    { kind: "page", title: t("top.search_pages") },
    { kind: "customer", title: t("top.search_customers") },
    { kind: "machine", title: t("top.search_machines") },
  ];

  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-nacht/50 backdrop-blur-[2px]" />
        <D.Content className="fixed left-1/2 top-[12vh] z-50 flex max-h-[70vh] w-[calc(100vw-24px)] max-w-xl -translate-x-1/2 flex-col overflow-hidden rounded-lg border border-line bg-raised shadow-2xl focus:outline-none" onKeyDown={onKey}>
          <D.Title className="sr-only">{t("top.search")}</D.Title>
          <D.Description className="sr-only">{t("top.search_ph")}</D.Description>
          <div className="flex items-center gap-3 border-b border-line px-4">
            <Search className="size-5 shrink-0 text-muted" />
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("top.search_ph")}
              aria-label={t("top.search")}
              className="h-14 min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-muted focus-visible:outline-none"
            />
            <kbd className="mono rounded border border-line px-1.5 text-[11px] text-muted">Esc</kbd>
          </div>
          <div ref={listRef} className="overflow-y-auto p-2" role="listbox" aria-label={t("top.search")}>
            {!results.length && <p className="px-3 py-6 text-center text-sm text-muted">{nodes.isLoading ? t("top.search_loading") : t("top.search_none")}</p>}
            {groups.map((g) => {
              const rows = results.map((r, i) => ({ r, i })).filter(({ r }) => r.kind === g.kind);
              if (!rows.length) return null;
              return (
                <div key={g.kind} className="pb-1">
                  <div className="px-3 pb-1 pt-2 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{g.title}</div>
                  {rows.map(({ r, i }) => (
                    <button
                      key={r.key}
                      type="button"
                      role="option"
                      aria-selected={i === active}
                      data-idx={i}
                      onMouseMove={() => setActive(i)}
                      onClick={r.run}
                      className={cn("flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-left text-sm [&_svg]:size-4 [&_svg]:text-muted", i === active && "bg-sunken")}
                    >
                      <span className="grid size-5 shrink-0 place-items-center">{r.icon}</span>
                      <span className="min-w-0 flex-1 truncate font-semibold">{r.label}</span>
                      <span className="max-w-[45%] truncate text-xs text-muted">{r.hint}</span>
                      {i === active && <CornerDownLeft className="shrink-0" />}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
          <div className="flex items-center gap-4 border-t border-line px-4 py-2 text-[11.5px] text-muted">
            <span><Building2 className="mr-1 inline size-3.5" />{t("top.search_footer")}</span>
          </div>
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}
