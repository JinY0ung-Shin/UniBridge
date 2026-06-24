import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getGatewayRoute,
  saveGatewayRoute,
  getGatewayUpstreams,
  getAlertResourceOwners,
  setAlertResourceOwner,
  type GatewayRoute,
  type GatewayUpstream,
} from '../api/client';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import './GatewayRouteForm.css';

const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'];

function parseEmails(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((email) => email.trim())
    .filter(Boolean);
}

interface ServiceKeyRow {
  rowKey: string;
  header_name: string;
  header_value: string;
  isExisting: boolean;
  existingPlaceholder: string;
}

function makeRowKey(): string {
  return crypto.randomUUID();
}

function routeServiceKeys(route: GatewayRoute | undefined) {
  if (!route) return [];
  if (route.service_keys?.length) return route.service_keys;
  return route.service_key ? [route.service_key] : [];
}

function initialServiceKeys(route: GatewayRoute | undefined): ServiceKeyRow[] {
  return routeServiceKeys(route).map((sk) => ({
    rowKey: makeRowKey(),
    header_name: sk.header_name,
    header_value: '',
    isExisting: true,
    existingPlaceholder: sk.header_value,
  }));
}

function routeFormKey(route: GatewayRoute | undefined, isEdit: boolean): string {
  if (!isEdit) return 'new';
  if (!route) return 'loading';
  const serviceKeySignature = JSON.stringify(
    routeServiceKeys(route).map((key) => [key.header_name, key.header_value]),
  );
  return JSON.stringify([
    route.id,
    route.name ?? '',
    route.uri,
    route.methods ?? [],
    route.upstream_id ?? '',
    route.status,
    route.require_auth,
    route.strip_prefix,
    route.timeout_override,
    route.timeout_seconds,
    serviceKeySignature,
  ]);
}

function GatewayRouteEditor({
  id,
  isEdit,
  initialRoute,
  initialAssignees,
  assigneesReady,
  canReadAlerts,
  canManageAlerts,
  upstreams,
}: {
  id: string | undefined;
  isEdit: boolean;
  initialRoute: GatewayRoute | undefined;
  initialAssignees: string;
  assigneesReady: boolean;
  canReadAlerts: boolean;
  canManageAlerts: boolean;
  upstreams: GatewayUpstream[];
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [name, setName] = useState(initialRoute?.name || '');
  const [uriSuffix, setUriSuffix] = useState((initialRoute?.uri || '').replace(/^\/api\//, ''));
  const [methods, setMethods] = useState<string[]>(initialRoute?.methods || ['GET', 'POST']);
  const [upstreamId, setUpstreamId] = useState(initialRoute?.upstream_id || '');
  const [statusVal, setStatusVal] = useState(initialRoute?.status ?? 1);
  const [requireAuth, setRequireAuth] = useState(!!initialRoute?.require_auth);
  const [stripPrefix, setStripPrefix] = useState(initialRoute ? !!initialRoute.strip_prefix : true);
  // Blank = inherit the global gateway default; a number is a per-route override.
  const [timeoutInput, setTimeoutInput] = useState(
    initialRoute?.timeout_override ? String(initialRoute.timeout_seconds ?? '') : '',
  );
  const [serviceKeys, setServiceKeys] = useState<ServiceKeyRow[]>(() => initialServiceKeys(initialRoute));
  const [assignees, setAssignees] = useState(initialAssignees);
  const [error, setError] = useState('');

  const saveMutation = useMutation({
    mutationFn: (data: { routeId: string; body: Record<string, unknown> }) =>
      saveGatewayRoute(data.routeId, data.body),
    onSuccess: async (savedRoute, data) => {
      queryClient.setQueryData(['gateway-route', data.routeId], savedRoute);
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
      // Only touch assignees when allowed and the baseline is known, and only
      // when actually changed — never overwrite assignees we couldn't load.
      if (canManageAlerts && assigneesReady) {
        const next = parseEmails(assignees);
        if (JSON.stringify(next) !== JSON.stringify(parseEmails(initialAssignees))) {
          try {
            await setAlertResourceOwner('route', data.routeId, { emails: next });
            queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
          } catch {
            addToast({ type: 'error', title: t('gatewayRouteForm.assignees'), message: t('common.errorOccurred') });
          }
        }
      }
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

    const service_keys = serviceKeys
      .map((row) => {
        const headerName = row.header_name.trim();
        const headerValue = row.header_value.trim();
        if (!headerName) return null;
        if (!row.isExisting && !headerValue) return null;
        return { header_name: headerName, header_value: headerValue };
      })
      .filter((entry): entry is { header_name: string; header_value: string } => entry !== null);

    const body: Record<string, unknown> = {
      name: name.trim() || undefined,
      uri,
      methods,
      upstream_id: upstreamId || undefined,
      status: statusVal,
      require_auth: requireAuth,
      strip_prefix: stripPrefix,
      timeout: timeoutInput.trim() === '' ? null : Number(timeoutInput),
      service_keys,
    };

    setError('');
    saveMutation.mutate({ routeId, body });
  }

  function toggleMethod(method: string) {
    setMethods((prev) =>
      prev.includes(method) ? prev.filter((m) => m !== method) : [...prev, method]
    );
  }

  function updateKeyRow(rowKey: string, patch: Partial<Pick<ServiceKeyRow, 'header_name' | 'header_value'>>) {
    setServiceKeys((prev) =>
      prev.map((row) => {
        if (row.rowKey !== rowKey) return row;
        const next = { ...row, ...patch };
        // If the user renamed an existing row, we can no longer preserve the
        // old secret under the new key; force them to type a fresh value.
        if (
          patch.header_name !== undefined &&
          row.isExisting &&
          patch.header_name.trim() !== row.header_name.trim()
        ) {
          next.isExisting = false;
          next.existingPlaceholder = '';
        }
        return next;
      })
    );
  }

  function addKeyRow() {
    setServiceKeys((prev) => [
      ...prev,
      {
        rowKey: makeRowKey(),
        header_name: '',
        header_value: '',
        isExisting: false,
        existingPlaceholder: '',
      },
    ]);
  }

  function removeKeyRow(rowKey: string) {
    setServiceKeys((prev) => prev.filter((row) => row.rowKey !== rowKey));
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
              <label htmlFor="gateway-route-name">{t('common.name')}</label>
              <input id="gateway-route-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="My API Route" aria-label={t('common.name')} />
            </div>
            <div className="field">
              <label htmlFor="gateway-route-status">{t('common.status')}</label>
              <select
                id="gateway-route-status"
                value={statusVal}
                onChange={(e) => setStatusVal(Number(e.target.value))}
                aria-label={t('common.status')}
              >
                <option value={1}>{t('common.active')}</option>
                <option value={0}>{t('common.disabled')}</option>
              </select>
            </div>
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label htmlFor="gateway-route-uri">{t('gatewayRouteForm.uri')}</label>
              <div className="uri-input-group">
                <span className="uri-prefix">/api/</span>
                <input
                  id="gateway-route-uri"
                  value={uriSuffix}
                  onChange={(e) => setUriSuffix(e.target.value)}
                  placeholder="myservice/*"
                  aria-label={t('gatewayRouteForm.uri')}
                  aria-describedby="gateway-route-uri-hint"
                  required
                />
              </div>
              <span id="gateway-route-uri-hint" className="field-hint">{t('gatewayRouteForm.uriHint')}</span>
            </div>
          </div>
          <div className="field">
            <label id="gateway-route-methods-label">{t('gatewayRouteForm.methods')}</label>
            <div className="methods-group" role="group" aria-labelledby="gateway-route-methods-label">
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
                <span>{upstreamId ? (upstreams.find(u => u.id === upstreamId)?.name || upstreamId) : t('gatewayRouteForm.upstreamPreviewPlaceholder')}</span>
                <code>{stripPrefix ? '/users' : `/api/${uriSuffix.replace(/\/?\*$/, '')}/users`}</code>
              </>
            ) : (
              <>
                <code>/api/service/users</code>
                <span className="routing-flow-arrow">→</span>
                <span>{upstreamId ? (upstreams.find(u => u.id === upstreamId)?.name || upstreamId) : t('gatewayRouteForm.upstreamPreviewPlaceholder')}</span>
                <code>{stripPrefix ? '/users' : '/api/service/users'}</code>
              </>
            )}
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label htmlFor="gateway-route-upstream">{t('gatewayRouteForm.upstream')}</label>
              <select id="gateway-route-upstream" value={upstreamId} onChange={(e) => setUpstreamId(e.target.value)} aria-label={t('gatewayRouteForm.upstream')}>
                <option value="">{t('gatewayRouteForm.selectUpstream')}</option>
                {upstreams.map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.id}</option>
                ))}
              </select>
            </div>
          </div>
          <label className="method-check" style={{ marginTop: 12 }}>
            <input
              id="gateway-route-strip-prefix"
              type="checkbox"
              checked={stripPrefix}
              onChange={(e) => setStripPrefix(e.target.checked)}
            />
            {t('gatewayRouteForm.stripPrefix')}
          </label>
          <div className="field" style={{ marginTop: 12 }}>
            <label htmlFor="gateway-route-timeout">{t('gatewayRouteForm.timeout')}</label>
            <input
              id="gateway-route-timeout"
              type="number"
              min={1}
              max={3600}
              value={timeoutInput}
              onChange={(e) => setTimeoutInput(e.target.value)}
              placeholder={t('gatewayRouteForm.timeoutPlaceholder')}
              aria-label={t('gatewayRouteForm.timeout')}
              aria-describedby="gateway-route-timeout-hint"
            />
            <span id="gateway-route-timeout-hint" className="field-hint">{t('gatewayRouteForm.timeoutHint')}</span>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.authentication')}</div>
          <label className="method-check">
            <input
              id="gateway-route-require-auth"
              type="checkbox"
              checked={requireAuth}
              onChange={(e) => setRequireAuth(e.target.checked)}
              aria-describedby="gateway-route-require-auth-hint"
            />
            {t('gatewayRouteForm.requireAuth')}
          </label>
          <span id="gateway-route-require-auth-hint" className="field-hint">
            {t('gatewayRouteForm.requireAuthHint')}
          </span>
        </div>

        <div className="form-section">
          <div className="form-section-title">{t('gatewayRouteForm.serviceKeyTitle')}</div>
          <span id="gateway-route-service-key-help" className="field-hint" style={{ marginBottom: 12, display: 'block' }}>
            {t('gatewayRouteForm.serviceKeyDesc')}
          </span>

          {serviceKeys.length > 0 && (
            <div className="service-key-list">
              {serviceKeys.map((row, idx) => (
                <div className="service-key-row" key={row.rowKey}>
                  <div className="field service-key-name">
                    <label htmlFor={`gateway-route-header-name-${idx + 1}`}>{t('gatewayRouteForm.headerName')}</label>
                    <input
                      id={`gateway-route-header-name-${idx + 1}`}
                      value={row.header_name}
                      onChange={(e) => updateKeyRow(row.rowKey, { header_name: e.target.value })}
                      placeholder="Authorization"
                      aria-label={`${t('gatewayRouteForm.headerName')} ${idx + 1}`}
                      aria-describedby="gateway-route-service-key-help"
                    />
                  </div>
                  <div className="field service-key-value">
                    <label htmlFor={`gateway-route-header-value-${idx + 1}`}>{t('gatewayRouteForm.headerValue')}</label>
                    <input
                      id={`gateway-route-header-value-${idx + 1}`}
                      type="password"
                      value={row.header_value}
                      onChange={(e) => updateKeyRow(row.rowKey, { header_value: e.target.value })}
                      placeholder={
                        row.isExisting
                          ? row.existingPlaceholder || t('gatewayRouteForm.headerValueEditPlaceholder')
                          : t('gatewayRouteForm.headerValueNewHint')
                      }
                      aria-label={`${t('gatewayRouteForm.headerValue')} ${idx + 1}`}
                      aria-describedby={
                        row.isExisting
                          ? `gateway-route-header-value-${idx + 1}-hint gateway-route-service-key-help`
                          : 'gateway-route-service-key-help'
                      }
                    />
                    {row.isExisting && (
                      <span id={`gateway-route-header-value-${idx + 1}-hint`} className="field-hint">
                        {t('gatewayRouteForm.headerValueEditHint')}
                      </span>
                    )}
                  </div>
                  <button
                    type="button"
                    className="btn btn-sm btn-danger service-key-remove"
                    onClick={() => removeKeyRow(row.rowKey)}
                    aria-label={t('gatewayRouteForm.removeHeader', { index: idx + 1 })}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <button type="button" className="btn btn-sm btn-secondary service-key-add" onClick={addKeyRow}>
            {t('gatewayRouteForm.addHeader')}
          </button>
        </div>

        {canReadAlerts && (
          <div className="form-section">
            <div className="form-section-title">{t('gatewayRouteForm.assigneesSection')}</div>
            <div className="form-row form-row--full">
              <div className="field">
                <label htmlFor="gateway-route-assignees">{t('gatewayRouteForm.assignees')}</label>
                <textarea
                  id="gateway-route-assignees"
                  value={assignees}
                  onChange={(e) => setAssignees(e.target.value)}
                  rows={2}
                  disabled={!canManageAlerts || !assigneesReady}
                  placeholder="alice@example.com, bob@example.com"
                  aria-label={t('gatewayRouteForm.assignees')}
                  aria-describedby="gateway-route-assignees-hint"
                />
                <span id="gateway-route-assignees-hint" className="field-hint">{t('gatewayRouteForm.assigneesHint')}</span>
              </div>
            </div>
          </div>
        )}

        {error && <div className="error-banner" role="alert">{error}</div>}

        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={() => navigate('/gateway/routes')}>
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saveMutation.isPending || !upstreamId}
            aria-busy={saveMutation.isPending}
          >
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
  const canReadAlerts = useCanWrite('alerts.read');
  const canManageAlerts = useCanWrite('alerts.write');

  const routeQuery = useQuery({
    queryKey: ['gateway-route', id],
    queryFn: () => getGatewayRoute(id!),
    enabled: isEdit,
  });

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  const ownersQuery = useQuery({
    queryKey: ['alert-resource-owners'],
    queryFn: getAlertResourceOwners,
    enabled: canReadAlerts,
  });

  if (isEdit && routeQuery.isLoading) {
    return <div className="loading-message" role="status">{t('gatewayRouteForm.loadingRoute')}</div>;
  }
  // For edits, wait until existing assignees are loaded before mounting the
  // editor so its initial value is correct AND stable (no late remount that
  // would discard in-progress edits, no '' baseline that would wipe assignees).
  if (isEdit && canReadAlerts && ownersQuery.isLoading) {
    return <div className="loading-message" role="status">{t('gatewayRouteForm.loadingRoute')}</div>;
  }

  // Known baseline only when create (no existing owner) or owners loaded ok.
  const assigneesReady = isEdit ? ownersQuery.isSuccess : true;
  const initialAssignees = isEdit
    ? ((ownersQuery.data ?? []).find(
        (o) => o.resource_type === 'route' && o.resource_id === id,
      )?.emails.join(', ') ?? '')
    : '';

  return (
    <GatewayRouteEditor
      key={routeFormKey(routeQuery.data, isEdit)}
      id={id}
      isEdit={isEdit}
      initialRoute={routeQuery.data}
      initialAssignees={initialAssignees}
      assigneesReady={assigneesReady}
      canReadAlerts={canReadAlerts}
      canManageAlerts={canManageAlerts}
      upstreams={upstreamsQuery.data?.items ?? []}
    />
  );
}

export default GatewayRouteForm;
