import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getGatewayRoute,
  saveGatewayRoute,
  getGatewayUpstreams,
} from '../api/client';
import './GatewayRouteForm.css';

const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'];

function GatewayRouteForm() {
  const { id } = useParams<{ id: string }>();
  const isEdit = !!id;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState('');
  const [uri, setUri] = useState('');
  const [methods, setMethods] = useState<string[]>(['GET', 'POST']);
  const [upstreamId, setUpstreamId] = useState('');
  const [statusVal, setStatusVal] = useState(1);
  const [requireAuth, setRequireAuth] = useState(false);
  const [keyHeader, setKeyHeader] = useState('');
  const [keyValue, setKeyValue] = useState('');
  const [error, setError] = useState('');

  const routeQuery = useQuery({
    queryKey: ['gateway-route', id],
    queryFn: () => getGatewayRoute(id!),
    enabled: isEdit,
  });

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  useEffect(() => {
    if (routeQuery.data) {
      const r = routeQuery.data;
      setName(r.name || '');
      setUri(r.uri || '');
      setMethods(r.methods || ['GET', 'POST']);
      setUpstreamId(r.upstream_id || '');
      setStatusVal(r.status ?? 1);
      setRequireAuth(!!(r as unknown as Record<string, unknown>).require_auth);
      if (r.service_key) {
        setKeyHeader(r.service_key.header_name || '');
      }
    }
  }, [routeQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (data: { routeId: string; body: Record<string, unknown> }) =>
      saveGatewayRoute(data.routeId, data.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
      navigate('/gateway/routes');
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Failed to save route');
      } else {
        setError('Failed to save route');
      }
    },
  });

  const upstreams = upstreamsQuery.data?.items ?? [];

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!uri.trim()) return;

    const routeId = id || Date.now().toString();
    const body: Record<string, unknown> = {
      name: name.trim() || undefined,
      uri: uri.trim(),
      methods,
      upstream_id: upstreamId || undefined,
      status: statusVal,
      require_auth: requireAuth,
    };

    if (keyHeader.trim() && keyValue.trim()) {
      body.service_key = {
        header_name: keyHeader.trim(),
        header_value: keyValue.trim(),
      };
    }

    setError('');
    saveMutation.mutate({ routeId, body });
  }

  function toggleMethod(method: string) {
    setMethods((prev) =>
      prev.includes(method) ? prev.filter((m) => m !== method) : [...prev, method]
    );
  }

  if (isEdit && routeQuery.isLoading) {
    return <div className="loading-message">Loading route...</div>;
  }

  return (
    <div className="route-form">
      <div className="page-header">
        <h1>{isEdit ? 'Edit Route' : 'New Route'}</h1>
        <p className="page-subtitle">{isEdit ? `Editing route ${id}` : 'Create a new API gateway route'}</p>
      </div>

      <form onSubmit={handleSubmit}>
        <div className="form-section">
          <div className="form-section-title">Basic Info</div>
          <div className="form-row">
            <div className="field">
              <label>Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My API Route" />
            </div>
            <div className="field">
              <label>Status</label>
              <select value={statusVal} onChange={(e) => setStatusVal(Number(e.target.value))}>
                <option value={1}>Active</option>
                <option value={0}>Disabled</option>
              </select>
            </div>
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>URI</label>
              <input value={uri} onChange={(e) => setUri(e.target.value)} placeholder="/api/service/*" required />
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
          <div className="form-section-title">Upstream</div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>Upstream</label>
              <select value={upstreamId} onChange={(e) => setUpstreamId(e.target.value)}>
                <option value="">Select upstream...</option>
                {upstreams.map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.id}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section-title">Authentication</div>
          <label className="method-check">
            <input type="checkbox" checked={requireAuth} onChange={(e) => setRequireAuth(e.target.checked)} />
            Require Authentication (key-auth)
          </label>
        </div>

        <div className="form-section">
          <div className="form-section-title">Service Key (Optional)</div>
          <div className="form-row">
            <div className="field">
              <label>Header Name</label>
              <input value={keyHeader} onChange={(e) => setKeyHeader(e.target.value)} placeholder="Authorization" />
            </div>
            <div className="field">
              <label>Header Value</label>
              <input
                type="password"
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                placeholder={isEdit ? 'Leave empty to keep current' : 'Bearer sk-xxx...'}
              />
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={() => navigate('/gateway/routes')}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving...' : isEdit ? 'Update Route' : 'Create Route'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default GatewayRouteForm;
