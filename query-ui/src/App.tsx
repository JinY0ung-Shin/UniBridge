import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Connections from './pages/Connections';
import Permissions from './pages/Permissions';
import AuditLogs from './pages/AuditLogs';
import QueryPlayground from './pages/QueryPlayground';

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/permissions" element={<Permissions />} />
        <Route path="/audit-logs" element={<AuditLogs />} />
        <Route path="/query" element={<QueryPlayground />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export default App;
