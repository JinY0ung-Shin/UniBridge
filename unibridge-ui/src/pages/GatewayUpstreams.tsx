import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getGatewayUpstreams,
  saveGatewayUpstream,
  deleteGatewayUpstream,
  type GatewayUpstream,
} from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import { useResourceMutation } from '../components/useResourceMutation';
import ResourceModal from '../components/ResourceModal';
import DataTablePageHeader from '../components/DataTablePageHeader';
import './GatewayUpstreams.css';

const UPSTREAMS_KEY = ['gateway-upstreams'];

interface NodeEntry {
  host: string;
  port: string;
  weight: string;
}

type UpstreamScheme = 'http' | 'https';
type PassHostMode = 'pass' | 'node' | 'rewrite';

const defaultScheme: UpstreamScheme = 'http';
const defaultPorts: Record<UpstreamScheme, string> = { http: '80', https: '443' };
const defaultPassHost: PassHostMode = 'node';

function defaultPortForScheme(scheme: UpstreamScheme): string {
  return defaultPorts[scheme];
}

function emptyNodeForScheme(scheme: UpstreamScheme): NodeEntry {
  return { host: '', port: defaultPortForScheme(scheme), weight: '1' };
}

function normalizeScheme(value: unknown): UpstreamScheme {
  return value === 'https' ? 'https' : 'http';
}

function normalizePassHost(value: unknown, fallback: PassHostMode): PassHostMode {
  return value === 'pass' || value === 'node' || value === 'rewrite' ? value : fallback;
}

function nodesToEntries(nodes: Record<string, number>, scheme: UpstreamScheme): NodeEntry[] {
  return Object.entries(nodes).map(([addr, weight]) => {
    const [host, port] = addr.split(':');
    return { host, port: port || defaultPortForScheme(scheme), weight: String(weight) };
  });
}

function entriesToNodes(entries: NodeEntry[], scheme: UpstreamScheme): Record<string, number> {
  const nodes: Record<string, number> = {};
  for (const e of entries) {
    if (e.host.trim()) {
      nodes[`${e.host.trim()}:${e.port || defaultPortForScheme(scheme)}`] = Number(e.weight) || 1;
    }
  }
  return nodes;
}

function GatewayUpstreams() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('gateway.upstreams.write');

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [scheme, setScheme] = useState<UpstreamScheme>(defaultScheme);
  const [passHost, setPassHost] = useState<PassHostMode>(defaultPassHost);
  const [upstreamHost, setUpstreamHost] = useState('');
  const [type, setType] = useState('roundrobin');
  const [nodes, setNodes] = useState<NodeEntry[]>([emptyNodeForScheme(defaultScheme)]);
  const [error, setError] = useState('');
  const [upstreamSearch, setUpstreamSearch] = useState('');

  const upstreamsQuery = useQuery({
    queryKey: UPSTREAMS_KEY,
    queryFn: getGatewayUpstreams,
  });

  const saveMutation = useResourceMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      saveGatewayUpstream(id, body),
    invalidateKey: UPSTREAMS_KEY,
    onSuccess: () => closeModal(),
    errorMode: { kind: 'setError', setError, fallback: t('gatewayUpstreams.saveFailed') },
  });

  const deleteMutation = useResourceMutation({
    mutationFn: (id: string) => deleteGatewayUpstream(id),
    invalidateKey: UPSTREAMS_KEY,
    errorMode: { kind: 'toast', title: t('gatewayUpstreams.deleteFailed') },
  });

  const upstreams = upstreamsQuery.data?.items ?? [];
  const normalizedUpstreamSearch = upstreamSearch.trim().toLowerCase();
  const filteredUpstreams = normalizedUpstreamSearch
    ? upstreams.filter((u) => [
        u.name,
        u.id,
        normalizeScheme(u.scheme),
        u.type,
        u.pass_host,
        u.upstream_host,
        formatNodes(u.nodes || {}),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedUpstreamSearch))
    : upstreams;

  function openCreate() {
    setEditingId(null);
    setName('');
    setScheme(defaultScheme);
    setPassHost(defaultPassHost);
    setUpstreamHost('');
    setType('roundrobin');
    setNodes([emptyNodeForScheme(defaultScheme)]);
    setError('');
    setShowModal(true);
  }

  function openEdit(u: GatewayUpstream) {
    const upstreamScheme = normalizeScheme(u.scheme);
    const nodeEntries = nodesToEntries(u.nodes || {}, upstreamScheme);
    setEditingId(u.id);
    setName(u.name || '');
    setScheme(upstreamScheme);
    setPassHost(normalizePassHost(u.pass_host, 'pass'));
    setUpstreamHost(u.upstream_host || '');
    setType(u.type || 'roundrobin');
    setNodes(nodeEntries.length > 0 ? nodeEntries : [emptyNodeForScheme(upstreamScheme)]);
    setError('');
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingId(null);
    setError('');
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const upstreamId = editingId || crypto.randomUUID();
    const body = {
      name: name.trim() || undefined,
      scheme,
      pass_host: passHost,
      upstream_host: passHost === 'rewrite' ? upstreamHost.trim() : undefined,
      type,
      nodes: entriesToNodes(nodes, scheme),
    };
    setError('');
    saveMutation.mutate({ id: upstreamId, body });
  }

  function handleDelete(u: GatewayUpstream) {
    const label = u.name || u.id;
    if (window.confirm(t('gatewayUpstreams.deleteConfirm', { name: label }))) {
      deleteMutation.mutate(u.id);
    }
  }

  function updateNode(index: number, field: keyof NodeEntry, value: string) {
    setNodes((prev) => prev.map((n, i) => (i === index ? { ...n, [field]: value } : n)));
  }

  function handleSchemeChange(value: string) {
    const nextScheme = normalizeScheme(value);
    const currentDefaultPort = defaultPortForScheme(scheme);
    const nextDefaultPort = defaultPortForScheme(nextScheme);
    setScheme(nextScheme);
    setNodes((prev) =>
      prev.map((node) =>
        !node.port || node.port === currentDefaultPort
          ? { ...node, port: nextDefaultPort }
          : node,
      ),
    );
  }

  function addNode() {
    setNodes((prev) => [...prev, emptyNodeForScheme(scheme)]);
  }

  function removeNode(index: number) {
    setNodes((prev) => prev.filter((_, i) => i !== index));
  }

  function formatNodes(nodesObj: Record<string, number>): string {
    return Object.entries(nodesObj)
      .map(([addr, w]) => `${addr} (w:${w})`)
      .join(', ');
  }

  return (
    <div className="gateway-upstreams">
      <DataTablePageHeader
        title={t('gatewayUpstreams.title')}
        subtitle={t('gatewayUpstreams.subtitle')}
        canAdd={canWrite}
        addLabel={t('gatewayUpstreams.addUpstream')}
        onAdd={openCreate}
        extra={upstreams.length > 0 ? (
          <input
            className="upstream-search-input"
            type="search"
            value={upstreamSearch}
            onChange={(event) => setUpstreamSearch(event.target.value)}
            placeholder={t('gatewayUpstreams.searchPlaceholder')}
            aria-label={t('gatewayUpstreams.searchPlaceholder')}
          />
        ) : null}
      />

      {upstreamsQuery.isLoading && <div className="loading-message" role="status">{t('gatewayUpstreams.loadingUpstreams')}</div>}
      {upstreamsQuery.isError && <div className="error-banner" role="alert">{t('gatewayUpstreams.loadFailed')}</div>}

      {upstreams.length > 0 && filteredUpstreams.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('common.name')}</th>
                <th scope="col">{t('gatewayUpstreams.scheme')}</th>
                <th scope="col">{t('common.type')}</th>
                <th scope="col">{t('gatewayUpstreams.nodes')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredUpstreams.map((u) => {
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === u.id;
                return (
                <tr key={u.id}>
                  <td className="cell-alias">
                    {u.name || u.id}
                    {u.system && <span className="badge badge-system">System</span>}
                  </td>
                  <td><span className="badge badge-type">{normalizeScheme(u.scheme).toUpperCase()}</span></td>
                  <td><span className="badge badge-type">{u.type}</span></td>
                  <td className="cell-nodes">{formatNodes(u.nodes || {})}</td>
                  <td>
                    <div className="action-buttons">
                      {canWrite && !u.system && (
                        <>
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            aria-label={t('gatewayUpstreams.editUpstream', { name: u.name || u.id })}
                            onClick={() => openEdit(u)}
                          >
                            {t('common.edit')}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            aria-label={t('gatewayUpstreams.deleteUpstream', { name: u.name || u.id })}
                            onClick={() => handleDelete(u)}
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

      {!upstreamsQuery.isLoading && upstreams.length > 0 && filteredUpstreams.length === 0 && !upstreamsQuery.isError && (
        <div className="empty-state">
          <h3>{t('gatewayUpstreams.noSearchResults')}</h3>
          <p>{t('gatewayUpstreams.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setUpstreamSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!upstreamsQuery.isLoading && upstreams.length === 0 && !upstreamsQuery.isError && (
        <div className="empty-state">
          <h3>{t('gatewayUpstreams.noUpstreams')}</h3>
          <p>{t('gatewayUpstreams.noUpstreamsDesc')}</p>
        </div>
      )}

      {canWrite && showModal && (
        <ResourceModal
          title={editingId ? t('gatewayUpstreams.editTitle') : t('gatewayUpstreams.addTitle')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handleSubmit}>
            <div className="form-grid">
              <div className="form-group">
                <label htmlFor="gateway-upstream-name">{t('common.name')}</label>
                <input
                  id="gateway-upstream-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="my-backend"
                  aria-label={t('common.name')}
                  aria-describedby="gateway-upstream-name-hint"
                />
                <span id="gateway-upstream-name-hint" className="field-hint">
                  {t('gatewayUpstreams.nameHint')}
                </span>
              </div>
              <div className="form-group">
                <label htmlFor="gateway-upstream-scheme">{t('gatewayUpstreams.scheme')}</label>
                <select
                  id="gateway-upstream-scheme"
                  value={scheme}
                  onChange={(e) => handleSchemeChange(e.target.value)}
                  aria-label={t('gatewayUpstreams.scheme')}
                  aria-describedby="gateway-upstream-scheme-hint"
                >
                  <option value="http">HTTP</option>
                  <option value="https">HTTPS</option>
                </select>
                <span id="gateway-upstream-scheme-hint" className="field-hint">
                  {t('gatewayUpstreams.schemeHint')}
                </span>
              </div>
              <div className="form-group">
                <label htmlFor="gateway-upstream-host-header">{t('gatewayUpstreams.hostHeader')}</label>
                <select
                  id="gateway-upstream-host-header"
                  value={passHost}
                  onChange={(e) => setPassHost(normalizePassHost(e.target.value, defaultPassHost))}
                  aria-label={t('gatewayUpstreams.hostHeader')}
                  aria-describedby="gateway-upstream-host-header-hint"
                >
                  <option value="node">{t('gatewayUpstreams.hostHeaderNode')}</option>
                  <option value="pass">{t('gatewayUpstreams.hostHeaderPass')}</option>
                  <option value="rewrite">{t('gatewayUpstreams.hostHeaderRewrite')}</option>
                </select>
                <span id="gateway-upstream-host-header-hint" className="field-hint">
                  {t('gatewayUpstreams.hostHeaderHint')}
                </span>
              </div>
              {passHost === 'rewrite' && (
                <div className="form-group">
                  <label htmlFor="gateway-upstream-rewrite-host">{t('gatewayUpstreams.upstreamHost')}</label>
                  <input
                    id="gateway-upstream-rewrite-host"
                    value={upstreamHost}
                    onChange={(e) => setUpstreamHost(e.target.value)}
                    placeholder="api.example.com"
                    aria-label={t('gatewayUpstreams.upstreamHost')}
                    required
                  />
                </div>
              )}
              <div className="form-group">
                <label htmlFor="gateway-upstream-type">{t('common.type')}</label>
                <select
                  id="gateway-upstream-type"
                  value={type}
                  onChange={(e) => setType(e.target.value)}
                  aria-label={t('common.type')}
                  aria-describedby="gateway-upstream-type-hint"
                >
                  <option value="roundrobin">{t('gatewayUpstreams.typeRoundRobin')}</option>
                  <option value="chash">{t('gatewayUpstreams.typeConsistentHash')}</option>
                  <option value="ewma">{t('gatewayUpstreams.typeEwma')}</option>
                  <option value="least_conn">{t('gatewayUpstreams.typeLeastConnections')}</option>
                </select>
                <span id="gateway-upstream-type-hint" className="field-hint">
                  {t('gatewayUpstreams.typeHint')}
                </span>
              </div>
              <div className="form-group form-group--full">
                <label id="gateway-upstream-nodes-label">{t('gatewayUpstreams.nodesLabel')}</label>
                <span id="gateway-upstream-nodes-hint" className="field-hint">
                  {t('gatewayUpstreams.nodesHint')}
                </span>
                <div
                  className="nodes-list"
                  role="group"
                  aria-labelledby="gateway-upstream-nodes-label"
                  aria-describedby="gateway-upstream-nodes-hint"
                >
                  <div className="node-row node-row--header">
                    <span className="node-label node-host">{t('gatewayUpstreams.hostIp')}</span>
                    <span className="node-label node-port">{t('gatewayUpstreams.port')}</span>
                    <span className="node-label node-weight">{t('gatewayUpstreams.weight')}</span>
                  </div>
                  {nodes.map((node, idx) => (
                    <div key={idx} className="node-row">
                      <input
                        className="node-host"
                        placeholder="e.g. 192.168.1.10 or api.example.com"
                        value={node.host}
                        onChange={(e) => updateNode(idx, 'host', e.target.value)}
                        aria-label={t('gatewayUpstreams.nodeHost', { index: idx + 1 })}
                        aria-describedby="gateway-upstream-nodes-hint"
                        required
                      />
                      <input
                        className="node-port"
                        placeholder="8080"
                        type="number"
                        value={node.port}
                        onChange={(e) => updateNode(idx, 'port', e.target.value)}
                        aria-label={t('gatewayUpstreams.nodePort', { index: idx + 1 })}
                        aria-describedby="gateway-upstream-nodes-hint"
                      />
                      <input
                        className="node-weight"
                        placeholder="1"
                        type="number"
                        value={node.weight}
                        onChange={(e) => updateNode(idx, 'weight', e.target.value)}
                        aria-label={t('gatewayUpstreams.nodeWeight', { index: idx + 1 })}
                        aria-describedby="gateway-upstream-nodes-hint"
                      />
                      {nodes.length > 1 && (
                        <button
                          type="button"
                          className="node-remove"
                          aria-label={t('gatewayUpstreams.removeNode', { index: idx + 1 })}
                          onClick={() => removeNode(idx)}
                        >
                          &times;
                        </button>
                      )}
                    </div>
                  ))}
                  <button
                    type="button"
                    className="btn btn-sm btn-secondary add-node-btn"
                    onClick={addNode}
                    aria-describedby="gateway-upstream-nodes-hint"
                  >
                    {t('gatewayUpstreams.addNode')}
                  </button>
                </div>
              </div>
            </div>

            {error && <div className="form-error" role="alert">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={saveMutation.isPending}
                aria-busy={saveMutation.isPending}
              >
                {saveMutation.isPending ? t('common.saving') : editingId ? t('common.update') : t('common.create')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}

export default GatewayUpstreams;
