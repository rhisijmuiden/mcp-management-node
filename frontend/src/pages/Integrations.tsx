import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, Cable, Check, CloudCog, DatabaseBackup, ExternalLink, GitBranch, Mail, Network, Plug, type LucideIcon } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, PageHeader, Spinner } from "@/components/ui/misc";
import { get } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { GithubStatus } from "@/lib/types";
import { appUrl, cn, timeAgo } from "@/lib/utils";
import { GithubConnect } from "./Deployments";

interface Overview {
  customers: number;
  github: GithubStatus;
  m365: { tenants: number; customers: number; errors: number };
  unifi: { connections: number; customers: number; errors: number };
  mcp: { servers: number; enabled: number; errors: number };
  ai: { tokens: number; oauth: number };
  agents: { machines: number; online: number };
  mail: { enabled: boolean; host: string | null };
  app_backups: { targets: number };
}

type Tone = "online" | "warning" | "offline" | "neutral";

interface Step {
  /** i18n key under intg.<id>: title and text ("\n" separates bullets). */
  key: string;
  link?: { href: string; label: string };
  code?: string;
}

interface IntegrationDef {
  id: string;
  icon: LucideIcon;
  steps: Step[];
  /** The page where it is set up, opened by the last step. */
  page?: string;
  /** Rendered as the last step instead of the page button. */
  finish?: ReactNode;
  admin?: boolean;
}

const GH_FINE = "https://github.com/settings/personal-access-tokens/new";
const GH_CLASSIC = "https://github.com/settings/tokens/new?description=Stackpilot&scopes=repo,workflow,admin:repo_hook,admin:org,delete_repo,write:packages,read:user";

export function useIntegrations() {
  return useQuery({ queryKey: ["integrations"], queryFn: () => get<Overview>("api/integrations"), refetchInterval: 60_000 });
}

function defs(mcpUrl: string): IntegrationDef[] {
  return [
    {
      id: "github", icon: GitBranch, admin: true,
      steps: [
        { key: "s1" },
        { key: "s2", link: { href: GH_FINE, label: "github.com → Fine-grained tokens" } },
        { key: "s3" },
        { key: "s4", link: { href: GH_CLASSIC, label: "github.com → Tokens (classic)" } },
        { key: "s5" },
      ],
      finish: <GithubConnect guide={false} />,
    },
    {
      id: "m365", icon: CloudCog, admin: true, page: "/m365",
      steps: [{ key: "s1", link: { href: "https://entra.microsoft.com", label: "entra.microsoft.com" } }, { key: "s2" }, { key: "s3" }, { key: "s4" }],
    },
    { id: "unifi", icon: Cable, page: "/unifi/connections", steps: [{ key: "s1" }, { key: "s2" }, { key: "s3" }] },
    { id: "mcp", icon: Network, admin: true, page: "/ai/hub", steps: [{ key: "s1" }, { key: "s2" }, { key: "s3" }] },
    { id: "ai", icon: Bot, page: "/ai/connections", steps: [{ key: "s1", code: mcpUrl }, { key: "s2" }, { key: "s3" }] },
    { id: "agents", icon: Plug, admin: true, page: "/admin/agents", steps: [{ key: "s1" }, { key: "s2" }, { key: "s3" }] },
    { id: "mail", icon: Mail, admin: true, page: "/admin/mail", steps: [{ key: "s1" }, { key: "s2" }] },
    { id: "app_backups", icon: DatabaseBackup, admin: true, page: "/admin/backups", steps: [{ key: "s1" }, { key: "s2" }, { key: "s3" }] },
  ];
}

function status(id: string, o: Overview | undefined, t: (k: string, v?: Record<string, unknown>) => string, lang: string): { tone: Tone; badge: string; line: string } {
  if (!o) return { tone: "neutral", badge: "…", line: "" };
  const none = { tone: "neutral" as Tone, badge: t("intg.not_set") };
  switch (id) {
    case "github":
      return o.github.configured
        ? { tone: "online", badge: t("intg.connected"), line: t("intg.github.line", { login: o.github.login ?? "?", ago: timeAgo(o.github.checked_at, lang) }) }
        : { ...none, line: t("intg.github.line_none") };
    case "m365":
      return o.m365.tenants
        ? { tone: o.m365.errors ? "warning" : "online", badge: o.m365.errors ? t("intg.errors", { count: o.m365.errors }) : t("intg.connected"), line: t("intg.m365.line", { count: o.m365.tenants, customers: o.m365.customers, total: o.customers }) }
        : { ...none, line: t("intg.m365.line_none") };
    case "unifi":
      return o.unifi.connections
        ? { tone: o.unifi.errors ? "warning" : "online", badge: o.unifi.errors ? t("intg.errors", { count: o.unifi.errors }) : t("intg.connected"), line: t("intg.unifi.line", { count: o.unifi.connections, customers: o.unifi.customers }) }
        : { ...none, line: t("intg.unifi.line_none") };
    case "mcp":
      return o.mcp.servers
        ? { tone: o.mcp.errors ? "warning" : "online", badge: o.mcp.errors ? t("intg.errors", { count: o.mcp.errors }) : t("intg.active", { count: o.mcp.enabled }), line: t("intg.mcp.line", { count: o.mcp.servers }) }
        : { ...none, line: t("intg.mcp.line_none") };
    case "ai":
      return o.ai.tokens
        ? { tone: "online", badge: t("intg.connected"), line: t("intg.ai.line", { count: o.ai.tokens }) }
        : { ...none, line: t("intg.ai.line_none") };
    case "agents":
      return o.agents.machines
        ? { tone: o.agents.online < o.agents.machines ? "warning" : "online", badge: `${o.agents.online} / ${o.agents.machines}`, line: t("intg.agents.line", { online: o.agents.online, count: o.agents.machines }) }
        : { ...none, line: t("intg.agents.line_none") };
    case "mail":
      return o.mail.enabled
        ? { tone: "online", badge: t("intg.connected"), line: t("intg.mail.line", { host: o.mail.host ?? "" }) }
        : { tone: "warning", badge: t("intg.not_set"), line: t("intg.mail.line_none") };
    case "app_backups":
      return o.app_backups.targets
        ? { tone: "online", badge: t("intg.connected"), line: t("intg.app_backups.line", { count: o.app_backups.targets }) }
        : { tone: "warning", badge: t("intg.not_set"), line: t("intg.app_backups.line_none") };
  }
  return { tone: "neutral", badge: "", line: "" };
}

export function IntegrationsPage() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const o = useIntegrations();
  const [params, setParams] = useSearchParams();
  const list = defs(appUrl("/mcp")).filter((d) => !d.admin || admin);
  const open = list.find((d) => d.id === params.get("setup"));
  return (
    <div>
      <PageHeader title={t("intg.title")} description={t("intg.desc")} />
      {o.isLoading ? <Spinner /> : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {list.map((d) => {
            const s = status(d.id, o.data, t, i18n.language);
            const Icon = d.icon;
            return (
              <Card key={d.id} className="flex flex-col p-5">
                <div className="flex items-start gap-3">
                  <div className="grid size-10 shrink-0 place-items-center rounded-md bg-sunken [&_svg]:size-5"><Icon /></div>
                  <div className="min-w-0 flex-1">
                    <h2 className="text-[15px] font-bold leading-6">{t(`intg.${d.id}.title`)}</h2>
                    <p className="text-sm text-muted">{t(`intg.${d.id}.what`)}</p>
                  </div>
                  <Badge tone={s.tone} dot>{s.badge}</Badge>
                </div>
                <p className="mt-3 flex-1 text-sm">{s.line}</p>
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button size="sm" variant={s.tone === "online" ? "secondary" : "primary"} onClick={() => setParams({ setup: d.id })}>
                    {s.tone === "online" ? t("intg.guide") : t("intg.setup")}
                  </Button>
                  {d.page && <Link to={d.page} className="inline-flex items-center gap-1 px-2 text-sm font-semibold text-accent-text hover:underline">{t("intg.open")} <ArrowRight className="size-3.5" /></Link>}
                </div>
              </Card>
            );
          })}
        </div>
      )}
      {open && <Wizard def={open} onClose={() => setParams({}, { replace: true })} />}
    </div>
  );
}

/** Step-by-step guide for one integration. */
export function Wizard({ def, onClose }: { def: IntegrationDef; onClose: () => void }) {
  const { t } = useTranslation();
  const total = def.steps.length + 1;
  const [step, setStep] = useState(0);
  const last = step === def.steps.length;
  const s = def.steps[step];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()} wide title={t("intg.wizard_title", { name: t(`intg.${def.id}.title`) })}
      description={t("intg.step_of", { n: step + 1, total })}
      footer={<>
        {step > 0 && <Button onClick={() => setStep(step - 1)}>{t("intg.prev")}</Button>}
        {!last ? <Button variant="primary" onClick={() => setStep(step + 1)}>{t("intg.next")}</Button>
          : <Button variant="primary" onClick={onClose}>{t("intg.done")}</Button>}
      </>}>
      <div className="grid gap-5 sm:grid-cols-[180px_minmax(0,1fr)]">
        <ol className="hidden space-y-1 sm:block">
          {[...def.steps.map((x) => t(`intg.${def.id}.${x.key}_title`)), t("intg.finish_title")].map((label, i) => (
            <li key={i}>
              <button type="button" onClick={() => setStep(i)}
                className={cn("flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px]", i === step ? "bg-sunken font-bold" : "text-muted hover:bg-sunken")}>
                <span className={cn("grid size-5 shrink-0 place-items-center rounded-full border text-[11px] font-bold",
                  i < step ? "border-transparent bg-online-soft text-online" : i === step ? "border-nacht bg-nacht text-white dark:border-lucht dark:bg-lucht dark:text-nacht" : "border-line")}>
                  {i < step ? <Check className="size-3" /> : i + 1}
                </span>
                <span className="min-w-0 truncate">{label}</span>
              </button>
            </li>
          ))}
        </ol>
        <div className="min-w-0">
          {!last && s ? (
            <>
              <h3 className="mb-2 text-base font-bold">{t(`intg.${def.id}.${s.key}_title`)}</h3>
              <ul className="list-disc space-y-1.5 pl-5 text-sm leading-relaxed">
                {t(`intg.${def.id}.${s.key}_text`).split("\n").map((line, i) => <li key={i}>{line}</li>)}
              </ul>
              {s.code && (
                <div className="mt-3 flex items-center gap-2 rounded-md bg-sunken px-3 py-2">
                  <code className="mono min-w-0 flex-1 truncate text-[13px]">{s.code}</code>
                  <CopyButton value={s.code} />
                </div>
              )}
              {s.link && (
                <a href={s.link.href} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1.5 text-sm font-semibold text-accent-text hover:underline">
                  {s.link.label} <ExternalLink className="size-3.5" />
                </a>
              )}
            </>
          ) : (
            <>
              <h3 className="mb-2 text-base font-bold">{t("intg.finish_title")}</h3>
              {def.finish ?? (
                <>
                  <p className="text-sm text-muted">{t(`intg.${def.id}.finish`)}</p>
                  {def.page && <Link to={def.page} onClick={onClose} className="mt-4 inline-flex"><Button variant="primary">{t("intg.open_page", { name: t(`intg.${def.id}.title`) })} <ArrowRight /></Button></Link>}
                </>
              )}
              {def.id === "github" && <Alert tone="info" className="mt-4">{t("intg.github.after")}</Alert>}
            </>
          )}
        </div>
      </div>
    </Dialog>
  );
}
