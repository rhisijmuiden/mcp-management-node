import * as DM from "@radix-ui/react-dropdown-menu";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
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
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { post, get, patch } from "@/lib/api";
import { applyTheme, localStorageSet, useAuth } from "@/lib/auth";
import type { SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InfoDialog } from "./InfoDialog";
import { CustomerSwitcher, useCustomer } from "./customers";
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

const EXACT = new Set(["/", "/proxmox", "/servers"]);

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
    window.addEventListener("mmn:approval", onApproval);
    return () => window.removeEventListener("mmn:approval", onApproval);
  }, [isAdmin, navigate, t]);

  const pending = useQuery({
    queryKey: ["approvals-count"],
    queryFn: () => get<{ pending: number }>("api/gateway/approvals/count"),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const sections: { title?: string; items: Item[] }[] = [
    {
      items: [
        { to: customerId ? `/customers/${customerId}` : "/customers", icon: <Building2 />, label: customerId ? t("nav.customer_page") : t("nav.customers") },
      ],
    },
    {
      title: t("nav.section.gateway"),
      items: [
        { to: "/", icon: <LayoutDashboard />, label: t("nav.command_center") },
        { to: "/ai/approvals", icon: <Hand />, label: t("nav.approvals"), admin: true, badge: pending.data?.pending },
        { to: "/ai/policies", icon: <ShieldHalf />, label: t("nav.ai_policies"), admin: true },
        { to: "/ai/connections", icon: <Bot />, label: t("nav.ai_connections") },
        { to: "/ai/sessions", icon: <History />, label: t("nav.ai_sessions"), admin: true },
        { to: "/ai/tokens", icon: <KeyRound />, label: t("nav.api_tokens") },
      ],
    },
    {
      title: t("nav.section.infrastructure"),
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
            { to: "/unifi/connections", icon: <Cable />, label: t("nav.unifi_connections") },
          ],
        },
      ],
    },
    {
      title: t("nav.section.operations"),
      items: [
        { to: "/monitoring", icon: <Activity />, label: t("nav.monitoring") },
        { to: "/proxmox/updates", icon: <ArrowUpCircle />, label: t("nav.updates") },
        { to: "/servers/software", icon: <ShieldCheck />, label: t("nav.srv_software"), admin: true },
        { to: "/tasks", icon: <CalendarClock />, label: t("nav.tasks") },
        { to: "/deployments", icon: <Rocket />, label: t("nav.deployments") },
      ],
    },
    {
      title: t("nav.section.admin"),
      items: [
        { to: "/admin/agents", icon: <Plug />, label: t("nav.agents"), admin: true },
        { to: "/admin/users", icon: <Users />, label: t("nav.users"), admin: true },
        { to: "/admin/backups", icon: <DatabaseBackup />, label: t("nav.backups"), admin: true },
        { to: "/admin/mail", icon: <Mail />, label: t("nav.mail"), admin: true },
        { to: "/admin/notifications", icon: <Bell />, label: t("nav.notifications"), admin: true },
        { to: "/admin/web-server", icon: <Globe />, label: t("nav.web_server"), admin: true },
        { to: "/admin/audit", icon: <ScrollText />, label: t("nav.audit"), admin: true },
        { to: "/admin/general", icon: <Settings />, label: t("nav.general"), admin: true },
      ],
    },
    { title: t("nav.section.help"), items: [{ to: "/help", icon: <HelpCircle />, label: t("nav.help") }] },
  ];

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
      <div className="flex h-16 items-center gap-3 px-5">
        <img src="brand/rhis-icon.svg" alt="" className="size-8 rounded-[8px]" />
        <div className="min-w-0 leading-tight">
          <div className="display line-clamp-2 text-[14px] leading-tight">{status?.app_name || "Stackpilot"}</div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">Powered by RHIS</div>
        </div>
      </div>
      <div className="px-3 pb-1 pt-1">
        <CustomerSwitcher onNavigate={() => setMobileOpen(false)} />
      </div>
      <nav className="flex-1 overflow-y-auto px-3 pb-4">
        {sections.map((s) => (
          <Section key={s.title ?? "top"} title={s.title} items={s.items} onNavigate={() => setMobileOpen(false)} isAdmin={isAdmin} />
        ))}
      </nav>
      <div className="border-t border-line p-3">
        <button
          onClick={() => setInfoOpen(true)}
          className="flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-sunken"
        >
          <Info className="size-[18px] text-muted" />
          <span className="flex-1">
            <span className="block font-semibold">{t("info.button")}</span>
            <span className="mono block text-xs text-muted">
              {info.data ? `${info.data.tag}${info.data.channel === "test" ? " · test" : ""}` : "…"}
            </span>
          </span>
          {info.data?.update_available && (
            <span className="rounded-full bg-koraal px-2 py-0.5 text-[10px] font-bold uppercase text-nacht">{t("info.update_badge")}</span>
          )}
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-full">
      <aside className="hidden w-[264px] shrink-0 border-r border-line bg-raised lg:block">{sidebar}</aside>
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-nacht/50" onClick={() => setMobileOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-[280px] bg-raised shadow-2xl">
            <button className="absolute right-3 top-4 rounded p-1 hover:bg-sunken" onClick={() => setMobileOpen(false)} aria-label="Close menu">
              <X className="size-5" />
            </button>
            {sidebar}
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line bg-surface/85 px-4 backdrop-blur lg:px-8">
          <button className="rounded-md p-2 hover:bg-sunken lg:hidden" onClick={() => setMobileOpen(true)} aria-label="Menu">
            <Menu className="size-5" />
          </button>
          <div className="flex-1" />
          <button
            onClick={() => setPref({ language: i18n.language === "nl" ? "en" : "nl" })}
            className="cursor-pointer rounded-md border border-line bg-raised px-2.5 py-1.5 text-xs font-bold uppercase hover:bg-sunken"
            title={t("account.language")}
          >
            {i18n.language === "nl" ? "NL" : "EN"}
          </button>
          <DM.Root>
            <DM.Trigger className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-sunken focus:outline-none">
              <span className="grid size-8 place-items-center rounded-full bg-nacht text-sm font-bold text-white dark:bg-lucht dark:text-nacht">
                {(user?.full_name || user?.username || "?").slice(0, 1).toUpperCase()}
              </span>
              <span className="hidden text-left text-sm leading-tight sm:block">
                <span className="block font-semibold">{user?.full_name || user?.username}</span>
                <span className="block text-xs text-muted">{user?.role === "admin" ? t("roles.admin") : t("roles.viewer")}</span>
              </span>
              <ChevronDown className="size-4 text-muted" />
            </DM.Trigger>
            <DM.Portal>
              <DM.Content align="end" sideOffset={6} className="z-50 min-w-56 rounded-md border border-line bg-raised p-1 shadow-xl">
                <DM.Item onSelect={() => navigate("/account")} className="flex cursor-pointer items-center gap-2 rounded px-3 py-2 text-sm outline-none data-[highlighted]:bg-sunken">
                  <UserIcon className="size-4" /> {t("account.title")}
                </DM.Item>
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
        </header>
        <PausedBanner />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1280px] px-4 py-6 lg:px-8 lg:py-8">
            <Outlet />
          </div>
        </main>
      </div>
      <InfoDialog open={infoOpen} onOpenChange={setInfoOpen} />
    </div>
  );
}
