import { Fragment, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getGatewayUpstreams,
  saveGatewayUpstream,
  deleteGatewayUpstream,
  type GatewayUpstream,
  type GatewayUpstreamNode,
} from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import { useResourceMutation } from '../components/useResourceMutation';
import ResourceModal from '../components/ResourceModal';
import DataTablePageHeader from '../components/DataTablePageHeader';
import './GatewayUpstreams.css';

const UPSTREAMS_KEY = ['gateway-upstreams'];

// The gateway's node path prefix plugin reads a node's prefix from this metadata key.
const PATH_PREFIX_KEY = 'path_prefix';
// Same rule the backend enforces: one or more "/segment" parts of URL path
// characters or %XX escapes, plus no "." or ".." segment (escaped or not) and
// at most 256 characters.
const PATH_PREFIX_PATTERN = /^(?:\/(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})+)+$/;
const PATH_PREFIX_MAX_LENGTH = 256;
const PATH_PREFIX_HINT_ID = 'gateway-upstream-path-prefix-hint';

interface NodeEntry {
  host: string;
  port: string;
  weight: string;
  pathPrefix: string;
  // Carried over from a loaded list-form node. The form does not edit these, so a
  // save sends them back unchanged.
  priority?: number;
  extraMetadata?: Record<string, unknown>;
}

type EditableNodeField = 'host' | 'port' | 'weight' | 'pathPrefix';

type UpstreamScheme = 'http' | 'https';
type PassHostMode = 'pass' | 'node' | 'rewrite';

const defaultScheme: UpstreamScheme = 'http';
const defaultPorts: Record<UpstreamScheme, string> = { http: '80', https: '443' };
const defaultPassHost: PassHostMode = 'node';

function defaultPortForScheme(scheme: UpstreamScheme): string {
  return defaultPorts[scheme];
}

function emptyNodeForScheme(scheme: UpstreamScheme): NodeEntry {
  return { host: '', port: defaultPortForScheme(scheme), weight: '1', pathPrefix: '' };
}

function normalizeScheme(value: unknown): UpstreamScheme {
  return value === 'https' ? 'https' : 'http';
}

function normalizePassHost(value: unknown, fallback: PassHostMode): PassHostMode {
  return value === 'pass' || value === 'node' || value === 'rewrite' ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// Dict-form addresses are host:port, with an IPv6 host in brackets ("[::1]:8080").
// A bare IPv6 literal has more than one colon and no port to split off.
function splitAddress(addr: string): { host: string; port: string } {
  if (addr.startsWith('[')) {
    const close = addr.indexOf(']');
    if (close === -1) return { host: addr, port: '' };
    const rest = addr.slice(close + 1);
    return { host: addr.slice(0, close + 1), port: rest.startsWith(':') ? rest.slice(1) : '' };
  }
  const colon = addr.lastIndexOf(':');
  if (colon === -1 || addr.indexOf(':') !== colon) return { host: addr, port: '' };
  return { host: addr.slice(0, colon), port: addr.slice(colon + 1) };
}

// APISIX only takes an IPv6 host in brackets, in both node forms.
function bracketHost(host: string): string {
  return host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
}

function formatAddress(host: string, port: string | number | undefined): string {
  const bracketed = bracketHost(host);
  return port === undefined || port === '' ? bracketed : `${bracketed}:${port}`;
}

// Weight 0 is valid (APISIX sends that node no traffic). Only a blank or
// unparsable value falls back to the default of 1.
function parseWeight(value: string): number {
  const weight = Number(value);
  return value.trim() === '' || Number.isNaN(weight) ? 1 : weight;
}

function normalizePathPrefix(value: string): string {
  return value.trim().replace(/\/+$/, '');
}

function isValidPathPrefix(prefix: string): boolean {
  if (prefix === '') return true;
  return (
    prefix.length <= PATH_PREFIX_MAX_LENGTH
    && PATH_PREFIX_PATTERN.test(prefix)
    && !prefix.split('/').some((segment) => {
      // "%2e" counts as a dot: a backend that decodes it would see "." or "..".
      const decoded = segment.replace(/%2e/gi, '.');
      return decoded === '.' || decoded === '..';
    })
  );
}

function nodePathPrefix(node: GatewayUpstreamNode): string {
  const prefix = isRecord(node.metadata) ? node.metadata[PATH_PREFIX_KEY] : undefined;
  if (prefix === undefined || prefix === null) return '';
  // Any other type is shown as JSON, which never passes validation, so the admin
  // sees it flagged instead of a save quietly turning it into something else.
  return typeof prefix === 'string' ? prefix : JSON.stringify(prefix);
}

function nodesToEntries(nodes: GatewayUpstream['nodes'], scheme: UpstreamScheme): NodeEntry[] {
  if (Array.isArray(nodes)) {
    return nodes.map((node) => {
      const extraMetadata = isRecord(node.metadata) ? { ...node.metadata } : {};
      delete extraMetadata[PATH_PREFIX_KEY];
      return {
        host: String(node.host ?? ''),
        port: node.port == null ? defaultPortForScheme(scheme) : String(node.port),
        weight: String(node.weight ?? 1),
        pathPrefix: nodePathPrefix(node),
        priority: typeof node.priority === 'number' ? node.priority : undefined,
        extraMetadata: Object.keys(extraMetadata).length > 0 ? extraMetadata : undefined,
      };
    });
  }
  return Object.entries(nodes).map(([addr, weight]) => {
    const { host, port } = splitAddress(addr);
    return { host, port: port || defaultPortForScheme(scheme), weight: String(weight), pathPrefix: '' };
  });
}

// Only the list form can carry a prefix, a priority or other metadata. Without
// any of them the dict form is written, as before path prefixes existed.
function needsListForm(entry: NodeEntry): boolean {
  return (
    normalizePathPrefix(entry.pathPrefix) !== ''
    || entry.priority !== undefined
    || entry.extraMetadata !== undefined
  );
}

function entriesToNodes(entries: NodeEntry[], scheme: UpstreamScheme): GatewayUpstream['nodes'] {
  const filled = entries.filter((e) => e.host.trim());
  if (!filled.some(needsListForm)) {
    const nodes: Record<string, number> = {};
    for (const e of filled) {
      nodes[formatAddress(e.host.trim(), e.port || defaultPortForScheme(scheme))] = parseWeight(e.weight);
    }
    return nodes;
  }
  return filled.map((e) => {
    const node: GatewayUpstreamNode = {
      host: bracketHost(e.host.trim()),
      port: Number(e.port || defaultPortForScheme(scheme)),
      weight: parseWeight(e.weight),
    };
    if (e.priority !== undefined) node.priority = e.priority;
    const prefix = normalizePathPrefix(e.pathPrefix);
    const metadata = prefix ? { ...e.extraMetadata, [PATH_PREFIX_KEY]: prefix } : e.extraMetadata;
    if (metadata) node.metadata = metadata;
    return node;
  });
}

function formatNodes(nodes: GatewayUpstream['nodes']): string {
  if (Array.isArray(nodes)) {
    return nodes
      .map((node) => `${formatAddress(String(node.host ?? ''), node.port)}${nodePathPrefix(node)} (w:${node.weight})`)
      .join(', ');
  }
  return Object.entries(nodes)
    .map(([addr, w]) => `${addr} (w:${w})`)
    .join(', ');
}

function pathPrefixInputId(index: number): string {
  return `gateway-upstream-node-prefix-${index}`;
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
    // Each invalid prefix already shows its error under the row; move focus there.
    const invalidPrefixIndex = nodes.findIndex(
      (node) => !isValidPathPrefix(normalizePathPrefix(node.pathPrefix)),
    );
    if (invalidPrefixIndex !== -1) {
      document.getElementById(pathPrefixInputId(invalidPrefixIndex))?.focus();
      return;
    }
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

  function updateNode(index: number, field: EditableNodeField, value: string) {
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
          className="modal--upstream"
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
                <span id={PATH_PREFIX_HINT_ID} className="field-hint">
                  {t('gatewayUpstreams.pathPrefixHint')}
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
                    <span className="node-label node-prefix">{t('gatewayUpstreams.pathPrefix')}</span>
                    <span className="node-label node-weight">{t('gatewayUpstreams.weight')}</span>
                    {nodes.length > 1 && <span className="node-remove-spacer" aria-hidden="true" />}
                  </div>
                  {nodes.map((node, idx) => {
                    const prefixInvalid = !isValidPathPrefix(normalizePathPrefix(node.pathPrefix));
                    const prefixErrorId = `gateway-upstream-node-prefix-error-${idx}`;
                    return (
                      <Fragment key={idx}>
                        <div className="node-row">
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
                            id={pathPrefixInputId(idx)}
                            className="node-prefix"
                            placeholder={t('gatewayUpstreams.pathPrefixPlaceholder')}
                            value={node.pathPrefix}
                            onChange={(e) => updateNode(idx, 'pathPrefix', e.target.value)}
                            aria-label={t('gatewayUpstreams.nodePathPrefix', { index: idx + 1 })}
                            aria-describedby={
                              prefixInvalid ? `${prefixErrorId} ${PATH_PREFIX_HINT_ID}` : PATH_PREFIX_HINT_ID
                            }
                            aria-invalid={prefixInvalid ? 'true' : undefined}
                            autoCapitalize="off"
                            autoCorrect="off"
                            spellCheck={false}
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
                        {prefixInvalid && (
                          <div id={prefixErrorId} className="node-error" role="alert">
                            {t('gatewayUpstreams.pathPrefixInvalid', { index: idx + 1 })}
                          </div>
                        )}
                      </Fragment>
                    );
                  })}
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
