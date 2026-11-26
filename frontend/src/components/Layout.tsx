import * as DM from "@radix-ui/react-dropdown-menu";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  ArrowUpCircle,
  Gauge,
  Plug,
  Bell,
  Bot,
  CalendarClock,
  ChevronDown,
  DatabaseBackup,
  Globe,
  HardDrive,
  HelpCircle,
  Info,
  KeyRound,
  Layers,
  LayoutDashboard,
  LogOut,
  Mail,
  Menu,
  Monitor,
  Moon,
  Network,
  ScrollText,
  Server,
  Settings,
  Sun,
  User as UserIcon,
  Users,
  X,
  Rocket,
  FolderGit2,
  ServerCog,
  FileSearch,
  ShieldCheck,
  Copy,
  Wifi,
  DoorClosed,
  Cable,
  Building2,
  Laptop,
  Boxes,
  ChevronRight,
  Hand,
  History,
  ShieldHalf,
  Wrench,
  Sunrise,
  Languages,
  Eye,
  Search,
  BookOpenCheck, HeartPulse, Radar, CloudCog, LifeBuoy, BookText, LockKeyhole, Award, Flame, Waypoints, Wallet, CalendarRange,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { post, get, patch } from "@/lib/api";
import { applyTheme, localStorageSet, useAuth } from "@/lib/auth";
import type { SystemInfo } from "@/lib/types";
import { appUrl, cn } from "@/lib/utils";
import { InfoDialog } from "./InfoDialog";
import { CUSTOMER_CSS, CustomerSwitcher, useCustomer, useCustomers } from "./customers";
import { CommandPalette, type PageEntry } from "./CommandPalette";
import { useLiveEvents } from "./useLiveEvents";
import { PausedBanner } from "@/pages/Gateway";
import i18n from "@/i18n";
import { toast } from "sonner";

interface Item {
  to?: string;
  icon: ReactNode;
  label: string;
  soon?: boolean;
  admin?: boolean;
  badge?: number;
  /** A group: the item opens a submenu. */
  children?: Item[];
}

const EXACT = new Set(["/", "/proxmox", "/servers", "/customers"]);

function NavItem({ item, onClick, isAdmin, nested }: { item: Item; onClick?: () => void; isAdmin: boolean; nested?: boolean }) {
  const { t } = useTranslation();
  const loc = useLocation();
  const base = cn("group flex items-center gap-3 rounded-md px-3 py-2 text-[14px] font-medium transition-colors [&_svg]:size-[18px]", nested && "py-1.5 text-[13.5px]");
  const kids = item.children?.filter((i) => !i.admin || isAdmin);
  const childActive = !!kids?.some((k) => k.to && (EXACT.has(k.to) ? loc.pathname === k.to : loc.pathname === k.to || loc.pathname.startsWith(k.to + "/")));
  const [open, setOpen] = useState(childActive);
  useEffect(() => { if (childActive) setOpen(true); }, [childActive]);
  if (kids) {
    if (!kids.length) return null;
    return (
      <div>
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
          className={cn(base, "w-full cursor-pointer text-left", childActive && !open ? "bg-sunken text-ink" : "text-ink hover:bg-sunken")}>
          {item.icon}
          <span className="flex-1 truncate">{item.label}</span>
          {!open && kids.some((k) => k.badge) && <span className="min-w-5 rounded-full bg-koraal px-1.5 py-px text-center text-[11px] font-bold text-nacht">{kids.reduce((a, k) => a + (k.badge ?? 0), 0)}</span>}
          <ChevronRight className={cn("!size-4 text-muted transition-transform", open && "rotate-90")} />
        </button>
        {open && (
          <div className="ml-[21px] mt-0.5 space-y-0.5 border-l border-line pl-2">
            {kids.map((k) => <NavItem key={k.label} item={k} onClick={onClick} isAdmin={isAdmin} nested />)}
          </div>
        )}
      </div>
    );
  }
  if (item.soon || !item.to) {
    return (
      <div className={cn(base, "cursor-default text-muted/70")} title={t("nav.soon_hint")}>
        {item.icon}
        <span className="flex-1 truncate">{item.label}</span>
        <span className="rounded-full bg-sunken px-1.5 py-px text-[10px] font-bold uppercase tracking-wide">{t("nav.soon")}</span>
      </div>
    );
  }
  return (
    <NavLink
      to={item.to}
      end={EXACT.has(item.to)}
      onClick={onClick}
      className={({ isActive }) =>
        cn(base, isActive ? "bg-nacht text-white dark:bg-lucht/15 dark:text-ink [&_svg]:text-koraal" : "text-ink hover:bg-sunken")
      }
    >
      {!nested && item.icon}
      <span className="flex-1 truncate">{item.label}</span>
      {!!item.badge && <span className="min-w-5 rounded-full bg-koraal px-1.5 py-px text-center text-[11px] font-bold text-nacht">{item.badge}</span>}
    </NavLink>
  );
}

function Section({ title, items, onNavigate, isAdmin }: { title?: string; items: Item[]; onNavigate?: () => void; isAdmin: boolean }) {
  const visible = items.filter((i) => (!i.admin || isAdmin) && (!i.children || i.children.some((k) => !k.admin || isAdmin)));
  if (!visible.length) return null;
  return (
    <div className="space-y-0.5">
      {title ? <div className="px-3 pb-1 pt-4 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{title}</div> : <div className="pt-2" />}
      {visible.map((i) => (
        <NavItem key={i.label} item={i} onClick={onNavigate} isAdmin={isAdmin} />
      ))}
    </div>
  );
}

export function Layout() {
  const { t } = useTranslation();
  const { user, status, refresh } = useAuth();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => get<SystemInfo>("api/system/info"), refetchInterval: 5 * 60_000 });
  useLiveEvents();
  const isAdmin = user?.role === "admin";
  const { customerId } = useCustomer();

  useEffect(() => {
    if (!isAdmin) return;
    const onApproval = (e: Event) => {
      const d = (e as CustomEvent).detail as { id?: string; tool?: string; who?: string; test?: boolean };
      toast(d.test ? t("gw.toast.test") : t("gw.toast.new", { tool: d.tool ?? "", who: d.who ?? "AI" }), {
        duration: 12_000,
        action: { label: t("gw.appr.review"), onClick: () => navigate(d.id ? `/ai/approvals?id=${d.id}` : "/ai/approvals") },
      });
    };
    const onNightly = (e: Event) => {
      const d = (e as CustomEvent).detail as { high?: number; new?: number; proposals?: number };
      toast(t("nightly.toast", { high: d.high ?? 0, new: d.new ?? 0, proposals: d.proposals ?? 0 }), {
        duration: 20_000,
        action: { label: t("nightly.toast_open"), onClick: () => navigate("/ai/nightly") },
      });
    };
    const onRemediation = (e: Event) => {
      const d = (e as CustomEvent).detail as { status?: string; target?: string; rule?: string; note?: string };
      const text = d.status === "paused" ? t("rem.toast.paused", { note: d.note || "–" })
        : t(`rem.toast.${d.status}`, { target: d.target ?? "–", rule: d.rule ?? "", defaultValue: "" });
      if (!text) return;
      const show = d.status === "succeeded" ? toast.success : d.status === "failed" || d.status === "paused" || d.status === "skipped" ? toast.error : toast;
      show(text, { duration: 15_000, action: { label: t("rem.toast.open"), onClick: () => navigate("/remediation") } });
    };
    const onTicket = (e: Event) => {
      const d = (e as CustomEvent).detail as { id?: string; number?: number };
      toast(t("tk.toast", { number: d.number ?? "" }), {
        duration: 15_000,
        action: { label: t("tk.toast_open"), onClick: () => navigate(d.id ? `/tickets/${d.id}` : "/tickets") },
      });
    };
    const onVault = (e: Event) => {
      const d = (e as CustomEvent).detail as { item?: string; token?: string; action?: string; machine?: string };
      toast(t(d.action === "use" ? "vault.toast_use" : "vault.toast_reveal", { item: d.item ?? "", token: d.token ?? "AI", machine: d.machine ?? "" }), {
        duration: 15_000,
        action: { label: t("vault.toast_open"), onClick: () => navigate("/vault") },
      });
    };
    window.addEventListener("mmn:vault", onVault);
    window.addEventListener("mmn:ticket", onTicket);
    window.addEventListener("mmn:approval", onApproval);
    window.addEventListener("mmn:nightly", onNightly);
    window.addEventListener("mmn:remediation", onRemediation);
    return () => {
      window.removeEventListener("mmn:vault", onVault);
      window.removeEventListener("mmn:ticket", onTicket);
      window.removeEventListener("mmn:approval", onApproval);
      window.removeEventListener("mmn:nightly", onNightly);
      window.removeEventListener("mmn:remediation", onRemediation);
    };
  }, [isAdmin, navigate, t]);

  const pending = useQuery({
    queryKey: ["approvals-count"],
    queryFn: () => get<{ pending: number }>("api/gateway/approvals/count"),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const runbookInput = useQuery({
    queryKey: ["runbook-runs-count"],
    queryFn: () => get<{ waiting_input: number }>("api/runbooks/runs/count"),
    refetchInterval: 60_000,
  });

  const uptimeDown = useQuery({
    queryKey: ["uptime-checks", "menu"],
    queryFn: async () => (await get<{ counts: Record<string, number> }>("api/uptime/checks")).counts.down ?? 0,
    refetchInterval: 60_000,
  });

  const m365Risk = useQuery({
    queryKey: ["m365", "menu"],
    queryFn: async () => {
      const o = await get<{ totals: { risky?: number; admins_no_mfa?: number } }>("api/m365/overview");
      return (o.totals.risky ?? 0) + (o.totals.admins_no_mfa ?? 0);
    },
    refetchInterval: 120_000,
  });

  const netsec = useQuery({
    queryKey: ["netsec", "overview", "menu"],
    queryFn: () => get<{ certificates: Record<string, number>; firewall: { unacknowledged: number }; vpn: { watched_down: number } }>("api/netsec/overview"),
    refetchInterval: 120_000,
  });
  const certBadge = (netsec.data?.certificates.expired ?? 0) + (netsec.data?.certificates.critical ?? 0);

  const costBadge = useQuery({
    queryKey: ["costs", "badge"],
    queryFn: () => get<{ attention: number }>("api/costs/badge"),
    refetchInterval: 300_000,
  });

  const changeBadge = useQuery({
    queryKey: ["changes", "badge"],
    queryFn: () => get<{ in_progress: number; warnings: number }>("api/changes/badge"),
    refetchInterval: 300_000,
  });

  const ticketCounts = useQuery({
    queryKey: ["ticket-counts"],
    queryFn: () => get<{ needs_reply: number }>("api/tickets/counts"),
    refetchInterval: 60_000,
  });

  const sections: { title?: string; items: Item[] }[] = [
    {
      title: t("nav.section.today"),
      items: [
        { to: "/", icon: <LayoutDashboard />, label: t("nav.command_center") },
        { to: "/ai/nightly", icon: <Sunrise />, label: t("nav.nightly") },
        { to: "/ai/approvals", icon: <Hand />, label: t("nav.approvals"), admin: true, badge: pending.data?.pending },
        { to: "/tickets", icon: <LifeBuoy />, label: t("nav.tickets"), badge: ticketCounts.data?.needs_reply || undefined },
      ],
    },
    {
      title: t("nav.section.customer"),
      items: [
        ...(customerId ? [{ to: `/customers/${customerId}`, icon: <Building2 />, label: t("nav.customer_overview") }] : []),
        { to: "/customers", icon: <Users />, label: t("nav.customers_all") },
      ],
    },
    {
      title: t("nav.section.environment"),
      items: [
        {
          icon: <Boxes />, label: t("nav.section.proxmox"), children: [
            { to: "/proxmox", icon: <Gauge />, label: t("nav.px_overview") },
            { to: "/proxmox/nodes", icon: <Server />, label: t("nav.nodes") },
            { to: "/proxmox/guests", icon: <HardDrive />, label: t("nav.guests") },
            { to: "/proxmox/templates", icon: <Layers />, label: t("nav.templates") },
            { to: "/proxmox/network", icon: <Network />, label: t("nav.network") },
            { to: "/proxmox/cluster", icon: <ServerCog />, label: t("nav.cluster") },
          ],
        },
        {
          icon: <Monitor />, label: t("nav.section.servers"), children: [
            { to: "/servers", icon: <Monitor />, label: t("nav.servers") },
            { to: "/servers/logs", icon: <FileSearch />, label: t("nav.srv_logs"), admin: true },
            { to: "/servers/copy", icon: <Copy />, label: t("nav.srv_copy"), admin: true },
          ],
        },
        { to: "/workstations", icon: <Laptop />, label: t("nav.workstations") },
        {
          icon: <Wifi />, label: t("nav.section.unifi"), children: [
            { to: "/unifi/network", icon: <Wifi />, label: t("nav.unifi_network") },
            { to: "/unifi/access", icon: <DoorClosed />, label: t("nav.unifi_access") },
          ],
        },
        { to: "/m365", icon: <CloudCog />, label: t("nav.m365"), badge: m365Risk.data || undefined },
      ],
    },
    {
      title: t("nav.section.maintenance"),
      items: [
        { to: "/backups", icon: <Archive />, label: t("nav.protection") },
        {
          icon: <ArrowUpCircle />, label: t("nav.updates_software"), children: [
            { to: "/proxmox/updates", icon: <ArrowUpCircle />, label: t("nav.updates") },
            { to: "/servers/software", icon: <ShieldCheck />, label: t("nav.srv_software"), admin: true },
          ],
        },
        {
          icon: <Activity />, label: t("nav.monitoring_uptime"), children: [
            { to: "/monitoring", icon: <Activity />, label: t("nav.monitoring") },
            { to: "/uptime", icon: <Radar />, label: t("nav.uptime"), badge: uptimeDown.data || undefined },
          ],
        },
        {
          icon: <ShieldCheck />, label: t("nav.security"), children: [
            { to: "/security/certificates", icon: <Award />, label: t("nav.sec_certs"), badge: certBadge || undefined },
            { to: "/security/firewall", icon: <Flame />, label: t("nav.sec_fw"), badge: netsec.data?.firewall.unacknowledged || undefined },
            { to: "/security/vpn", icon: <Waypoints />, label: t("nav.sec_vpn"), badge: netsec.data?.vpn.watched_down || undefined },
          ],
        },
        { to: "/changes", icon: <CalendarRange />, label: t("nav.changes"), badge: ((changeBadge.data?.in_progress ?? 0) + (changeBadge.data?.warnings ?? 0)) || undefined },
      ],
    },
    {
      title: t("nav.section.automation"),
      items: [
        { to: "/runbooks", icon: <BookOpenCheck />, label: t("nav.runbooks"), badge: runbookInput.data?.waiting_input },
        {
          icon: <Rocket />, label: t("nav.software"), children: [
            { to: "/repos", icon: <FolderGit2 />, label: t("nav.repos") },
            { to: "/deployments", icon: <Rocket />, label: t("nav.deployments") },
          ],
        },
        { to: "/tasks", icon: <CalendarClock />, label: t("nav.tasks") },
        { to: "/remediation", icon: <HeartPulse />, label: t("nav.remediation") },
      ],
    },
    {
      title: t("nav.section.knowledge"),
      items: [
        { to: "/docs", icon: <BookText />, label: t("nav.docs") },
        { to: "/vault", icon: <LockKeyhole />, label: t("nav.vault") },
        { to: "/costs/overview", icon: <Wallet />, label: t("nav.costs"), badge: costBadge.data?.attention || undefined },
      ],
    },
  ];

  const footerItems: Item[] = [
    {
      icon: <Plug />, label: t("nav.section.integrations"), children: [
        { to: "/integrations", icon: <Plug />, label: t("nav.integrations_all") },
        { to: "/admin/agents", icon: <Plug />, label: t("nav.agents"), admin: true },
        { to: "/ai/connections", icon: <Bot />, label: t("nav.ai_connections") },
        { to: "/ai/tokens", icon: <KeyRound />, label: t("nav.api_tokens") },
        { to: "/ai/hub", icon: <Network />, label: t("nav.mcp_hub"), admin: true },
        { to: "/unifi/connections", icon: <Cable />, label: t("nav.unifi_connections_full") },
        { to: "/admin/mail", icon: <Mail />, label: t("nav.mail"), admin: true },
      ],
    },
    {
      icon: <Settings />, label: t("nav.section.settings"), children: [
        { to: "/ai/policies", icon: <ShieldHalf />, label: t("nav.ai_policies"), admin: true },
        { to: "/ai/tools", icon: <Wrench />, label: t("nav.tool_catalog"), admin: true },
        { to: "/ai/sessions", icon: <History />, label: t("nav.ai_sessions"), admin: true },
        { to: "/admin/users", icon: <Users />, label: t("nav.users"), admin: true },
        { to: "/admin/backups", icon: <DatabaseBackup />, label: t("nav.backups"), admin: true },
        { to: "/admin/notifications", icon: <Bell />, label: t("nav.notifications"), admin: true },
        { to: "/admin/portal", icon: <Eye />, label: t("nav.portal_settings"), admin: true },
        { to: "/admin/web-server", icon: <Globe />, label: t("nav.web_server"), admin: true },
        { to: "/admin/audit", icon: <ScrollText />, label: t("nav.audit"), admin: true },
        { to: "/admin/general", icon: <Settings />, label: t("nav.general"), admin: true },
      ],
    },
    { to: "/help", icon: <HelpCircle />, label: t("nav.help") },
  ];

  // Every page for the Ctrl+K search, with the menu group it lives in.
  const pages: PageEntry[] = [];
  const collect = (items: Item[], group: string) => {
    for (const i of items) {
      if (i.admin && !isAdmin) continue;
      if (i.children) collect(i.children, `${group} › ${i.label}`);
      else if (i.to && !i.soon) pages.push({ to: i.to, label: i.label, group });
    }
  };
  sections.forEach((s) => collect(s.items, s.title ?? ""));
  collect(footerItems, t("nav.section.setup"));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const logout = async () => {
    await post("api/auth/logout");
    await refresh();
    navigate("/login");
  };

  const setPref = async (data: { language?: string; theme?: string }) => {
    if (data.language) {
      localStorageSet("mmn-lang", data.language);
      i18n.changeLanguage(data.language);
    }
    if (data.theme) {
      localStorageSet("mmn-theme", data.theme);
      applyTheme(data.theme);
    }
    await patch("api/auth/me", data);
    await refresh();
  };

  const sidebar = (
    <div className="flex h-full flex-col">
      <nav className="flex-1 overflow-y-auto px-3 pb-4" aria-label={t("nav.menu")}>
        {sections.map((s) => (
          <Section key={s.title ?? "top"} title={s.title} items={s.items} onNavigate={() => setMobileOpen(false)} isAdmin={isAdmin} />
        ))}
      </nav>
      <div className="border-t border-line px-3 pb-3 pt-1">
        <Section items={footerItems} onNavigate={() => setMobileOpen(false)} isAdmin={isAdmin} />
      </div>
    </div>
  );

  const userMenu = (
    <DM.Root>
      <DM.Trigger className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-white hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-lucht" aria-label={t("account.title")}>
        <span className="grid size-8 place-items-center rounded-full bg-lucht text-sm font-bold text-nacht">
          {(user?.full_name || user?.username || "?").slice(0, 1).toUpperCase()}
        </span>
        <span className="hidden text-left text-sm leading-tight xl:block">
          <span className="block font-semibold">{user?.full_name || user?.username}</span>
          <span className="block text-xs text-white/60">{user?.role === "admin" ? t("roles.admin") : t("roles.viewer")}</span>
        </span>
        <ChevronDown className="hidden size-4 text-white/60 sm:block" />
      </DM.Trigger>
      <DM.Portal>
        <DM.Content align="end" sideOffset={6} className="z-50 min-w-56 rounded-md border border-line bg-raised p-1 shadow-xl">
          <DM.Item onSelect={() => navigate("/account")} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
            <UserIcon className="size-4" /> {t("account.title")}
          </DM.Item>
          <DM.Item onSelect={() => setInfoOpen(true)} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
            <Info className="size-4" /> {t("info.button")}
          </DM.Item>
          <DM.Separator className="my-1 h-px bg-line" />
          <div className="px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-muted">{t("account.language")}</div>
          {(["en", "nl"] as const).map((lng) => (
            <DM.Item key={lng} onSelect={() => setPref({ language: lng })} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
              <Languages className="size-4" /> {lng === "nl" ? "Nederlands" : "English"}
              {i18n.language === lng && <span className="ml-auto size-1.5 rounded-full bg-koraal" />}
            </DM.Item>
          ))}
          <DM.Separator className="my-1 h-px bg-line" />
          <div className="px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-muted">{t("account.theme")}</div>
          {(["system", "light", "dark"] as const).map((th) => (
            <DM.Item key={th} onSelect={() => setPref({ theme: th })} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
              {th === "light" ? <Sun className="size-4" /> : th === "dark" ? <Moon className="size-4" /> : <Monitor className="size-4" />}
              {t(`account.theme_${th}`)}
              {user?.theme === th && <span className="ml-auto size-1.5 rounded-full bg-koraal" />}
            </DM.Item>
          ))}
          <DM.Separator className="my-1 h-px bg-line" />
          <DM.Item onSelect={logout} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm text-offline outline-none data-[highlighted]:bg-sunken">
            <LogOut className="size-4" /> {t("auth.logout")}
          </DM.Item>
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );

  const pill = "flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border border-white/20 px-3 py-1 text-xs font-semibold text-white/90 hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-lucht [&_svg]:size-3.5";

  return (
    <div className="flex h-full flex-col">
      <header className="z-30 flex h-14 shrink-0 items-center gap-2 bg-nacht px-3 text-white sm:gap-3 lg:px-4 dark:border-b dark:border-line dark:bg-[#0a111e]">
        <button className="rounded-md p-2 hover:bg-white/10 lg:hidden" onClick={() => setMobileOpen(true)} aria-label={t("nav.open_menu")}>
          <Menu className="size-5" />
        </button>
        <NavLink to="/" className="flex shrink-0 items-center gap-2.5 lg:w-[240px]" aria-label={status?.app_name || "Stackpilot"}>
          <img src="brand/rhis-icon.svg" alt="" className="size-7 rounded-[7px]" />
          <span className="hidden leading-tight sm:block">
            <span className="display block text-[14px] leading-tight">{status?.app_name || "Stackpilot"}</span>
            <span className="block text-[9.5px] font-semibold uppercase tracking-wider text-white/55">Powered by RHIS</span>
          </span>
        </NavLink>
        <CustomerSwitcher onNavigate={() => setMobileOpen(false)} />
        <button
          type="button"
          onClick={() => setSearchOpen(true)}
          className="ml-1 hidden h-9 min-w-0 max-w-[380px] flex-1 cursor-pointer items-center gap-2 rounded-md border border-white/15 bg-white/[0.06] px-3 text-left text-[13px] text-white/60 hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-lucht md:flex"
        >
          <Search className="size-4 shrink-0" />
          <span className="flex-1 truncate">{t("top.search_ph")}</span>
          <kbd className="mono rounded border border-white/20 px-1.5 text-[10.5px]">Ctrl K</kbd>
        </button>
        <button type="button" onClick={() => setSearchOpen(true)} className="rounded-md p-2 hover:bg-white/10 md:hidden" aria-label={t("top.search")}>
          <Search className="size-5" />
        </button>
        <div className="ml-auto flex items-center gap-2">
          {isAdmin && !!pending.data?.pending && (
            <NavLink to="/ai/approvals" className={cn(pill, "border-koraal bg-koraal text-nacht hover:bg-koraal/90")}>
              <Hand /> <span className="hidden sm:inline">{t("top.approvals", { count: pending.data.pending })}</span><span className="sm:hidden">{pending.data.pending}</span>
            </NavLink>
          )}
          {isAdmin && customerId && (
            <a href={appUrl(`/portal?preview=${encodeURIComponent(customerId)}`)} target="_blank" rel="noreferrer" className={cn(pill, "hidden md:flex")} title={t("top.view_as_customer_hint")}>
              <Eye /> {t("top.view_as_customer")}
            </a>
          )}
          <button type="button" onClick={() => setInfoOpen(true)} className={cn(pill, "hidden sm:flex")} title={t("info.button")}>
            <Info />
            <span className="mono">{info.data ? `${info.data.tag}${info.data.channel === "test" ? " · test" : ""}` : "…"}</span>
            {info.data?.update_available && <span className="rounded-full bg-koraal px-1.5 text-[10px] font-bold uppercase text-nacht">{t("info.update_badge")}</span>}
          </button>
          {userMenu}
        </div>
      </header>
      <ContextBand />
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-[264px] shrink-0 border-r border-line bg-raised lg:block">{sidebar}</aside>
        {mobileOpen && (
          <div className="fixed inset-0 z-40 lg:hidden">
            <div className="absolute inset-0 bg-nacht/50" onClick={() => setMobileOpen(false)} />
            <aside className="absolute inset-y-0 left-0 flex w-[280px] flex-col bg-raised shadow-2xl">
              <div className="flex h-14 shrink-0 items-center justify-between px-5">
                <span className="display text-[14px]">{status?.app_name || "Stackpilot"}</span>
                <button className="rounded p-1 hover:bg-sunken" onClick={() => setMobileOpen(false)} aria-label={t("nav.close_menu")}>
                  <X className="size-5" />
                </button>
              </div>
              <div className="min-h-0 flex-1">{sidebar}</div>
            </aside>
          </div>
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <PausedBanner />
          <main className="flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-[1280px] px-4 py-6 lg:px-8 lg:py-8">
              <Outlet />
            </div>
          </main>
        </div>
      </div>
      <InfoDialog open={infoOpen} onOpenChange={setInfoOpen} />
      <CommandPalette open={searchOpen} onOpenChange={setSearchOpen} pages={pages} />
    </div>
  );
}

/** The band under the top bar: which customer the whole app is limited to. */
function ContextBand() {
  const { t } = useTranslation();
  const { customerId, setCustomer } = useCustomer();
  const list = useCustomers();
  const loc = useLocation();
  const current = list.data?.find((c) => c.id === customerId);
  if (!current) return null;
  const color = CUSTOMER_CSS[current.color] ?? "var(--lucht)";
  const onPage = loc.pathname === `/customers/${current.id}`;
  return (
    <div
      className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b-2 px-4 py-1.5 text-[13px] lg:px-5"
      style={{ borderBottomColor: color, background: `color-mix(in srgb, ${color} 16%, var(--surface-raised))` }}
      role="status"
    >
      <span className="size-2.5 shrink-0 rounded-full" style={{ background: color }} />
      <span className="min-w-0">
        <Trans i18nKey="top.band_working_in" values={{ name: current.name }} components={{ b: <b /> }} />
        <span className="hidden text-muted sm:inline"> {t("top.band_filtered")}</span>
      </span>
      <span className="ml-auto flex gap-4 font-semibold">
        {!onPage && <NavLink to={`/customers/${current.id}`} className="hover:underline">{t("top.band_overview")}</NavLink>}
        <button type="button" className="cursor-pointer font-semibold hover:underline" onClick={() => setCustomer("")}>{t("top.band_show_all")}</button>
      </span>
    </div>
  );
}
