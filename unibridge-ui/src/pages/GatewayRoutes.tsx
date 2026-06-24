import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getGatewayRoutes,
  deleteGatewayRoute,
  testGatewayRoute,
  getGatewayRouteCurl,
  getGatewayOpenApiSpec,
  type GatewayRoute,
} from '../api/client';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import ResourceModal from '../components/ResourceModal';
import './GatewayRoutes.css';

const METHOD_COLORS: Record<string, string> = {
  GET: 'get', POST: 'post', PUT: 'put', DELETE: 'delete', PATCH: 'patch',
};

function routeServiceKeys(route: GatewayRoute) {
  if (route.service_keys?.length) return route.service_keys;
  return route.service_key ? [route.service_key] : [];
}

function GatewayRoutes() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canWrite = useCanWrite('gateway.routes.write');

  const { addToast } = useToast();
  const [testStatus, setTestStatus] = useState<Record<string, 'ok' | 'fail'>>({});
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set());
  const [curlModal, setCurlModal] = useState<{ routeName: string; curl: string } | null>(null);
  const [curlCopied, setCurlCopied] = useState(false);
  const [routeSearch, setRouteSearch] = useState('');
  const curlCopyTimeoutRef = useRef<number | null>(null);

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
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined;
      addToast({ type: 'error', title: t('gatewayRoutes.deleteFailed'), message: detail });
    },
  });

  const routes = routesQuery.data?.items ?? [];
  const normalizedRouteSearch = routeSearch.trim().toLowerCase();
  const filteredRoutes = normalizedRouteSearch
    ? routes.filter((route) => {
        const serviceKeyHeaders = routeServiceKeys(route).map((sk) => sk.header_name);
        return [
          route.name,
          route.uri,
          route.upstream_id,
          route.status === 1 ? 'active' : 'disabled',
          ...(route.methods || ['ALL']),
          ...serviceKeyHeaders,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
          .includes(normalizedRouteSearch);
      })
    : routes;

  function handleDelete(route: GatewayRoute) {
    const name = route.name || route.uri;
    if (window.confirm(t('gatewayRoutes.deleteConfirm', { name }))) {
      deleteMutation.mutate(route.id);
    }
  }

  function clearCurlCopyTimer() {
    if (curlCopyTimeoutRef.current !== null) {
      window.clearTimeout(curlCopyTimeoutRef.current);
      curlCopyTimeoutRef.current = null;
    }
  }

  useEffect(() => {
    return () => {
      clearCurlCopyTimer();
    };
  }, []);

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
      clearCurlCopyTimer();
      setCurlModal({ routeName: route.name || route.uri, curl });
      setCurlCopied(false);
    } catch {
      addToast({ type: 'error', title: t('gatewayRoutes.curlFailed') });
    }
  }

  async function handleCopy() {
    if (curlModal) {
      try {
        clearCurlCopyTimer();
        await navigator.clipboard.writeText(curlModal.curl);
        setCurlCopied(true);
        curlCopyTimeoutRef.current = window.setTimeout(() => {
          setCurlCopied(false);
          curlCopyTimeoutRef.current = null;
        }, 2000);
      } catch {
        setCurlCopied(false);
        addToast({ type: 'error', title: t('connections.copyFailed') });
      }
    }
  }

  function closeCurlModal() {
    clearCurlCopyTimer();
    setCurlCopied(false);
    setCurlModal(null);
  }

  async function handleOpenApiDownload() {
    try {
      const spec = await getGatewayOpenApiSpec();
      const blob = new Blob([JSON.stringify(spec, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'unibridge-gateway-openapi.json';
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      addToast({ type: 'error', title: t('gatewayRoutes.openApiFailed') });
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
        <div className="page-header-actions">
          {routes.length > 0 && (
            <input
              className="route-search-input"
              type="search"
              value={routeSearch}
              onChange={(event) => setRouteSearch(event.target.value)}
              placeholder={t('gatewayRoutes.searchPlaceholder')}
              aria-label={t('gatewayRoutes.searchPlaceholder')}
            />
          )}
          <button type="button" className="btn btn-secondary" onClick={handleOpenApiDownload}>
            {t('gatewayRoutes.openApiDownload')}
          </button>
          {canWrite && (
            <button type="button" className="btn btn-primary" onClick={() => navigate('/gateway/routes/new')}>
              {t('gatewayRoutes.addRoute')}
            </button>
          )}
        </div>
      </div>

      {routesQuery.isLoading && <div className="loading-message" role="status">{t('gatewayRoutes.loadingRoutes')}</div>}

      {routesQuery.isError && (
        <div className="error-banner" role="alert">{t('gatewayRoutes.loadFailed')}</div>
      )}

      {routes.length > 0 && filteredRoutes.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('common.name')}</th>
                <th scope="col">{t('gatewayRoutes.uri')}</th>
                <th scope="col">{t('gatewayRoutes.methods')}</th>
                <th scope="col">{t('gatewayRoutes.upstream')}</th>
                <th scope="col">{t('gatewayRoutes.serviceKey')}</th>
                <th scope="col">{t('common.status')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredRoutes.map((route) => {
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === route.id;
                return (
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
                    {routeServiceKeys(route).length > 0 ? (
                      <div className="service-key-cell">
                        {routeServiceKeys(route).map((sk) => (
                          <div key={sk.header_name} className="service-key-cell-item">
                            <span className="service-key-cell-name">{sk.header_name}</span>
                            <span className="service-key-cell-value" title={t('gatewayRoutes.serviceKeyHidden')}>
                              {t('gatewayRoutes.serviceKeyHidden')}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      '—'
                    )}
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
                        type="button"
                        className="btn btn-sm btn-outline"
                        aria-label={t('gatewayRoutes.testRoute', { name: route.name || route.uri })}
                        onClick={() => handleTest(route.id, route.name || route.uri)}
                        disabled={testingIds.has(route.id)}
                      >
                        {t('gatewayRoutes.test')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-outline"
                        aria-label={t('gatewayRoutes.showCurl', { name: route.name || route.uri })}
                        onClick={() => handleCurl(route)}
                      >
                        {t('gatewayRoutes.curl')}
                      </button>
                      {canWrite && !route.system && (
                        <>
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            aria-label={t('gatewayRoutes.editRoute', { name: route.name || route.uri })}
                            onClick={() => navigate(`/gateway/routes/${route.id}/edit`)}
                          >
                            {t('common.edit')}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            aria-label={t('gatewayRoutes.deleteRoute', { name: route.name || route.uri })}
                            onClick={() => handleDelete(route)}
                            disabled={deleteMutation.isPending}
                            aria-busy={isDeleting}
                          >
                            {isDeleting ? t('common.deleting') : t('common.delete')}
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!routesQuery.isLoading && routes.length > 0 && filteredRoutes.length === 0 && !routesQuery.isError && (
        <div className="empty-state">
          <h3>{t('gatewayRoutes.noSearchResults')}</h3>
          <p>{t('gatewayRoutes.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setRouteSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!routesQuery.isLoading && routes.length === 0 && !routesQuery.isError && (
        <div className="empty-state">
          <h3>{t('gatewayRoutes.noRoutes')}</h3>
          <p>{t('gatewayRoutes.noRoutesDesc')}</p>
        </div>
      )}

      {curlModal && (
        <ResourceModal
          title={t('gatewayRoutes.curlTitle')}
          onClose={closeCurlModal}
          closeLabel={t('common.close')}
          className="modal--sm"
        >
          <div className="curl-block">
            <div className="curl-route-name">{curlModal.routeName}</div>
            <pre className="curl-code">{curlModal.curl}</pre>
            <button
              type="button"
              className="btn btn-sm btn-secondary curl-copy-btn"
              onClick={handleCopy}
              aria-label={curlCopied ? t('gatewayRoutes.curlCopiedLabel') : t('gatewayRoutes.curlCopyLabel')}
            >
              {curlCopied ? t('gatewayRoutes.curlCopied') : t('gatewayRoutes.curlCopy')}
            </button>
            <span className="visually-hidden" role="status" aria-live="polite">
              {curlCopied ? t('gatewayRoutes.curlCopied') : ''}
            </span>
          </div>
        </ResourceModal>
      )}
    </div>
  );
}

export default GatewayRoutes;
