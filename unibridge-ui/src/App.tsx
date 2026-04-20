import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import { usePermissions } from './components/PermissionContext';
import Dashboard from './pages/Dashboard';
import Connections from './pages/Connections';
import Permissions from './pages/Permissions';
import AuditLogs from './pages/AuditLogs';
import QueryPlayground from './pages/QueryPlayground';
import GatewayRoutes from './pages/GatewayRoutes';
import GatewayRouteForm from './pages/GatewayRouteForm';
import GatewayUpstreams from './pages/GatewayUpstreams';
import GatewayMonitoring from './pages/GatewayMonitoring';
import LlmMonitoring from './pages/LlmMonitoring';
import ApiKeys from './pages/ApiKeys';
import QuerySettings from './pages/QuerySettings';
import Roles from './pages/Roles';
import Users from './pages/Users';
import AlertSettings from './pages/AlertSettings';
import AlertHistory from './pages/AlertHistory';
import AlertStatus from './pages/AlertStatus';
import S3Connections from './pages/S3Connections';
import S3Browser from './pages/S3Browser';

export function ProtectedRoute({ permission, children }: { permission: string; children: React.ReactNode }) {
  const { permissions: perms, loaded } = usePermissions();
  if (!loaded) {
    return null;
  }
  if (!perms.includes(permission)) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/connections" element={<ProtectedRoute permission="query.databases.read"><Connections /></ProtectedRoute>} />
        <Route path="/permissions" element={<ProtectedRoute permission="query.permissions.read"><Permissions /></ProtectedRoute>} />
        <Route path="/audit-logs" element={<ProtectedRoute permission="query.audit.read"><AuditLogs /></ProtectedRoute>} />
        <Route path="/query" element={<ProtectedRoute permission="query.execute"><QueryPlayground /></ProtectedRoute>} />
        <Route path="/query-settings" element={<ProtectedRoute permission="query.settings.read"><QuerySettings /></ProtectedRoute>} />
        <Route path="/s3" element={<ProtectedRoute permission="s3.connections.read"><S3Connections /></ProtectedRoute>} />
        <Route path="/s3/browse/:alias" element={<ProtectedRoute permission="s3.browse"><S3Browser /></ProtectedRoute>} />
        <Route path="/gateway/routes" element={<ProtectedRoute permission="gateway.routes.read"><GatewayRoutes /></ProtectedRoute>} />
        <Route path="/gateway/routes/new" element={<ProtectedRoute permission="gateway.routes.write"><GatewayRouteForm /></ProtectedRoute>} />
        <Route path="/gateway/routes/:id/edit" element={<ProtectedRoute permission="gateway.routes.write"><GatewayRouteForm /></ProtectedRoute>} />
        <Route path="/gateway/upstreams" element={<ProtectedRoute permission="gateway.upstreams.read"><GatewayUpstreams /></ProtectedRoute>} />
        <Route path="/gateway/monitoring" element={<ProtectedRoute permission="gateway.monitoring.read"><GatewayMonitoring /></ProtectedRoute>} />
        <Route path="/llm/monitoring" element={<ProtectedRoute permission="gateway.monitoring.read"><LlmMonitoring /></ProtectedRoute>} />
        <Route path="/api-keys" element={<ProtectedRoute permission="apikeys.read"><ApiKeys /></ProtectedRoute>} />
        <Route path="/roles" element={<ProtectedRoute permission="admin.roles.read"><Roles /></ProtectedRoute>} />
        <Route path="/users" element={<ProtectedRoute permission="admin.roles.read"><Users /></ProtectedRoute>} />
        <Route path="/alerts/status" element={<ProtectedRoute permission="alerts.read"><AlertStatus /></ProtectedRoute>} />
        <Route path="/alerts/settings" element={<ProtectedRoute permission="alerts.write"><AlertSettings /></ProtectedRoute>} />
        <Route path="/alerts/history" element={<ProtectedRoute permission="alerts.read"><AlertHistory /></ProtectedRoute>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export default App;
