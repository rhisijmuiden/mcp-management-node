import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import { useAllNodes } from "@/components/guests";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Alert } from "@/components/ui/misc";
import type { NodeInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Proxmox VE nodes (with the agent's "pve" capability). */
export function usePveNodes() {
  const nodes = useAllNodes();
  const list = useMemo(() => (nodes.data ?? []).filter((n) => n.capabilities.includes("pve")), [nodes.data]);
  return { ...nodes, list };
}

/** One node per cluster is enough to talk to the cluster; the picker shows all nodes, grouped. */
export function useNodeParam(key = "node") {
  const [params, setParams] = useSearchParams();
  const { list, isLoading } = usePveNodes();
  const value = params.get(key) ?? "";
  const setValue = (v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(key, v); else p.delete(key);
    setParams(p, { replace: true });
  };
  useEffect(() => {
    if (!value && list.length) {
      const first = list.find((n) => n.online) ?? list[0];
      setValue(first.id);
    }
  }, [value, list.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const node = list.find((n) => n.id === value) ?? null;
  return { nodeId: value, setNodeId: setValue, node, nodes: list, isLoading };
}

export function NodeSelect({ value, onChange, nodes, label, className }: { value: string; onChange: (v: string) => void; nodes: NodeInfo[]; label?: ReactNode; className?: string }) {
  const { t } = useTranslation();
  const groups = useMemo(() => {
    const g = new Map<string, NodeInfo[]>();
    for (const n of nodes) {
      const k = `${n.environment_name ?? "-"}${n.cluster_name ? ` · ${n.cluster_name}` : ""}`;
      g.set(k, [...(g.get(k) ?? []), n]);
    }
    return [...g.entries()];
  }, [nodes]);
  const sel = (
    <Select value={value} onChange={(e) => onChange(e.target.value)} className={cn("min-w-56", className)}>
      {!value && <option value="">…</option>}
      {groups.map(([k, ns]) => (
        <optgroup key={k} label={k}>
          {ns.map((n) => <option key={n.id} value={n.id}>{n.name}{n.online ? "" : ` · ${t("px.offline")}`}</option>)}
        </optgroup>
      ))}
    </Select>
  );
  return label ? <Field label={label}>{sel}</Field> : sel;
}

const VLAN_COLORS = ["#9cc3ff", "#ff6b57", "#f2b640", "#4acb8c", "#b392f0", "#5ad1d1", "#ff9ec7", "#c9d3e6", "#8fb3ff", "#ffb38a"];

export function vlanColor(tag: number | null | undefined): string {
  if (!tag) return "#4f5b73";
  return VLAN_COLORS[Math.abs(tag * 7) % VLAN_COLORS.length];
}

/** A small trend line; values are percentages (0-100). */
export function Sparkline({ values, color = "currentColor", width = 120, height = 32, className, label }: { values: (number | null)[]; color?: string; width?: number; height?: number; className?: string; label?: string }) {
  const vals = values.map((v) => (v == null ? null : Math.max(0, Math.min(100, v))));
  const pts = vals.map((v, i) => (v == null ? null : `${((i * width) / Math.max(1, vals.length - 1)).toFixed(1)},${(height - 2 - (v * (height - 4)) / 100).toFixed(1)}`)).filter(Boolean);
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={className} role="img" aria-label={label}>
      <line x1="0" y1={height - 1} x2={width} y2={height - 1} stroke="var(--line)" strokeWidth="1" />
      {pts.length > 1 && <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />}
    </svg>
  );
}

/** Coloured unified diff. */
export function DiffView({ diff, className }: { diff: string; className?: string }) {
  const lines = diff.split("\n");
  return (
    <pre className={cn("max-h-[420px] overflow-auto rounded-md bg-nacht p-3 text-[12px] leading-5 text-[#e8edf7]", className)}>
      {lines.map((l, i) => (
        <div key={i} className={cn(l.startsWith("+") && !l.startsWith("+++") ? "bg-[#1c8a57]/30 text-[#b6f0d0]" : l.startsWith("-") && !l.startsWith("---") ? "bg-[#c23434]/30 text-[#ffc9c9]" : l.startsWith("@@") ? "text-lucht" : "")}>{l || " "}</div>
      ))}
    </pre>
  );
}

/** A confirmation that needs the user to type a word (node name, zone id). */
export function TypeToConfirm({ open, onOpenChange, title, children, word, onConfirm, loading, danger = true, action }: { open: boolean; onOpenChange: (v: boolean) => void; title: ReactNode; children?: ReactNode; word: string; onConfirm: (typed: string) => void; loading?: boolean; danger?: boolean; action: ReactNode }) {
  const { t } = useTranslation();
  const [typed, setTyped] = useState("");
  useEffect(() => { if (open) setTyped(""); }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title}
      footer={<>
        <Button onClick={() => onOpenChange(false)}>{t("common.cancel")}</Button>
        <Button variant={danger ? "danger" : "primary"} disabled={typed !== word} loading={loading} onClick={() => onConfirm(typed)}>{action}</Button>
      </>}>
      <div className="space-y-4">
        {children}
        <Field label={t("net.type_to_confirm", { word })}><Input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus className="mono" /></Field>
      </div>
    </Dialog>
  );
}

export function ApiErrors({ items }: { items: { node: string; error: string }[] }) {
  if (!items.length) return null;
  return <Alert tone="warning">{items.map((e) => `${e.node}: ${e.error}`).join("\n")}</Alert>;
}

export const tabCls = "rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer";
