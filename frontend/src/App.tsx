import { Navigate, Route, Routes, useLocation } from "react-router";
import { Layout } from "@/components/Layout";
import { Spinner } from "@/components/ui/misc";
import { useAuth } from "@/lib/auth";
import { AccountPage } from "@/pages/Account";
import { AgentsPage } from "@/pages/Agents";
import { AiActivityPage, AuditPage, ConnectionsPage, TokensPage } from "@/pages/Ai";
import { ConsentPage, ForgotPage, LoginPage, ResetPage, SetupPage } from "@/pages/AuthPages";
import { BackupsPage } from "@/pages/Backups";
import { Dashboard } from "@/pages/Dashboard";
import { HelpPage, McpCapabilitiesPage, McpHelpPage, NodesHelpPage } from "@/pages/Help";
import { ConsolePage } from "@/pages/Console";
import { GuestDetailPage, GuestsPage } from "@/pages/Guests";
import { NodeDetailPage } from "@/pages/Node";
import { EnvironmentDetailPage, EnvironmentsPage, NodesPage, ProxmoxOverviewPage, UpdatesPage } from "@/pages/Proxmox";
import { TaskRunPage, TasksPage } from "@/pages/Tasks";
import { TemplatesPage } from "@/pages/Templates";
import { GeneralPage, MailPage, NotificationsPage, WebServerPage } from "@/pages/Settings";
import { UsersPage } from "@/pages/Users";

function RequireAuth({ admin, children }: { admin?: boolean; children: React.ReactNode }) {
  const { user, status, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="grid h-full place-items-center"><Spinner /></div>;
  if (status?.setup_required) return <Navigate to="/setup" replace />;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  if (admin && user.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/forgot-password" element={<ForgotPage />} />
      <Route path="/reset-password" element={<ResetPage />} />
      <Route path="/connect/consent" element={<ConsentPage />} />
      <Route path="/console/guest/:node/:vmid" element={<RequireAuth admin><ConsolePage kind="guest" /></RequireAuth>} />
      <Route path="/console/node/:node" element={<RequireAuth admin><ConsolePage kind="node" /></RequireAuth>} />
      <Route path="/console/session/:sid" element={<RequireAuth admin><ConsolePage kind="session" /></RequireAuth>} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Dashboard />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="ai/connections" element={<ConnectionsPage />} />
        <Route path="ai/tokens" element={<TokensPage />} />
        <Route path="ai/activity" element={<RequireAuth admin><AiActivityPage /></RequireAuth>} />
        <Route path="help" element={<HelpPage />} />
        <Route path="help/mcp" element={<McpHelpPage />} />
        <Route path="help/capabilities" element={<McpCapabilitiesPage />} />
        <Route path="help/ai-admin" element={<Navigate to="/help/capabilities" replace />} />
        <Route path="help/nodes" element={<NodesHelpPage />} />
        <Route path="proxmox" element={<ProxmoxOverviewPage />} />
        <Route path="proxmox/environments" element={<EnvironmentsPage />} />
        <Route path="proxmox/environments/:id" element={<EnvironmentDetailPage />} />
        <Route path="proxmox/nodes" element={<NodesPage />} />
        <Route path="proxmox/nodes/:id" element={<NodeDetailPage />} />
        <Route path="proxmox/updates" element={<UpdatesPage />} />
        <Route path="proxmox/guests" element={<GuestsPage />} />
        <Route path="proxmox/guests/:node/:vmid" element={<GuestDetailPage />} />
        <Route path="proxmox/templates" element={<TemplatesPage />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="tasks/runs/:id" element={<TaskRunPage />} />
        <Route path="admin/agents" element={<RequireAuth admin><AgentsPage /></RequireAuth>} />
        <Route path="admin/users" element={<RequireAuth admin><UsersPage /></RequireAuth>} />
        <Route path="admin/backups" element={<RequireAuth admin><BackupsPage /></RequireAuth>} />
        <Route path="admin/mail" element={<RequireAuth admin><MailPage /></RequireAuth>} />
        <Route path="admin/notifications" element={<RequireAuth admin><NotificationsPage /></RequireAuth>} />
        <Route path="admin/web-server" element={<RequireAuth admin><WebServerPage /></RequireAuth>} />
        <Route path="admin/audit" element={<RequireAuth admin><AuditPage /></RequireAuth>} />
        <Route path="admin/general" element={<RequireAuth admin><GeneralPage /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
