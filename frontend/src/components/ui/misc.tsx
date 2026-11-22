import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

type Tone = "online" | "warning" | "offline" | "info" | "neutral" | "accent";

const toneClass: Record<Tone, string> = {
  online: "bg-online-soft text-online",
  warning: "bg-warning-soft text-warning",
  offline: "bg-offline-soft text-offline",
  info: "bg-info-soft text-ink",
  neutral: "bg-sunken text-muted",
  accent: "bg-accent-soft text-accent-text",
};

export function Badge({ tone = "neutral", children, className, dot }: { tone?: Tone; children: ReactNode; className?: string; dot?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold", toneClass[tone], className)}>
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Alert({ tone = "info", title, children, className }: { tone?: Tone; title?: ReactNode; children?: ReactNode; className?: string }) {
  const Icon = tone === "online" ? CheckCircle2 : tone === "offline" ? XCircle : tone === "warning" ? AlertTriangle : Info;
  return (
    <div className={cn("flex gap-3 rounded-md px-4 py-3 text-sm", toneClass[tone], className)}>
      <Icon className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 text-ink">
        {title && <div className="font-semibold">{title}</div>}
        {children && <div className={cn("break-words", title && "text-muted")}>{children}</div>}
      </div>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-5 animate-spin text-muted", className)} />;
}

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="display text-[28px] leading-[34px]">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-[15px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      {icon && <div className="mb-3 grid size-12 place-items-center rounded-lg bg-sunken text-muted [&_svg]:size-6">{icon}</div>}
      <div className="font-bold">{title}</div>
      {children && <div className="mt-1 max-w-md text-sm text-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Stat({ label, value, hint, tone, icon }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: Tone; icon?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-raised p-4 shadow-card">
      <div className="flex items-center justify-between text-[13px] font-semibold text-muted">
        <span>{label}</span>
        {icon && <span className={cn("grid size-8 place-items-center rounded-md [&_svg]:size-4", tone ? toneClass[tone] : "bg-sunken")}>{icon}</span>}
      </div>
      <div className="display mt-2 text-[26px] leading-8">{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="mono rounded border border-line bg-sunken px-1.5 py-0.5 text-[11px]">{children}</kbd>;
}
