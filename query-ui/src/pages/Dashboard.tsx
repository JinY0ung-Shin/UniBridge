import { useQuery } from '@tanstack/react-query';
import { getHealth, getAdminDatabases, type DatabaseHealth } from '../api/client';
import './Dashboard.css';

interface DashboardDbEntry {
  alias: string;
  status: 'connected' | 'error';
}

function Dashboard() {
  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const healthData = healthQuery.data;
  const databases = dbsQuery.data ?? [];

  const dbHealthMap: Record<string, DatabaseHealth> = healthData?.databases ?? {};
  const healthEntries: DashboardDbEntry[] = Object.entries(dbHealthMap).map(([alias, h]) => ({
    alias,
    status: h.status === 'ok' ? 'connected' : 'error',
  }));
  const totalDbs = databases.length || healthEntries.length;
  const connectedCount = healthEntries.filter((h) => h.status === 'connected').length;
  const errorCount = healthEntries.filter((h) => h.status === 'error').length;

  const isLoading = healthQuery.isLoading || dbsQuery.isLoading;
  const isError = healthQuery.isError || dbsQuery.isError;

  return (
    <div className="dashboard">
      <div className="page-header">
        <h1>Dashboard</h1>
        <p className="page-subtitle">Overview of database connections and health</p>
      </div>

      {/* Summary cards */}
      <div className="summary-cards">
        <div className="summary-card">
          <div className="summary-card__value">{totalDbs}</div>
          <div className="summary-card__label">Total Databases</div>
        </div>
        <div className="summary-card">
          <div className="summary-card__value" style={{ color: 'var(--accent-green)' }}>{connectedCount}</div>
          <div className="summary-card__label">Connected</div>
        </div>
        <div className="summary-card">
          <div className="summary-card__value" style={{ color: 'var(--accent-red)' }}>{errorCount}</div>
          <div className="summary-card__label">Errors</div>
        </div>
      </div>

      {/* Status */}
      {isLoading && <div className="loading-message">Loading database health...</div>}
      {isError && (
        <div className="error-banner">
          Failed to load health data. Is the Query Service running?
        </div>
      )}

      {/* DB health grid */}
      {healthEntries.length > 0 && (
        <>
          <h2 className="section-title">Database Status</h2>
          <div className="db-grid">
            {healthEntries.map((db) => (
              <div key={db.alias} className={`db-card ${db.status === 'error' ? 'db-card--error' : ''}`}>
                <div className="db-card__header">
                  <span className={`status-dot ${db.status === 'connected' ? 'status-dot--green' : 'status-dot--red'}`} />
                  <span className="db-card__alias">{db.alias}</span>
                </div>
                <div className="db-card__body">
                  {db.status === 'connected' ? (
                    <div className="db-card__connected">Connected</div>
                  ) : (
                    <div className="db-card__error">Connection failed</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Empty state */}
      {!isLoading && healthEntries.length === 0 && !isError && (
        <div className="empty-state">
          <h3>No databases configured</h3>
          <p>Go to the Connections page to add your first database.</p>
        </div>
      )}
    </div>
  );
}

export default Dashboard;
