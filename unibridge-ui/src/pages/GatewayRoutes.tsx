import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getGatewayRoutes,
  deleteGatewayRoute,
  testGatewayRoute,
  getGatewayRouteCurl,
  type GatewayRoute,
} from '../api/client';
import { useToast } from '../components/ToastContext';
import './GatewayRoutes.css';

const METHOD_COLORS: Record<string, string> = {
  GET: 'get', POST: 'post', PUT: 'put', DELETE: 'delete', PATCH: 'patch',
};

function GatewayRoutes() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { addToast } = useToast();
  const [testStatus, setTestStatus] = useState<Record<string, 'ok' | 'fail'>>({});
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set());
  const [curlModal, setCurlModal] = useState<{ routeName: string; curl: string } | null>(null);
  const [curlCopied, setCurlCopied] = useState(false);

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
        alert(axiosErr.response?.data?.detail ?? t('gatewayRoutes.deleteFailed'));
      } else {
        alert(t('gatewayRoutes.deleteFailed'));
      }
    },
  });

  const routes = routesQuery.data?.items ?? [];

  function handleDelete(route: GatewayRoute) {
    const name = route.name || route.uri;
    if (window.confirm(t('gatewayRoutes.deleteConfirm', { name }))) {
      deleteMutation.mutate(route.id);
    }
  }

  async function handleTest(routeId: string, routeName: string) {
    setTestingIds((prev) => new Set(prev).add(routeId));
    try {
      const result = await testGatewayRoute(routeId);
      setTestStatus((prev) => ({ ...prev, [routeId]: result.reachable ? 'ok' : 'fail' }));
      if (result.reachable) {
        const bodyStr = typeof result.body === 'string' ? result.body : JSON.stringify(result.body, null, 2);
        addToast({
          type: 'success',
          title: `${routeName} — ${result.status_code} (${result.response_time_ms}ms)`,
          message: `${result.node}\n${bodyStr || ''}`.trim(),
        });
      } else {
        addToast({
          type: 'error',
          title: `${routeName} — ${t('gatewayRoutes.testUnreachable')}`,
          message: `${result.node}\n${result.error || ''}`.trim(),
        });
      }
    } catch {
      setTestStatus((prev) => ({ ...prev, [routeId]: 'fail' }));
      addToast({ type: 'error', title: `${routeName} — ${t('gatewayRoutes.testUnreachable')}` });
    } finally {
      setTestingIds((prev) => {
        const next = new Set(prev);
        next.delete(routeId);
        return next;
      });
    }
  }

  async function handleCurl(route: GatewayRoute) {
    try {
      const { curl } = await getGatewayRouteCurl(route.id);
      setCurlModal({ routeName: route.name || route.uri, curl });
      setCurlCopied(false);
    } catch {
      alert('Failed to generate cURL command');
    }
  }

  function handleCopy() {
    if (curlModal) {
      navigator.clipboard.writeText(curlModal.curl);
      setCurlCopied(true);
      setTimeout(() => setCurlCopied(false), 2000);
    }
  }

  function renderTestBadge(routeId: string) {
    if (testingIds.has(routeId)) {
      return <span className="test-result test-result--pending">{t('gatewayRoutes.testing')}</span>;
    }
    const status = testStatus[routeId];
    if (!status) return null;
    return (
      <span className={`test-result test-result--${status === 'ok' ? 'ok' : 'fail'}`}>
        {status === 'ok' ? t('common.ok') : t('common.error')}
      </span>
    );
  }

  return (
    <div className="gateway-routes">
      <div className="page-header">
        <div>
          <h1>{t('gatewayRoutes.title')}</h1>
          <p className="page-subtitle">{t('gatewayRoutes.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/gateway/routes/new')}>
          {t('gatewayRoutes.addRoute')}
        </button>
      </div>

      {routesQuery.isLoading && <div className="loading-message">{t('gatewayRoutes.loadingRoutes')}</div>}

      {routesQuery.isError && (
        <div className="error-banner">{t('gatewayRoutes.loadFailed')}</div>
      )}

      {routes.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('gatewayRoutes.uri')}</th>
                <th>{t('gatewayRoutes.methods')}</th>
                <th>{t('gatewayRoutes.upstream')}</th>
                <th>{t('gatewayRoutes.serviceKey')}</th>
                <th>{t('common.status')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {routes.map((route) => (
                <tr key={route.id}>
                  <td className="cell-alias">
                    {route.name || '—'}
                    {route.system && <span className="badge badge-system">System</span>}
                  </td>
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
                    <div className="status-cell">
                      <span className={`badge ${route.status === 1 ? 'badge-ok' : 'badge-unknown'}`}>
                        {route.status === 1 ? t('common.active') : t('common.disabled')}
                      </span>
                      {renderTestBadge(route.id)}
                    </div>
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-outline"
                        onClick={() => handleTest(route.id, route.name || route.uri)}
                        disabled={testingIds.has(route.id)}
                      >
                        {t('gatewayRoutes.test')}
                      </button>
                      <button
                        className="btn btn-sm btn-outline"
                        onClick={() => handleCurl(route)}
                      >
                        {t('gatewayRoutes.curl')}
                      </button>
                      {!route.system && (
                        <>
                          <button
                            className="btn btn-sm btn-secondary"
                            onClick={() => navigate(`/gateway/routes/${route.id}/edit`)}
                          >
                            {t('common.edit')}
                          </button>
                          <button
                            className="btn btn-sm btn-danger"
                            onClick={() => handleDelete(route)}
                            disabled={deleteMutation.isPending}
                          >
                            {t('common.delete')}
                          </button>
                        </>
                      )}
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
          <h3>{t('gatewayRoutes.noRoutes')}</h3>
          <p>{t('gatewayRoutes.noRoutesDesc')}</p>
        </div>
      )}

      {curlModal && (
        <div className="modal-overlay" onClick={() => setCurlModal(null)}>
          <div className="modal modal--sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{t('gatewayRoutes.curlTitle')}</h2>
              <button className="modal-close" onClick={() => setCurlModal(null)}>&times;</button>
            </div>
            <div className="curl-block">
              <pre className="curl-code">{curlModal.curl}</pre>
              <button className="btn btn-sm btn-secondary curl-copy-btn" onClick={handleCopy}>
                {curlCopied ? t('gatewayRoutes.curlCopied') : t('gatewayRoutes.curlCopy')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default GatewayRoutes;
