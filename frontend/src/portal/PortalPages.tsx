import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Clock, HelpCircle, Inbox, Laptop, LifeBuoy, MessageSquare, Plus, Send, Server, Sparkles, UserRound, Wrench } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Field, Input, Segmented, Select, Textarea } from "@/components/ui/form";
import { Alert, Badge, EmptyState, PageHeader, Spinner, Stat } from "@/components/ui/misc";
import { errorMessage } from "@/lib/api";
import { cn, formatDate, timeAgo } from "@/lib/utils";
import { PriorityBadge, StatusBadge, type Priority, type TicketStatus } from "@/pages/Tickets";
import { pget, ppatch, ppost } from "./api";
import { usePortal } from "./PortalApp";

interface PTicket {
  id: string; number: number; ref: string; subject: string; category: "problem" | "question" | "request"; priority: Priority;
  status: TicketStatus; machines: { id: string; name: string | null }[]; reporter_name: string | null; created_at: string;
  updated_at: string; resolved_at: string | null; messages: number; can_reply: boolean;
}
interface PMessage { id: string; author_kind: "customer" | "staff" | "ai" | "system"; author_name: string | null; body: string; created_at: string; event: { kind?: string; to?: string } }
interface PMachine { id: string; name: string; type: "server" | "workstation"; online: boolean }

const CATS = [
  { value: "problem", icon: <Wrench /> },
  { value: "question", icon: <HelpCircle /> },
  { value: "request", icon: <Sparkles /> },
] as const;

/* ------------------------------------------------------------------ home: my tickets */
export function PortalHomePage() {
  const { t, i18n } = useTranslation();
  const { me } = usePortal();
  const [state, setState] = useState<"active" | "closed" | "all">("active");
  const q = useQuery({
    queryKey: ["portal-tickets", state],
    queryFn: () => pget<{ tickets: PTicket[]; summary: { active: number; waiting_on_you: number; resolved: number } }>(`tickets?state=${state}`),
    refetchInterval: 60_000,
  });
  const s = q.data?.summary;
  const first = (me?.full_name || "").split(" ")[0];
  return (
    <div>
      <PageHeader title={first ? t("portal.home.hello", { name: first }) : t("portal.home.title")}
        description={me?.role === "manager" ? t("portal.home.desc_manager", { customer: me?.customer_name }) : t("portal.home.desc")}
        actions={<Link to="/portal/tickets/new"><Button variant="primary"><Plus /> {t("portal.nav.new")}</Button></Link>} />
      {me?.welcome && <Alert tone="info" className="mb-5"><span className="whitespace-pre-wrap">{me.welcome}</span></Alert>}
      <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-3 sm:gap-4">
        <Stat label={t("portal.home.k_active")} value={s?.active ?? "–"} hint={t("portal.home.k_active_hint")} tone="info" icon={<Inbox />} />
        <Stat label={t("portal.home.k_you")} value={s?.waiting_on_you ?? "–"} hint={t("portal.home.k_you_hint")} tone={s?.waiting_on_you ? "warning" : "neutral"} icon={<MessageSquare />} />
        <Stat label={t("portal.home.k_done")} value={s?.resolved ?? "–"} hint={t("portal.home.k_done_hint")} tone="online" icon={<CheckCircle2 />} />
      </div>
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
          <h2 className="text-[15px] font-bold">{t("portal.home.list")}</h2>
          <Segmented value={state} onChange={setState} options={[
            { value: "active", label: t("portal.home.f_active") }, { value: "closed", label: t("portal.home.f_closed") }, { value: "all", label: t("portal.home.f_all") },
          ]} />
        </div>
        {q.isLoading ? <div className="p-6"><Spinner /></div> : q.isError ? <div className="p-5"><Alert tone="offline">{errorMessage(q.error, t)}</Alert></div>
          : !(q.data?.tickets ?? []).length ? (
            <EmptyState icon={<LifeBuoy />} title={state === "active" ? t("portal.home.empty_active") : t("portal.home.empty")}
              action={<Link to="/portal/tickets/new"><Button variant="dark"><Plus /> {t("portal.nav.new")}</Button></Link>}>{t("portal.home.empty_hint")}</EmptyState>
          ) : (
            <ul className="divide-y divide-line">
              {q.data!.tickets.map((tk) => (
                <li key={tk.id}>
                  <Link to={`/portal/tickets/${tk.id}`} className="flex items-start gap-4 px-5 py-4 hover:bg-sunken/60 sm:items-center">
                    <span className="mono w-12 shrink-0 pt-0.5 text-sm text-muted sm:pt-0">{tk.ref}</span>
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2 font-semibold sm:line-clamp-1">{tk.subject}</span>
                      <span className="block text-xs text-muted">
                        {t(`portal.cat.${tk.category}`)}{tk.machines.length ? ` · ${tk.machines.map((m) => m.name).join(", ")}` : ""}
                        {me?.role === "manager" && tk.reporter_name ? ` · ${tk.reporter_name}` : ""}
                      </span>
                      <span className="mt-2 flex items-center gap-2 sm:hidden"><StatusBadge s={tk.status} portal /><span className="text-xs text-muted">{timeAgo(tk.updated_at, i18n.language)}</span></span>
                    </span>
                    <span className="hidden shrink-0 sm:block"><StatusBadge s={tk.status} portal /></span>
                    <span className="hidden w-28 shrink-0 text-right text-xs text-muted sm:block" title={formatDate(tk.updated_at, i18n.language)}>{timeAgo(tk.updated_at, i18n.language)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ new ticket */
export function PortalNewTicketPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const machines = useQuery({ queryKey: ["portal-machines"], queryFn: () => pget<PMachine[]>("machines") });
  const [f, setF] = useState({ category: "problem", subject: "", body: "", priority: "normal", node_ids: [] as string[] });
  const save = useMutation({
    mutationFn: () => ppost<PTicket>("tickets", f),
    onSuccess: (tk) => {
      qc.invalidateQueries({ queryKey: ["portal-tickets"] });
      toast.success(t("portal.new.done", { ref: tk.ref }));
      navigate(`/portal/tickets/${tk.id}`, { replace: true });
    },
  });
  const toggle = (id: string) => setF({ ...f, node_ids: f.node_ids.includes(id) ? f.node_ids.filter((x) => x !== id) : [...f.node_ids, id] });
  const list = machines.data ?? [];
  const groups: [string, PMachine[], ReactNode][] = [
    ["workstation", list.filter((m) => m.type === "workstation"), <Laptop key="w" />],
    ["server", list.filter((m) => m.type === "server"), <Server key="s" />],
  ];
  return (
    <div>
      <PageHeader title={t("portal.new.title")} description={t("portal.new.desc")} />
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }} className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card>
          <CardBody className="space-y-5">
            {save.isError && <Alert tone="offline">{errorMessage(save.error, t)}</Alert>}
            <div>
              <div className="mb-2 text-[13px] font-semibold">{t("portal.new.kind")}</div>
              <div className="grid gap-2 sm:grid-cols-3">
                {CATS.map((c) => (
                  <button type="button" key={c.value} onClick={() => setF({ ...f, category: c.value })}
                    className={cn("flex cursor-pointer items-start gap-3 rounded-md border p-3 text-left transition-colors [&_svg]:size-[18px]",
                      f.category === c.value ? "border-koraal bg-accent-soft" : "border-line hover:bg-sunken")}>
                    <span className={cn("mt-0.5", f.category === c.value ? "text-koraal" : "text-muted")}>{c.icon}</span>
                    <span><span className="block text-sm font-semibold">{t(`portal.cat.${c.value}`)}</span><span className="block text-xs text-muted">{t(`portal.cat.${c.value}_hint`)}</span></span>
                  </button>
                ))}
              </div>
            </div>
            <Field label={t("portal.new.subject")} hint={t("portal.new.subject_hint")}>
              <Input value={f.subject} maxLength={200} onChange={(e) => setF({ ...f, subject: e.target.value })} required autoFocus />
            </Field>
            <Field label={t("portal.new.body")} hint={t("portal.new.body_hint")}>
              <Textarea className="min-h-40 font-sans text-sm" value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} required />
            </Field>
            <Field label={t("portal.new.priority")} hint={t(`portal.prio_hint.${f.priority}`)}>
              <Select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })} className="sm:w-64">
                {(["low", "normal", "high", "urgent"] as const).map((p) => <option key={p} value={p}>{t(`tk.priority.${p}`)}</option>)}
              </Select>
            </Field>
          </CardBody>
        </Card>
        <div className="space-y-5">
          <Card>
            <CardHeader title={t("portal.new.machines")} description={t("portal.new.machines_hint")} />
            <CardBody className="space-y-4">
              {machines.isLoading ? <Spinner /> : !list.length ? <p className="text-sm text-muted">{t("portal.new.no_machines")}</p> : groups.filter(([, ms]) => ms.length).map(([k, ms, icon]) => (
                <div key={k}>
                  <div className="mb-1.5 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-muted [&_svg]:size-3.5">{icon}{t(`portal.new.group_${k}`)}</div>
                  <div className="space-y-1">
                    {ms.map((m) => (
                      <button type="button" key={m.id} onClick={() => toggle(m.id)}
                        className={cn("flex w-full cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors",
                          f.node_ids.includes(m.id) ? "border-koraal bg-accent-soft font-semibold" : "border-line hover:bg-sunken")}>
                        <span className={cn("size-2 shrink-0 rounded-full", m.online ? "bg-online" : "bg-line")} title={m.online ? t("tk.online") : t("tk.offline")} />
                        <span className="flex-1 truncate">{m.name}</span>
                        {f.node_ids.includes(m.id) && <CheckCircle2 className="size-4 text-koraal" />}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </CardBody>
          </Card>
          <Button variant="primary" size="lg" className="w-full" loading={save.isPending} disabled={!f.subject.trim() || !f.body.trim()}><Send /> {t("portal.new.submit")}</Button>
          <p className="text-xs text-muted">{t("portal.new.after")}</p>
        </div>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ one ticket */
export function PortalTicketPage() {
  const { id = "" } = useParams();
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const q = useQuery({ queryKey: ["portal-tickets", "one", id], queryFn: () => pget<PTicket & { thread: PMessage[] }>(`tickets/${id}`), refetchInterval: 60_000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["portal-tickets"] });
  const reply = useMutation({
    mutationFn: () => ppost(`tickets/${id}/messages`, { body }),
    onSuccess: () => { setBody(""); refresh(); toast.success(t("portal.ticket.sent")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const close = useMutation({
    mutationFn: () => ppost(`tickets/${id}/close`),
    onSuccess: () => { refresh(); toast.success(t("portal.ticket.closed_done")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  if (q.isLoading) return <Spinner />;
  if (q.isError || !q.data) return <Alert tone="offline">{errorMessage(q.error, t)}</Alert>;
  const tk = q.data;
  const hint = tk.status === "waiting_customer" ? "warning" : tk.status === "resolved" ? "online" : null;
  return (
    <div>
      <div className="mb-2 text-sm"><Link to="/portal" className="font-semibold text-accent-text hover:underline">← {t("portal.nav.tickets")}</Link></div>
      <PageHeader title={<span className="break-words"><span className="mono mr-2 text-muted">{tk.ref}</span>{tk.subject}</span>}
        description={<span className="flex flex-wrap items-center gap-2"><StatusBadge s={tk.status} portal /><PriorityBadge p={tk.priority} /><Badge>{t(`portal.cat.${tk.category}`)}</Badge><span>{t("portal.ticket.opened", { when: formatDate(tk.created_at, i18n.language) })}</span></span>}
        actions={tk.status !== "closed" && <Button onClick={() => close.mutate()} loading={close.isPending}>{t("portal.ticket.close")}</Button>} />
      {hint && <Alert tone={hint} className="mb-5">{t(`portal.ticket.hint_${tk.status}`)}</Alert>}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
        <Card>
          <CardBody className="space-y-3">
            {tk.thread.map((m) => m.author_kind === "system" ? (
              <div key={m.id} className="flex items-center gap-3 py-1 text-xs text-muted">
                <div className="h-px flex-1 bg-line" /><span>{t("portal.ticket.ev_status", { to: t(`portal.status.${m.event.to ?? "open"}`) })} · {timeAgo(m.created_at, i18n.language)}</span><div className="h-px flex-1 bg-line" />
              </div>
            ) : (
              <div key={m.id} className={cn("flex", m.author_kind === "customer" ? "justify-end" : "justify-start")}>
                <div className={cn("max-w-[92%] rounded-lg border px-4 py-3 sm:max-w-[80%]", m.author_kind === "customer" ? "border-line bg-sunken" : "border-line bg-raised shadow-card")}>
                  <div className="mb-1 flex items-center gap-2 text-xs">
                    {m.author_kind === "customer" ? <UserRound className="size-3.5 text-muted" /> : <LifeBuoy className="size-3.5 text-koraal" />}
                    <span className="font-semibold">{m.author_kind === "customer" ? m.author_name : t("portal.ticket.support", { name: m.author_name || "" })}</span>
                    <span className="text-muted" title={formatDate(m.created_at, i18n.language)}>{timeAgo(m.created_at, i18n.language)}</span>
                  </div>
                  <div className="whitespace-pre-wrap break-words text-sm">{m.body}</div>
                </div>
              </div>
            ))}
          </CardBody>
          <div className="border-t border-line px-5 py-4">
            {tk.can_reply ? (
              <>
                <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} className="font-sans text-sm" placeholder={t("portal.ticket.reply_placeholder")} />
                <div className="mt-3 flex justify-end">
                  <Button variant="primary" loading={reply.isPending} disabled={!body.trim()} onClick={() => reply.mutate()}><Send /> {t("portal.ticket.send")}</Button>
                </div>
              </>
            ) : <p className="text-sm text-muted">{t("portal.ticket.closed_hint")} <Link to="/portal/tickets/new" className="font-semibold text-accent-text hover:underline">{t("portal.nav.new")}</Link></p>}
          </div>
        </Card>
        <Card>
          <CardHeader title={t("portal.ticket.about")} />
          <CardBody className="space-y-3 text-sm">
            <div><div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("portal.ticket.reporter")}</div>{tk.reporter_name || "–"}</div>
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("portal.new.machines")}</div>
              {tk.machines.length ? tk.machines.map((m) => <div key={m.id}>{m.name}</div>) : <span className="text-muted">–</span>}
            </div>
            <div><div className="text-xs font-semibold uppercase tracking-wide text-muted">{t("portal.ticket.updated")}</div><span className="flex items-center gap-1"><Clock className="size-3.5 text-muted" />{timeAgo(tk.updated_at, i18n.language)}</span></div>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ account */
export function PortalAccountPage() {
  const { t } = useTranslation();
  const { me, refresh } = usePortal();
  const [f, setF] = useState({ full_name: me?.full_name ?? "", phone: me?.phone ?? "", language: me?.language ?? "en" });
  const [pw, setPw] = useState({ current: "", a: "", b: "" });
  const save = useMutation({
    mutationFn: () => ppatch("me", f),
    onSuccess: async () => { await refresh(); toast.success(t("common.saved")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  const change = useMutation({
    mutationFn: () => ppost("change-password", { current_password: pw.current, password: pw.a }),
    onSuccess: () => { setPw({ current: "", a: "", b: "" }); toast.success(t("portal.account.pw_changed")); },
    onError: (e) => toast.error(errorMessage(e, t)),
  });
  return (
    <div>
      <PageHeader title={t("portal.nav.account")} description={t("portal.account.desc", { customer: me?.customer_name })} />
      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader icon={<UserRound />} title={t("portal.account.details")} description={me?.email} />
          <CardBody className="space-y-4">
            <Field label={t("tk.acc.name")}><Input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></Field>
            <Field label={t("tk.acc.phone")}><Input value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} /></Field>
            <Field label={t("account.language")} hint={t("portal.account.language_hint")}>
              <Select value={f.language} onChange={(e) => setF({ ...f, language: e.target.value as "en" | "nl" })}>
                <option value="nl">Nederlands</option><option value="en">English</option>
              </Select>
            </Field>
            <Button variant="dark" loading={save.isPending} onClick={() => save.mutate()}>{t("common.save")}</Button>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("account.change_password")} />
          <CardBody>
            <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); change.mutate(); }}>
              <Field label={t("account.current_password")}><Input type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} required /></Field>
              <Field label={t("auth.new_password")} hint={t("portal.password_hint")}><Input type="password" autoComplete="new-password" minLength={10} value={pw.a} onChange={(e) => setPw({ ...pw, a: e.target.value })} required /></Field>
              <Field label={t("portal.confirm_password")}><Input type="password" autoComplete="new-password" value={pw.b} onChange={(e) => setPw({ ...pw, b: e.target.value })} required /></Field>
              {!!pw.b && pw.a !== pw.b && <Alert tone="warning">{t("auth.password_mismatch")}</Alert>}
              <Button variant="dark" loading={change.isPending} disabled={!pw.a || pw.a !== pw.b}>{t("account.change_password")}</Button>
            </form>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
