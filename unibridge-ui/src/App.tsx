import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import Layout from './components/Layout';
import { usePermissions } from './components/usePermissions';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const Connections = lazy(() => import('./pages/Connections'));
const Permissions = lazy(() => import('./pages/Permissions'));
const AuditLogs = lazy(() => import('./pages/AuditLogs'));
const QueryPlayground = lazy(() => import('./pages/QueryPlayground'));
const QueryTemplates = lazy(() => import('./pages/QueryTemplates'));
const GatewayRoutes = lazy(() => import('./pages/GatewayRoutes'));
const GatewayRouteForm = lazy(() => import('./pages/GatewayRouteForm'));
const GatewayUpstreams = lazy(() => import('./pages/GatewayUpstreams'));
const GatewayMonitoring = lazy(() => import('./pages/GatewayMonitoring'));
const LlmMonitoring = lazy(() => import('./pages/LlmMonitoring'));
const ApiKeys = lazy(() => import('./pages/ApiKeys'));
const QuerySettings = lazy(() => import('./pages/QuerySettings'));
const Roles = lazy(() => import('./pages/Roles'));
const Users = lazy(() => import('./pages/Users'));
const AlertSettings = lazy(() => import('./pages/AlertSettings'));
const AlertHistory = lazy(() => import('./pages/AlertHistory'));
const AlertStatus = lazy(() => import('./pages/AlertStatus'));
const S3Connections = lazy(() => import('./pages/S3Connections'));
const S3Browser = lazy(() => import('./pages/S3Browser'));

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

function RouteFallback() {
  const { t } = useTranslation();
  return (
    <div style={{ padding: 24, color: 'var(--text-secondary)', fontSize: 14 }}>
      {t('common.loading')}
    </div>
  );
}

function App() {
  return (
    <Layout>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/connections" element={<ProtectedRoute permission="query.databases.read"><Connections /></ProtectedRoute>} />
          <Route path="/permissions" element={<ProtectedRoute permission="query.permissions.read"><Permissions /></ProtectedRoute>} />
          <Route path="/audit-logs" element={<ProtectedRoute permission="query.audit.read"><AuditLogs /></ProtectedRoute>} />
          <Route path="/query" element={<ProtectedRoute permission="query.execute"><QueryPlayground /></ProtectedRoute>} />
          <Route path="/query-templates" element={<ProtectedRoute permission="query.settings.read"><QueryTemplates /></ProtectedRoute>} />
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
          <Route path="/users" element={<ProtectedRoute permission="admin.users.read"><Users /></ProtectedRoute>} />
          <Route path="/alerts/status" element={<ProtectedRoute permission="alerts.read"><AlertStatus /></ProtectedRoute>} />
          <Route path="/alerts/settings" element={<ProtectedRoute permission="alerts.read"><AlertSettings /></ProtectedRoute>} />
          <Route path="/alerts/history" element={<ProtectedRoute permission="alerts.read"><AlertHistory /></ProtectedRoute>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}

export default App;
