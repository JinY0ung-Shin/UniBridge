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
import GatewayConsumers from './pages/GatewayConsumers';
import GatewayMonitoring from './pages/GatewayMonitoring';
import QuerySettings from './pages/QuerySettings';
import Roles from './pages/Roles';
import Users from './pages/Users';

function ProtectedRoute({ permission, children }: { permission: string; children: React.ReactNode }) {
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
        <Route path="/gateway/routes" element={<ProtectedRoute permission="gateway.routes.read"><GatewayRoutes /></ProtectedRoute>} />
        <Route path="/gateway/routes/new" element={<ProtectedRoute permission="gateway.routes.write"><GatewayRouteForm /></ProtectedRoute>} />
        <Route path="/gateway/routes/:id/edit" element={<ProtectedRoute permission="gateway.routes.write"><GatewayRouteForm /></ProtectedRoute>} />
        <Route path="/gateway/upstreams" element={<ProtectedRoute permission="gateway.upstreams.read"><GatewayUpstreams /></ProtectedRoute>} />
        <Route path="/gateway/consumers" element={<ProtectedRoute permission="gateway.consumers.read"><GatewayConsumers /></ProtectedRoute>} />
        <Route path="/gateway/monitoring" element={<ProtectedRoute permission="gateway.monitoring.read"><GatewayMonitoring /></ProtectedRoute>} />
        <Route path="/roles" element={<ProtectedRoute permission="admin.roles.read"><Roles /></ProtectedRoute>} />
        <Route path="/users" element={<ProtectedRoute permission="admin.roles.read"><Users /></ProtectedRoute>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export default App;
