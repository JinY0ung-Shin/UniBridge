import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { getGatewayRoutes, deleteGatewayRoute, type GatewayRoute } from '../api/client';
import './GatewayRoutes.css';

const METHOD_COLORS: Record<string, string> = {
  GET: 'get', POST: 'post', PUT: 'put', DELETE: 'delete', PATCH: 'patch',
};

function GatewayRoutes() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const routesQuery = useQuery({
    queryKey: ['gateway-routes'],
    queryFn: getGatewayRoutes,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGatewayRoute(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? 'Failed to delete route');
      } else {
        alert('Failed to delete route');
      }
    },
  });

  const routes = routesQuery.data?.items ?? [];

  function handleDelete(route: GatewayRoute) {
    const name = route.name || route.uri;
    if (window.confirm(`Delete route "${name}"? This cannot be undone.`)) {
      deleteMutation.mutate(route.id);
    }
  }

  return (
    <div className="gateway-routes">
      <div className="page-header">
        <div>
          <h1>Gateway Routes</h1>
          <p className="page-subtitle">Manage API gateway routing rules</p>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/gateway/routes/new')}>
          + Add Route
        </button>
      </div>

      {routesQuery.isLoading && <div className="loading-message">Loading routes...</div>}

      {routesQuery.isError && (
        <div className="error-banner">Failed to load gateway routes.</div>
      )}

      {routes.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>URI</th>
                <th>Methods</th>
                <th>Upstream</th>
                <th>Service Key</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {routes.map((route) => (
                <tr key={route.id}>
                  <td className="cell-alias">{route.name || '—'}</td>
                  <td className="cell-uri">{route.uri}</td>
                  <td>
                    <div className="method-badges">
                      {(route.methods || ['ALL']).map((m) => (
                        <span key={m} className={`method-badge method-badge--${METHOD_COLORS[m] || 'patch'}`}>
                          {m}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>{route.upstream_id || '—'}</td>
                  <td className="cell-service-key">
                    {route.service_key ? `${route.service_key.header_name}: ${route.service_key.header_value}` : '—'}
                  </td>
                  <td>
                    <span className={`badge ${route.status === 1 ? 'badge-ok' : 'badge-unknown'}`}>
                      {route.status === 1 ? 'Active' : 'Disabled'}
                    </span>
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => navigate(`/gateway/routes/${route.id}/edit`)}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => handleDelete(route)}
                        disabled={deleteMutation.isPending}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!routesQuery.isLoading && routes.length === 0 && !routesQuery.isError && (
        <div className="empty-state">
          <h3>No gateway routes</h3>
          <p>Click "Add Route" to create your first API route.</p>
        </div>
      )}
    </div>
  );
}

export default GatewayRoutes;
