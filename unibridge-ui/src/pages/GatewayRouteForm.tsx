import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getGatewayRoute,
  saveGatewayRoute,
  getGatewayUpstreams,
  type GatewayRoute,
  type GatewayUpstream,
} from '../api/client';
import './GatewayRouteForm.css';

const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'];

function routeFormKey(route: GatewayRoute | undefined, isEdit: boolean): string {
  if (!isEdit) return 'new';
  if (!route) return 'loading';
  return [
    route.id,
    route.name ?? '',
    route.uri,
    route.methods?.join('\0') ?? '',
    route.upstream_id ?? '',
    route.status,
    String(route.require_auth),
    String(route.strip_prefix),
    route.service_key?.header_name ?? '',
  ].join(':');
}

function GatewayRouteEditor({
  id,
  isEdit,
  initialRoute,
  upstreams,
}: {
  id: string | undefined;
  isEdit: boolean;
  initialRoute: GatewayRoute | undefined;
  upstreams: GatewayUpstream[];
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState(initialRoute?.name || '');
  const [uriSuffix, setUriSuffix] = useState((initialRoute?.uri || '').replace(/^\/api\//, ''));
  const [methods, setMethods] = useState<string[]>(initialRoute?.methods || ['GET', 'POST']);
  const [upstreamId, setUpstreamId] = useState(initialRoute?.upstream_id || '');
  const [statusVal, setStatusVal] = useState(initialRoute?.status ?? 1);
  const [requireAuth, setRequireAuth] = useState(!!initialRoute?.require_auth);
  const [stripPrefix, setStripPrefix] = useState(initialRoute ? !!initialRoute.strip_prefix : true);
  const [keyHeader, setKeyHeader] = useState(initialRoute?.service_key?.header_name || '');
  const [keyValue, setKeyValue] = useState('');
  const [error, setError] = useState('');

  const saveMutation = useMutation({
    mutationFn: (data: { routeId: string; body: Record<string, unknown> }) =>
      saveGatewayRoute(data.routeId, data.body),
    onSuccess: (savedRoute, data) => {
      queryClient.setQueryData(['gateway-route', data.routeId], savedRoute);
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
      navigate('/gateway/routes');
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? t('gatewayRouteForm.saveFailed'));
      } else {
        setError(t('gatewayRouteForm.saveFailed'));
      }
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!uriSuffix.trim() || !upstreamId) return;

    const uri = `/api/${uriSuffix.replace(/^\/+/, '')}`;
    const routeId = id || crypto.randomUUID();
    const body: Record<string, unknown> = {
      name: name.trim() || undefined,
      uri,
      methods,
      upstream_id: upstreamId || undefined,
      status: statusVal,
      require_auth: requireAuth,
      strip_prefix: stripPrefix,
    };

    if (keyHeader.trim()) {
      if (keyValue.trim()) {
        body.service_key = {
          header_name: keyHeader.trim(),
          header_value: keyValue.trim(),
        };
      } else if (!isEdit) {
        // Only require value for new routes; editing without value preserves existing
        body.service_key = {
          header_name: keyHeader.trim(),
          header_value: '',
        };
      }
      // When editing and keyValue is empty, omit service_key to preserve existing value
    }

    setError('');
    saveMutation.mutate({ routeId, body });
  }

  function toggleMethod(method: string) {
    setMethods((prev) =>
      prev.includes(method) ? prev.filter((m) => m !== method) : [...prev, method]
    );
  }

  return (
    <div className="route-form">
      <div className="page-header">
        <h1>{isEdit ? t('gatewayRouteForm.editTitle') : t('gatewayRouteForm.newTitle')}</h1>
        <p className="page-subtitle">{isEdit ? t('gatewayRouteForm.editSubtitle', { id }) : t('gatewayRouteForm.newSubtitle')}</p>
      </div>

      <form onSubmit={handleSubmit}>
        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.basicInfo')}</div>
          <div className="form-row">
            <div className="field">
              <label>{t('common.name')}</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My API Route" />
            </div>
            <div className="field">
              <label>{t('common.status')}</label>
              <select value={statusVal} onChange={(e) => setStatusVal(Number(e.target.value))}>
                <option value={1}>{t('common.active')}</option>
                <option value={0}>{t('common.disabled')}</option>
              </select>
            </div>
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>URI</label>
              <div className="uri-input-group">
                <span className="uri-prefix">/api/</span>
                <input value={uriSuffix} onChange={(e) => setUriSuffix(e.target.value)} placeholder="myservice/*" required />
              </div>
              <span className="field-hint">{t('gatewayRouteForm.uriHint')}</span>
            </div>
          </div>
          <div className="field">
            <label>Methods</label>
            <div className="methods-group">
              {ALL_METHODS.map((m) => (
                <label key={m} className="method-check">
                  <input type="checkbox" checked={methods.includes(m)} onChange={() => toggleMethod(m)} />
                  {m}
                </label>
              ))}
            </div>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.upstream')}</div>
          <div className="routing-flow-hint">
            {uriSuffix.trim() ? (
              <>
                <code>/api/{uriSuffix.replace(/\/?\*$/, '')}/users</code>
                <span className="routing-flow-arrow">→</span>
                <span>{upstreamId ? (upstreams.find(u => u.id === upstreamId)?.name || upstreamId) : '(Upstream)'}</span>
                <code>{stripPrefix ? '/users' : `/api/${uriSuffix.replace(/\/?\*$/, '')}/users`}</code>
              </>
            ) : (
              <>
                <code>/api/service/users</code>
                <span className="routing-flow-arrow">→</span>
                <span>{upstreamId ? (upstreams.find(u => u.id === upstreamId)?.name || upstreamId) : '(Upstream)'}</span>
                <code>{stripPrefix ? '/users' : '/api/service/users'}</code>
              </>
            )}
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>{t('gatewayRouteForm.upstream')}</label>
              <select value={upstreamId} onChange={(e) => setUpstreamId(e.target.value)}>
                <option value="">{t('gatewayRouteForm.selectUpstream')}</option>
                {upstreams.map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.id}</option>
                ))}
              </select>
            </div>
          </div>
          <label className="method-check" style={{ marginTop: 12 }}>
            <input type="checkbox" checked={stripPrefix} onChange={(e) => setStripPrefix(e.target.checked)} />
            {t('gatewayRouteForm.stripPrefix')}
          </label>
        </div>

        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.authentication')}</div>
          <label className="method-check">
            <input type="checkbox" checked={requireAuth} onChange={(e) => setRequireAuth(e.target.checked)} />
            {t('gatewayRouteForm.requireAuth')}
          </label>
          <span className="field-hint">{t('gatewayRouteForm.requireAuthHint')}</span>
        </div>

        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.serviceKeyTitle')}</div>
          <span className="field-hint" style={{ marginBottom: 12, display: 'block' }}>
            {t('gatewayRouteForm.serviceKeyDesc')}
          </span>
          <div className="form-row">
            <div className="field">
              <label>{t('gatewayRouteForm.headerName')}</label>
              <input value={keyHeader} onChange={(e) => setKeyHeader(e.target.value)} placeholder="Authorization" />
            </div>
            <div className="field">
              <label>{t('gatewayRouteForm.headerValue')}</label>
              <input
                type="password"
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                placeholder={isEdit ? t('gatewayRouteForm.headerValueEditPlaceholder') : t('gatewayRouteForm.headerValueNewHint')}
              />
              {isEdit && <span className="field-hint">{t('gatewayRouteForm.headerValueEditHint')}</span>}
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={() => navigate('/gateway/routes')}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending || !upstreamId}>
            {saveMutation.isPending ? t('common.saving') : isEdit ? t('gatewayRouteForm.updateRoute') : t('gatewayRouteForm.createRoute')}
          </button>
        </div>
      </form>
    </div>
  );
}

function GatewayRouteForm() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const isEdit = !!id;

  const routeQuery = useQuery({
    queryKey: ['gateway-route', id],
    queryFn: () => getGatewayRoute(id!),
    enabled: isEdit,
  });

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  if (isEdit && routeQuery.isLoading) {
    return <div className="loading-message">{t('gatewayRouteForm.loadingRoute')}</div>;
  }

  return (
    <GatewayRouteEditor
      key={routeFormKey(routeQuery.data, isEdit)}
      id={id}
      isEdit={isEdit}
      initialRoute={routeQuery.data}
      upstreams={upstreamsQuery.data?.items ?? []}
    />
  );
}

export default GatewayRouteForm;
