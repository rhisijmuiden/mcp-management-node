import * as Tabs from "@radix-ui/react-tabs";
import { Activity, ArrowUpCircle, BookOpen, Bot, Boxes, CalendarClock, DatabaseBackup, GitBranch, Layers, Globe, Mail, MessageSquare, Network, Plug, Rocket, ScrollText, Server, ShieldCheck, Sparkles, Terminal, Wifi } from "lucide-react";
import type { ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";
import { CodeBlock, CopyButton } from "@/components/CopyButton";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Alert, Badge, PageHeader } from "@/components/ui/misc";
import { appUrl } from "@/lib/utils";

function Steps({ items }: { items: ReactNode[] }) {
  return (
    <ol className="space-y-3">
      {items.map((it, i) => (
        <li key={i} className="flex gap-3">
          <span className="grid size-6 shrink-0 place-items-center rounded-full bg-nacht text-xs font-bold text-white dark:bg-lucht dark:text-nacht">{i + 1}</span>
          <div className="pt-0.5 text-sm leading-6">{it}</div>
        </li>
      ))}
    </ol>
  );
}

const b = <strong className="font-semibold" />;
const c = <code className="mono rounded bg-sunken px-1 py-0.5 text-[12.5px]" />;

export function HelpPage() {
  const { t } = useTranslation();
  const topics = [
    { icon: <Sparkles />, title: t("help.topic_caps"), text: t("help.topic_caps_text"), to: "/help/capabilities" },
    { icon: <Bot />, title: t("help.topic_mcp"), text: t("help.topic_mcp_text"), to: "/help/mcp" },
    { icon: <Plug />, title: t("help.topic_nodes"), text: t("help.topic_nodes_text"), to: "/help/nodes" },
    { icon: <DatabaseBackup />, title: t("help.topic_backups"), text: t("help.topic_backups_text"), to: "/admin/backups" },
    { icon: <Rocket />, title: t("help.topic_updates"), text: t("help.topic_updates_text") },
    { icon: <Globe />, title: t("help.topic_proxy"), text: t("help.topic_proxy_text"), to: "/admin/web-server" },
    { icon: <ShieldCheck />, title: t("help.topic_security"), text: t("help.topic_security_text") },
  ];
  return (
    <>
      <PageHeader title={t("help.title")} description={t("help.subtitle")} />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {topics.map((tp) => {
          const body = (
            <Card className="h-full transition-shadow hover:shadow-lg">
              <CardHeader icon={tp.icon} title={tp.title} />
              <CardBody className="text-sm text-muted">{tp.text}</CardBody>
            </Card>
          );
          return tp.to ? <Link key={tp.title} to={tp.to}>{body}</Link> : <div key={tp.title}>{body}</div>;
        })}
      </div>
    </>
  );
}

export function McpHelpPage() {
  const { t } = useTranslation();
  const url = appUrl("/mcp");
  const isLocal = /^(http:\/\/)?(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(url.replace(/^https?:\/\//, "")) || url.startsWith("http://");
  const tab = "rounded-[8px] px-3 py-1.5 text-[13px] font-semibold text-muted data-[state=active]:bg-raised data-[state=active]:text-ink data-[state=active]:shadow-sm cursor-pointer";
  const claudeCode = `claude mcp add --transport http mcp-node ${url}`;
  const claudeCodeToken = `claude mcp add --transport http mcp-node ${url} \\\n  --header "Authorization: Bearer mmn_YOUR_TOKEN"`;
  const vscode = JSON.stringify({ servers: { "mcp-node": { type: "http", url } } }, null, 2);
  const vscodeToken = JSON.stringify(
    {
      inputs: [{ type: "promptString", id: "mmn-token", description: "MCP Management Node API token", password: true }],
      servers: { "mcp-node": { type: "http", url, headers: { Authorization: "Bearer ${input:mmn-token}" } } },
    },
    null,
    2,
  );
  const desktop = JSON.stringify(
    { mcpServers: { "mcp-node": { command: "npx", args: ["-y", "mcp-remote", url, "--header", "Authorization: Bearer ${MMN_TOKEN}"], env: { MMN_TOKEN: "mmn_YOUR_TOKEN" } } } },
    null,
    2,
  );
  const curl = `curl -s ${url} \\\n  -H "Authorization: Bearer mmn_YOUR_TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -H "Accept: application/json, text/event-stream" \\\n  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`;

  return (
    <>
      <PageHeader title={t("mcphelp.title")} description={t("mcphelp.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card>
            <CardBody className="flex flex-wrap items-center justify-between gap-4 py-5">
              <div>
                <div className="text-[13px] font-semibold text-muted">{t("ai.endpoint")}</div>
                <div className="mono mt-1 break-all text-[15px]">{url}</div>
              </div>
              <CopyButton value={url} />
            </CardBody>
          </Card>
          {isLocal && <Alert tone="warning" title={t("mcphelp.local_title")}>{t("mcphelp.local_text")}</Alert>}
          <Alert tone="info" title={t("help.topic_caps")}>
            {t("help.topic_caps_text")}{" "}
            <Link to="/help/capabilities" className="font-semibold text-accent-text hover:underline">{t("mcpcaps.read_more")} →</Link>
          </Alert>

          <Tabs.Root defaultValue="claude">
            <Tabs.List className="mb-4 inline-flex flex-wrap rounded-md border border-line bg-sunken p-0.5">
              <Tabs.Trigger value="claude" className={tab}>Claude</Tabs.Trigger>
              <Tabs.Trigger value="claude-code" className={tab}>Claude Code</Tabs.Trigger>
              <Tabs.Trigger value="chatgpt" className={tab}>ChatGPT</Tabs.Trigger>
              <Tabs.Trigger value="copilot" className={tab}>GitHub Copilot</Tabs.Trigger>
              <Tabs.Trigger value="other" className={tab}>{t("mcphelp.other")}</Tabs.Trigger>
            </Tabs.List>

            <Tabs.Content value="claude">
              <Card>
                <CardHeader icon={<Bot />} title={t("mcphelp.claude_title")} description={t("mcphelp.claude_desc")} />
                <CardBody className="space-y-5">
                  <Steps items={[
                    <Trans i18nKey="mcphelp.claude_1" components={{ b }} />,
                    <Trans i18nKey="mcphelp.claude_2" components={{ b }} />,
                    <span><Trans i18nKey="mcphelp.claude_3" components={{ b }} /> <code className="mono break-all rounded bg-sunken px-1 py-0.5 text-[12.5px]">{url}</code></span>,
                    <Trans i18nKey="mcphelp.claude_4" components={{ b }} />,
                    <Trans i18nKey="mcphelp.claude_5" components={{ b }} />,
                  ]} />
                  <Alert tone="info">{t("mcphelp.claude_desktop_note")}</Alert>
                  <details className="rounded-md border border-line p-4">
                    <summary className="cursor-pointer text-sm font-semibold">{t("mcphelp.claude_desktop_token")}</summary>
                    <p className="my-3 text-sm text-muted"><Trans i18nKey="mcphelp.claude_desktop_token_text" components={{ c }} /></p>
                    <CodeBlock code={desktop} />
                  </details>
                </CardBody>
              </Card>
            </Tabs.Content>

            <Tabs.Content value="claude-code">
              <Card>
                <CardHeader icon={<Bot />} title="Claude Code" description={t("mcphelp.cc_desc")} />
                <CardBody className="space-y-5">
                  <Steps items={[
                    <span>{t("mcphelp.cc_1")}<div className="mt-2"><CodeBlock code={claudeCode} /></div></span>,
                    <Trans i18nKey="mcphelp.cc_2" components={{ b, c }} />,
                    <Trans i18nKey="mcphelp.cc_3" components={{ b }} />,
                  ]} />
                  <details className="rounded-md border border-line p-4">
                    <summary className="cursor-pointer text-sm font-semibold">{t("mcphelp.with_token")}</summary>
                    <p className="my-3 text-sm text-muted">{t("mcphelp.with_token_text")}</p>
                    <CodeBlock code={claudeCodeToken} />
                  </details>
                </CardBody>
              </Card>
            </Tabs.Content>

            <Tabs.Content value="chatgpt">
              <Card>
                <CardHeader icon={<Bot />} title={t("mcphelp.chatgpt_title")} description={t("mcphelp.chatgpt_desc")} />
                <CardBody className="space-y-5">
                  <Steps items={[
                    <Trans i18nKey="mcphelp.chatgpt_1" components={{ b }} />,
                    <Trans i18nKey="mcphelp.chatgpt_2" components={{ b }} />,
                    <span><Trans i18nKey="mcphelp.chatgpt_3" components={{ b }} /> <code className="mono break-all rounded bg-sunken px-1 py-0.5 text-[12.5px]">{url}</code></span>,
                    <Trans i18nKey="mcphelp.chatgpt_4" components={{ b }} />,
                    <Trans i18nKey="mcphelp.chatgpt_5" components={{ b }} />,
                  ]} />
                  <Alert tone="info">{t("mcphelp.chatgpt_note")}</Alert>
                </CardBody>
              </Card>
            </Tabs.Content>

            <Tabs.Content value="copilot">
              <Card>
                <CardHeader icon={<Bot />} title={t("mcphelp.copilot_title")} description={t("mcphelp.copilot_desc")} />
                <CardBody className="space-y-5">
                  <Steps items={[
                    <span><Trans i18nKey="mcphelp.copilot_1" components={{ b, c }} /><div className="mt-2"><CodeBlock code={vscode} /></div></span>,
                    <Trans i18nKey="mcphelp.copilot_2" components={{ b }} />,
                    <Trans i18nKey="mcphelp.copilot_3" components={{ b }} />,
                    <Trans i18nKey="mcphelp.copilot_4" components={{ b }} />,
                  ]} />
                  <details className="rounded-md border border-line p-4">
                    <summary className="cursor-pointer text-sm font-semibold">{t("mcphelp.with_token")}</summary>
                    <p className="my-3 text-sm text-muted">{t("mcphelp.copilot_token_text")}</p>
                    <CodeBlock code={vscodeToken} />
                  </details>
                </CardBody>
              </Card>
            </Tabs.Content>

            <Tabs.Content value="other">
              <Card>
                <CardHeader icon={<BookOpen />} title={t("mcphelp.other_title")} description={t("mcphelp.other_desc")} />
                <CardBody className="space-y-4 text-sm">
                  <ul className="list-disc space-y-1 pl-5">
                    <li><Trans i18nKey="mcphelp.other_1" components={{ b, c }} /></li>
                    <li><Trans i18nKey="mcphelp.other_2" components={{ b, c }} /></li>
                    <li><Trans i18nKey="mcphelp.other_3" components={{ b, c }} /></li>
                  </ul>
                  <CodeBlock code={curl} />
                </CardBody>
              </Card>
            </Tabs.Content>
          </Tabs.Root>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("mcphelp.safety_title")} />
            <CardBody>
              <ul className="space-y-2 text-sm">
                {[1, 2, 3, 4].map((i) => <li key={i} className="flex gap-2"><span className="mt-2 size-1.5 shrink-0 rounded-full bg-koraal" />{t(`mcphelp.safety_${i}`)}</li>)}
              </ul>
              <div className="mt-4 flex flex-wrap gap-3 text-sm font-semibold">
                <Link to="/ai/activity" className="text-accent-text hover:underline">{t("nav.ai_activity")} →</Link>
                <Link to="/ai/tokens" className="text-accent-text hover:underline">{t("nav.api_tokens")} →</Link>
              </div>
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={t("mcphelp.try_title")} />
            <CardBody>
              <ul className="space-y-2 text-sm text-muted">
                {[1, 2, 3, 4].map((i) => <li key={i} className="rounded-md bg-sunken px-3 py-2 italic">“{t(`mcphelp.try_${i}`)}”</li>)}
              </ul>
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}

type Cap = { tool: string; scope: "read" | "backups" | "restore" | "updates" | "settings" | "nodes" | "node_updates" | "logs" | "services" | "exec" | "guests" | "guests_admin" | "templates" | "tasks" | "network" | "cluster" | "deploy" };
const NOW: { key: string; icon: ReactNode; tools: Cap[] }[] = [
  { key: "system", icon: <Rocket />, tools: [
    { tool: "get_system_info", scope: "read" }, { tool: "check_for_updates", scope: "read" }, { tool: "get_update_status", scope: "read" },
    { tool: "set_update_channel", scope: "updates" }, { tool: "install_update", scope: "updates" },
  ] },
  { key: "backups", icon: <DatabaseBackup />, tools: [
    { tool: "list_backup_targets", scope: "read" }, { tool: "list_backup_runs", scope: "read" }, { tool: "list_remote_backups", scope: "read" },
    { tool: "run_backup", scope: "backups" }, { tool: "restore_backup", scope: "restore" },
  ] },
  { key: "mail", icon: <Mail />, tools: [
    { tool: "get_mail_settings", scope: "read" }, { tool: "update_mail_settings", scope: "settings" }, { tool: "send_test_mail", scope: "settings" },
  ] },
  { key: "audit", icon: <ScrollText />, tools: [{ tool: "get_audit_log", scope: "read" }] },
  { key: "proxmox", icon: <Server />, tools: [
    { tool: "get_overview", scope: "read" }, { tool: "list_environments", scope: "read" }, { tool: "list_nodes", scope: "read" },
    { tool: "get_node", scope: "read" }, { tool: "create_environment", scope: "nodes" }, { tool: "create_pairing_code", scope: "nodes" },
    { tool: "set_cpu_governor", scope: "nodes" }, { tool: "get_environment_notifications", scope: "read" },
    { tool: "update_environment_notifications", scope: "nodes" }, { tool: "send_node_test_notification", scope: "nodes" },
    { tool: "update_node_agent", scope: "nodes" },
  ] },
  { key: "node_updates", icon: <ArrowUpCircle />, tools: [
    { tool: "refresh_node_updates", scope: "read" }, { tool: "get_node_job", scope: "read" },
    { tool: "install_node_updates", scope: "node_updates" }, { tool: "reboot_node", scope: "node_updates" },
  ] },
  { key: "servers", icon: <Terminal />, tools: [
    { tool: "read_logs", scope: "logs" }, { tool: "find_packages", scope: "logs" }, { tool: "read_file", scope: "logs" },
    { tool: "list_services", scope: "logs" }, { tool: "get_service_status", scope: "logs" },
    { tool: "manage_service", scope: "services" }, { tool: "run_command", scope: "exec" },
  ] },
  { key: "guests", icon: <Boxes />, tools: [
    { tool: "list_guests", scope: "read" }, { tool: "get_guest", scope: "read" }, { tool: "list_snapshots", scope: "read" },
    { tool: "suggest_node", scope: "read" }, { tool: "get_node_resources", scope: "read" },
    { tool: "guest_power", scope: "guests" }, { tool: "create_snapshot", scope: "guests" }, { tool: "set_guest_resources", scope: "guests" },
    { tool: "rollback_snapshot", scope: "guests_admin" }, { tool: "delete_snapshot", scope: "guests_admin" }, { tool: "migrate_guest", scope: "guests_admin" },
    { tool: "delete_guest", scope: "guests_admin" }, { tool: "create_container", scope: "guests_admin" }, { tool: "create_vm", scope: "guests_admin" },
    { tool: "run_in_container", scope: "exec" },
  ] },
  { key: "library", icon: <Layers />, tools: [
    { tool: "list_templates", scope: "read" }, { tool: "list_template_catalog", scope: "read" },
    { tool: "add_official_template", scope: "templates" }, { tool: "sync_templates", scope: "templates" },
    { tool: "list_community_scripts", scope: "read" }, { tool: "get_community_script", scope: "read" },
    { tool: "install_community_script", scope: "guests_admin" },
  ] },
  { key: "tasks", icon: <CalendarClock />, tools: [
    { tool: "list_scheduled_tasks", scope: "read" }, { tool: "get_task_run", scope: "read" },
    { tool: "save_scheduled_task", scope: "tasks" }, { tool: "delete_scheduled_task", scope: "tasks" }, { tool: "run_scheduled_task", scope: "tasks" },
  ] },
  { key: "network", icon: <Network />, tools: [
    { tool: "get_bridge_map", scope: "read" }, { tool: "get_node_network", scope: "read" }, { tool: "get_firewall", scope: "read" },
    { tool: "get_sdn", scope: "read" }, { tool: "list_sdn_designs", scope: "read" }, { tool: "list_config_backups", scope: "read" },
    { tool: "stage_bridge", scope: "network" }, { tool: "apply_network_changes", scope: "network" }, { tool: "revert_network_changes", scope: "network" },
    { tool: "set_firewall", scope: "network" }, { tool: "add_firewall_rule", scope: "network" }, { tool: "delete_firewall_rule", scope: "network" },
    { tool: "save_sdn_design", scope: "network" }, { tool: "apply_sdn_design", scope: "network" }, { tool: "restore_config_backup", scope: "network" },
  ] },
  { key: "cluster", icon: <Server />, tools: [
    { tool: "get_ha_status", scope: "read" }, { tool: "list_replication", scope: "read" }, { tool: "list_backup_jobs", scope: "read" }, { tool: "list_storage", scope: "read" },
    { tool: "set_ha_resource", scope: "cluster" }, { tool: "create_ha_rule", scope: "cluster" }, { tool: "manage_replication", scope: "cluster" },
    { tool: "save_backup_job", scope: "cluster" }, { tool: "run_backup_job", scope: "cluster" }, { tool: "delete_backup_job", scope: "cluster" },
    { tool: "add_storage", scope: "cluster" }, { tool: "set_storage_enabled", scope: "cluster" },
  ] },
  { key: "monitoring", icon: <Activity />, tools: [
    { tool: "get_monitoring", scope: "read" }, { tool: "get_node_metrics", scope: "read" }, { tool: "list_alerts", scope: "read" }, { tool: "get_disk_smart", scope: "read" },
  ] },
  { key: "deploy", icon: <GitBranch />, tools: [
    { tool: "list_deployments", scope: "read" }, { tool: "get_deployment", scope: "read" },
    { tool: "list_github_repos", scope: "deploy" }, { tool: "save_deployment", scope: "deploy" }, { tool: "run_deployment", scope: "deploy" },
  ] },
];
const NEXT: { key: string; icon: ReactNode; items: number }[] = [
  { key: "servers", icon: <Terminal />, items: 3 },
  { key: "unifi", icon: <Wifi />, items: 3 },
];

export function McpCapabilitiesPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("mcpcaps.title")} description={t("mcpcaps.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card className="bg-nacht text-white dark:bg-raised">
            <CardBody className="space-y-3 py-6">
              <div className="display text-xl">{t("mcpcaps.idea_title")}</div>
              <p className="text-sm leading-6 text-white/80 dark:text-muted">{t("mcpcaps.idea_1")}</p>
              <p className="text-sm leading-6 text-white/80 dark:text-muted">{t("mcpcaps.idea_2")}</p>
            </CardBody>
          </Card>

          <div>
            <div className="mb-3 flex items-center gap-2">
              <h2 className="display text-lg">{t("mcpcaps.now_title")}</h2>
              <Badge tone="online" dot>{t("mcpcaps.available")}</Badge>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              {NOW.map((g) => (
                <Card key={g.key}>
                  <CardHeader icon={g.icon} title={t(`mcpcaps.now_${g.key}`)} description={t(`mcpcaps.now_${g.key}_desc`)} />
                  <CardBody className="space-y-2">
                    {g.tools.map((c) => (
                      <div key={c.tool} className="flex items-center justify-between gap-3 text-sm">
                        <code className="mono truncate text-[12.5px]">{c.tool}</code>
                        <Badge tone={c.scope === "read" ? "neutral" : c.scope === "restore" || c.scope === "exec" ? "offline" : "accent"}>{t(`scopes.${c.scope}`)}</Badge>
                      </div>
                    ))}
                  </CardBody>
                </Card>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-3 flex items-center gap-2">
              <h2 className="display text-lg">{t("mcpcaps.next_title")}</h2>
              <Badge tone="info">{t("nav.soon")}</Badge>
            </div>
            <p className="mb-4 text-sm text-muted">{t("mcpcaps.next_intro")}</p>
            <div className="grid gap-4 md:grid-cols-2">
              {NEXT.map((g) => (
                <Card key={g.key}>
                  <CardHeader icon={g.icon} title={t(`mcpcaps.next_${g.key}`)} description={t(`mcpcaps.next_${g.key}_when`)} />
                  <CardBody>
                    <ul className="space-y-2 text-sm leading-6">
                      {Array.from({ length: g.items }, (_, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="mt-2 size-1.5 shrink-0 rounded-full bg-koraal" />
                          <span>{t(`mcpcaps.next_${g.key}_${i + 1}`)}</span>
                        </li>
                      ))}
                    </ul>
                  </CardBody>
                </Card>
              ))}
            </div>
          </div>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader icon={<MessageSquare />} title={t("mcpcaps.ask_title")} description={t("mcpcaps.ask_desc")} />
            <CardBody className="space-y-2">
              {[1, 2, 3, 4, 5, 6, 7, 9, 10, 8].map((i) => (
                <div key={i} className="rounded-md bg-sunken px-3 py-2 text-sm">
                  <div className="italic">“{t(`mcpcaps.ask_${i}`)}”</div>
                  <div className="mt-1 text-xs text-muted">{t(i !== 8 ? "mcpcaps.ask_now" : "mcpcaps.ask_later")}</div>
                </div>
              ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("mcpcaps.trail_title")} />
            <CardBody>
              <ul className="space-y-2 text-sm leading-6">
                {[1, 2, 3, 4].map((i) => (
                  <li key={i} className="flex gap-2">
                    <span className="mt-2 size-1.5 shrink-0 rounded-full bg-koraal" />
                    <span>{t(`mcpcaps.trail_${i}`)}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-4 flex flex-wrap gap-4 text-sm font-semibold">
                <Link to="/ai/activity" className="text-accent-text hover:underline">{t("nav.ai_activity")} →</Link>
                <Link to="/help/mcp" className="text-accent-text hover:underline">{t("help.topic_mcp")} →</Link>
              </div>
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}

export function NodesHelpPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("nodeshelp.title")} description={t("nodeshelp.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader icon={<Plug />} title={t("nodeshelp.steps_title")} />
          <CardBody>
            <Steps items={[1, 2, 3, 4, 5].map((i) => <Trans key={i} i18nKey={`nodeshelp.step_${i}`} components={{ b, c }} />)} />
          </CardBody>
        </Card>
        <div className="space-y-6">
          <Card>
            <CardHeader icon={<Network />} title={t("nodeshelp.network_title")} />
            <CardBody className="space-y-2 text-sm text-muted">
              <p><Trans i18nKey="nodeshelp.network_1" components={{ b, c }} /></p>
              <p><Trans i18nKey="nodeshelp.network_2" components={{ b, c }} /></p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("nodeshelp.security_title")} />
            <CardBody className="text-sm text-muted">{t("nodeshelp.security_text")}</CardBody>
          </Card>
          <Alert tone="info">{t("nodeshelp.files")}</Alert>
        </div>
      </div>
    </>
  );
}
