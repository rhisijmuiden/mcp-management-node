import * as Tabs from "@radix-ui/react-tabs";
import { ArrowUpCircle, BookOpen, Bot, Cpu, DatabaseBackup, FileText, Globe, Mail, Network, Plug, Rocket, ScrollText, Server, ShieldCheck, Terminal } from "lucide-react";
import type { ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";
import { CodeBlock, CopyButton } from "@/components/CopyButton";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Alert, PageHeader } from "@/components/ui/misc";
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
    { icon: <Bot />, title: t("help.topic_mcp"), text: t("help.topic_mcp_text"), to: "/help/mcp" },
    { icon: <Terminal />, title: t("help.topic_ai_admin"), text: t("help.topic_ai_admin_text"), to: "/help/ai-admin" },
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

const CAPS = [
  { key: "status", icon: <Server />, scope: "read" },
  { key: "logs", icon: <ScrollText />, scope: "logs" },
  { key: "packages", icon: <ShieldCheck />, scope: "logs" },
  { key: "files", icon: <FileText />, scope: "logs" },
  { key: "services", icon: <Cpu />, scope: "services" },
  { key: "exec", icon: <Terminal />, scope: "exec" },
  { key: "updates", icon: <ArrowUpCircle />, scope: "node_updates" },
  { key: "config", icon: <Mail />, scope: "nodes" },
  { key: "app", icon: <DatabaseBackup />, scope: "backups" },
];

export function CapabilitiesHelpPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("caps.title")} description={t("caps.subtitle")} />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card className="bg-nacht text-white">
            <CardBody className="space-y-3 py-5 text-sm leading-6 text-white/85">
              <div className="display text-xl text-white">{t("caps.idea_title")}</div>
              <p>{t("caps.idea_1")}</p>
              <p>{t("caps.idea_2")}</p>
            </CardBody>
          </Card>
          <div className="grid gap-4 md:grid-cols-2">
            {CAPS.map((cp) => (
              <Card key={cp.key}>
                <CardHeader icon={cp.icon} title={t(`caps.${cp.key}_title`)} action={<code className="mono rounded bg-sunken px-1.5 py-0.5 text-[11px]">{cp.scope}</code>} />
                <CardBody className="space-y-2 text-sm">
                  <p className="text-muted">{t(`caps.${cp.key}_text`)}</p>
                  <p className="rounded-md bg-sunken px-3 py-2 italic">“{t(`caps.${cp.key}_example`)}”</p>
                </CardBody>
              </Card>
            ))}
          </div>
          <Card>
            <CardHeader icon={<Network />} title={t("caps.soon_title")} />
            <CardBody className="text-sm text-muted">{t("caps.soon_text")}</CardBody>
          </Card>
        </div>
        <div className="space-y-6">
          <Card>
            <CardHeader icon={<ShieldCheck />} title={t("caps.safety_title")} />
            <CardBody>
              <ul className="space-y-2 text-sm">
                {[1, 2, 3, 4, 5].map((i) => <li key={i} className="flex gap-2"><span className="mt-2 size-1.5 shrink-0 rounded-full bg-koraal" />{t(`caps.safety_${i}`)}</li>)}
              </ul>
              <div className="mt-4 flex flex-wrap gap-3 text-sm font-semibold">
                <Link to="/ai/activity" className="text-accent-text hover:underline">{t("nav.ai_activity")} →</Link>
                <Link to="/help/mcp" className="text-accent-text hover:underline">{t("help.topic_mcp")} →</Link>
              </div>
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={t("caps.scopes_title")} />
            <CardBody className="space-y-2 text-sm">
              {["read", "logs", "services", "exec", "node_updates", "nodes"].map((s) => (
                <div key={s} className="flex gap-2"><code className="mono h-fit rounded bg-sunken px-1.5 py-0.5 text-[11px]">{s}</code><span className="text-muted">{t(`caps.scope_${s}`)}</span></div>
              ))}
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
