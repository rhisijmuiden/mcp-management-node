import { Navigate, Route, Routes, useLocation } from "react-router";
import { Layout } from "@/components/Layout";
import { Spinner } from "@/components/ui/misc";
import { useAuth } from "@/lib/auth";
import { AccountPage } from "@/pages/Account";
import { AiActivityPage, AuditPage, ConnectionsPage, TokensPage } from "@/pages/Ai";
import { ConsentPage, ForgotPage, LoginPage, ResetPage, SetupPage } from "@/pages/AuthPages";
import { BackupsPage } from "@/pages/Backups";
import { Dashboard } from "@/pages/Dashboard";
import { HelpPage, McpHelpPage } from "@/pages/Help";
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
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Dashboard />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="ai/connections" element={<ConnectionsPage />} />
        <Route path="ai/tokens" element={<TokensPage />} />
        <Route path="ai/activity" element={<RequireAuth admin><AiActivityPage /></RequireAuth>} />
        <Route path="help" element={<HelpPage />} />
        <Route path="help/mcp" element={<McpHelpPage />} />
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
