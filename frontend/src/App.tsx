import { Navigate, Route, Routes, useLocation, useParams } from "react-router";
import { Layout } from "@/components/Layout";
import { Spinner } from "@/components/ui/misc";
import { useAuth } from "@/lib/auth";
import { AccountPage } from "@/pages/Account";
import { AgentsPage } from "@/pages/Agents";
import { AiActivityPage, AuditPage, ConnectionsPage, TokensPage } from "@/pages/Ai";
import { ConsentPage, ForgotPage, LoginPage, ResetPage, SetupPage } from "@/pages/AuthPages";
import { BackupsPage } from "@/pages/Backups";
import { ApprovalsPage, CommandCenterPage, PoliciesPage, SessionsPage } from "@/pages/Gateway";
import { HubPage, ToolCatalogPage } from "@/pages/Hub";
import { HelpPage, McpCapabilitiesPage, McpHelpPage, NodesHelpPage } from "@/pages/Help";
import { ConsolePage } from "@/pages/Console";
import { GuestDetailPage, GuestsPage } from "@/pages/Guests";
import { NodeDetailPage } from "@/pages/Node";
import { NodesPage, ProxmoxOverviewPage, UpdatesPage } from "@/pages/Proxmox";
import { CustomerPage, CustomersPage } from "@/pages/Customers";
import { TaskRunPage, TasksPage } from "@/pages/Tasks";
import { TemplatesPage } from "@/pages/Templates";
import { GeneralPage, MailPage, NotificationsPage, WebServerPage } from "@/pages/Settings";
import { UsersPage } from "@/pages/Users";
import { CopyPage, LogSearchPage, ServersPage, SoftwarePage } from "@/pages/Servers";
import { lazy, Suspense, type ReactNode } from "react";

// Phase 4 pages are loaded on demand to keep the first load small.
const ClusterPage = lazy(() => import("@/pages/Cluster").then((m) => ({ default: m.ClusterPage })));
const DeploymentsPage = lazy(() => import("@/pages/Deployments").then((m) => ({ default: m.DeploymentsPage })));
const DeploymentDetailPage = lazy(() => import("@/pages/Deployments").then((m) => ({ default: m.DeploymentDetailPage })));
const MonitoringPage = lazy(() => import("@/pages/Monitoring").then((m) => ({ default: m.MonitoringPage })));
const UnifiNetworkPage = lazy(() => import("@/pages/Unifi").then((m) => ({ default: m.UnifiNetworkPage })));
const UnifiAccessPage = lazy(() => import("@/pages/Unifi").then((m) => ({ default: m.UnifiAccessPage })));
const UnifiConnectionsPage = lazy(() => import("@/pages/Unifi").then((m) => ({ default: m.UnifiConnectionsPage })));
const NetworkPage = lazy(() => import("@/pages/Network").then((m) => ({ default: m.NetworkPage })));
const SdnDesignerPage = lazy(() => import("@/pages/SdnDesigner").then((m) => ({ default: m.SdnDesignerPage })));
const L = ({ children }: { children: ReactNode }) => <Suspense fallback={<Spinner />}>{children}</Suspense>;

function RequireAuth({ admin, children }: { admin?: boolean; children: React.ReactNode }) {
  const { user, status, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="grid h-full place-items-center"><Spinner /></div>;
  if (status?.setup_required) return <Navigate to="/setup" replace />;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  if (admin && user.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

function EnvRedirect() {
  const { id = "" } = useParams();
  return <Navigate to={`/customers/${id}?tab=proxmox`} replace />;
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
        <Route index element={<CommandCenterPage />} />
        <Route path="dashboard" element={<Navigate to="/" replace />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="ai/connections" element={<ConnectionsPage />} />
        <Route path="ai/tokens" element={<TokensPage />} />
        <Route path="ai/activity" element={<RequireAuth admin><AiActivityPage /></RequireAuth>} />
        <Route path="ai/approvals" element={<RequireAuth admin><ApprovalsPage /></RequireAuth>} />
        <Route path="ai/policies" element={<RequireAuth admin><PoliciesPage /></RequireAuth>} />
        <Route path="ai/sessions" element={<RequireAuth admin><SessionsPage /></RequireAuth>} />
        <Route path="ai/hub" element={<RequireAuth admin><HubPage /></RequireAuth>} />
        <Route path="ai/tools" element={<RequireAuth admin><ToolCatalogPage /></RequireAuth>} />
        <Route path="help" element={<HelpPage />} />
        <Route path="help/mcp" element={<McpHelpPage />} />
        <Route path="help/capabilities" element={<McpCapabilitiesPage />} />
        <Route path="help/ai-admin" element={<Navigate to="/help/capabilities" replace />} />
        <Route path="help/nodes" element={<NodesHelpPage />} />
        <Route path="proxmox" element={<ProxmoxOverviewPage />} />
        <Route path="customers" element={<CustomersPage />} />
        <Route path="customers/:id" element={<CustomerPage />} />
        <Route path="proxmox/environments" element={<Navigate to="/customers" replace />} />
        <Route path="proxmox/environments/:id" element={<EnvRedirect />} />
        <Route path="proxmox/nodes" element={<NodesPage />} />
        <Route path="proxmox/nodes/:id" element={<NodeDetailPage />} />
        <Route path="proxmox/updates" element={<UpdatesPage />} />
        <Route path="proxmox/guests" element={<GuestsPage />} />
        <Route path="proxmox/guests/:node/:vmid" element={<GuestDetailPage />} />
        <Route path="proxmox/templates" element={<TemplatesPage />} />
        <Route path="proxmox/network" element={<L><NetworkPage /></L>} />
        <Route path="proxmox/network/sdn-designer" element={<RequireAuth admin><L><SdnDesignerPage /></L></RequireAuth>} />
        <Route path="proxmox/network/sdn-designer/:id" element={<RequireAuth admin><L><SdnDesignerPage /></L></RequireAuth>} />
        <Route path="proxmox/cluster" element={<L><ClusterPage /></L>} />
        <Route path="unifi" element={<Navigate to="/unifi/network" replace />} />
        <Route path="unifi/network" element={<L><UnifiNetworkPage /></L>} />
        <Route path="unifi/access" element={<L><UnifiAccessPage /></L>} />
        <Route path="unifi/connections" element={<L><UnifiConnectionsPage /></L>} />
        <Route path="servers" element={<ServersPage />} />
        <Route path="workstations" element={<ServersPage role="workstation" />} />
        <Route path="servers/logs" element={<RequireAuth admin><LogSearchPage /></RequireAuth>} />
        <Route path="servers/software" element={<RequireAuth admin><SoftwarePage /></RequireAuth>} />
        <Route path="servers/copy" element={<RequireAuth admin><CopyPage /></RequireAuth>} />
        <Route path="servers/:id" element={<NodeDetailPage section="servers" />} />
        <Route path="deployments" element={<L><DeploymentsPage /></L>} />
        <Route path="deployments/:id" element={<L><DeploymentDetailPage /></L>} />
        <Route path="monitoring" element={<L><MonitoringPage /></L>} />
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
