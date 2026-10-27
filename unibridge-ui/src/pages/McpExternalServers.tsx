import { useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  approveMcpServerTools,
  createMcpServer,
  deleteMcpServer,
  getMcpServers,
  refreshMcpServer,
  updateMcpServer,
  type McpRemoteServerInfo,
  type McpRemoteToolInfo,
  type McpRemoteToolStatus,
  type McpRemoteToolVersion,
} from '../api/client';
import ResourceModal from '../components/ResourceModal';
import { useToast } from '../components/useToast';
import { formatKST } from '../utils/time';
import './McpSettings.css';

const SERVERS_KEY = ['mcp-servers'];
const SERVER_NAME_PATTERN = /^[a-z][a-z0-9-]{0,15}$/;
const TIMEOUT_RANGE = { min: 5, max: 600 };
const MAX_HEADERS = 20;

const STATUS_BADGE: Record<McpRemoteToolStatus, string> = {
  approved: 'badge badge-ok',
  new: 'badge mcp-badge-new',
  changed: 'badge mcp-badge-warn',
  invalid_name: 'badge badge-error',
};

/** The API's `detail`: a message, or FastAPI's list of validation errors. */
function apiErrorMessage(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('response' in error)) return undefined;
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? String(item.msg) : ''))
      .filter(Boolean);
    return messages.length > 0 ? messages.join('; ') : undefined;
  }
  return undefined;
}

function httpStatus(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'response' in error) {
    return (error as { response?: { status?: number } }).response?.status;
  }
  return undefined;
}

function needsReview(server: McpRemoteServerInfo): boolean {
  return server.tools.some((tool) => tool.status === 'new' || tool.status === 'changed');
}

function useStoreServer() {
  const queryClient = useQueryClient();
  return (server: McpRemoteServerInfo) => {
    queryClient.setQueryData<McpRemoteServerInfo[]>(SERVERS_KEY, (list) =>
      [...(list ?? []).filter((item) => item.name !== server.name), server].sort((a, b) =>
        a.name.localeCompare(b.name),
      ),
    );
    queryClient.invalidateQueries({ queryKey: ['mcp-preview'] });
  };
}

interface HeaderRow {
  id: number;
  name: string;
  /** Typed value; empty keeps the stored one. */
  value: string;
  /** The mask a read returned, for a stored header. */
  masked: string | null;
  storedName: string | null;
}

let nextHeaderId = 0;

function headerRow(name = '', masked: string | null = null): HeaderRow {
  nextHeaderId += 1;
  return { id: nextHeaderId, name, value: '', masked, storedName: masked === null ? null : name };
}

function McpServerModal({
  server,
  onClose,
  onSaved,
}: {
  /** null adds a server. */
  server: McpRemoteServerInfo | null;
  onClose: () => void;
  onSaved: (server: McpRemoteServerInfo) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [name, setName] = useState('');
  const [url, setUrl] = useState(server?.url ?? '');
  const [description, setDescription] = useState(server?.description ?? '');
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [timeout, setTimeoutSeconds] = useState(server?.timeout_seconds ?? 60);
  const [headers, setHeaders] = useState<HeaderRow[]>(
    () => server?.headers.map((header) => headerRow(header.name, header.value)) ?? [],
  );
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState('');

  const nameValid = server !== null || SERVER_NAME_PATTERN.test(name);
  const timeoutValid =
    Number.isInteger(timeout) && timeout >= TIMEOUT_RANGE.min && timeout <= TIMEOUT_RANGE.max;
  const urlChanged = server !== null && url.trim() !== server.url;

  const saveMut = useMutation({
    mutationFn: async () => {
      const body = {
        url: url.trim(),
        description: description.trim(),
        enabled,
        timeout_seconds: timeout,
        headers: headers.map((row) => {
          const keep =
            row.value === '' &&
            row.masked !== null &&
            row.name.trim().toLowerCase() === row.storedName?.toLowerCase();
          return { name: row.name.trim(), value: keep ? (row.masked ?? '') : row.value };
        }),
      };
      if (server) return { server: await updateMcpServer(server.name, body), refreshError: null };
      const created = await createMcpServer({ ...body, name });
      // A new server has no tools yet: fetch them once so they can be reviewed.
      setFetching(true);
      try {
        return { server: await refreshMcpServer(created.name), refreshError: null };
      } catch (refreshError) {
        return { server: created, refreshError };
      }
    },
    onSuccess: ({ server: saved, refreshError }) => {
      onSaved(saved);
      if (server) {
        addToast({ type: 'success', title: t('mcp.ext.saved', { name: saved.name }) });
      } else if (refreshError) {
        // The failure is stored as last_error: read it back.
        queryClient.invalidateQueries({ queryKey: SERVERS_KEY });
        addToast({
          type: 'error',
          title: t('mcp.ext.createdRefreshFailed', { name: saved.name }),
          message: apiErrorMessage(refreshError),
        });
      } else {
        addToast({
          type: 'success',
          title: t('mcp.ext.created', { name: saved.name, count: saved.tools.length }),
        });
      }
      onClose();
    },
    onError: (err) => {
      setFetching(false);
      setError(apiErrorMessage(err) ?? t('common.errorOccurred'));
    },
  });

  function validateHeaders(): string | null {
    const seen = new Set<string>();
    for (const row of headers) {
      const headerName = row.name.trim();
      if (!headerName) return t('mcp.ext.headerNameRequired');
      if (seen.has(headerName.toLowerCase())) return t('mcp.ext.headerDuplicate', { name: headerName });
      seen.add(headerName.toLowerCase());
      const keepsStored = row.masked !== null && headerName.toLowerCase() === row.storedName?.toLowerCase();
      if (row.value === '' && !keepsStored) return t('mcp.ext.headerValueRequired', { name: headerName });
    }
    return null;
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!nameValid || !timeoutValid || saveMut.isPending) return;
    const headerError = validateHeaders();
    if (headerError) {
      setError(headerError);
      return;
    }
    setError('');
    saveMut.mutate();
  }

  function updateHeader(id: number, patch: Partial<HeaderRow>) {
    setHeaders(headers.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }

  const pendingLabel = fetching ? t('mcp.ext.fetchingTools') : t('common.saving');

  return (
    <ResourceModal
      title={server ? t('mcp.ext.editTitle', { name: server.name }) : t('mcp.ext.addTitle')}
      onClose={onClose}
      closeLabel={t('common.close')}
      closeOnOverlayClick={false}
      style={{ width: 640 }}
    >
      <form onSubmit={handleSubmit}>
        <div className="form-grid">
          {server === null && (
            <div className="form-group form-group--full">
              <label htmlFor="mcp-ext-name">{t('mcp.ext.name')}</label>
              <input
                id="mcp-ext-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={16}
                placeholder="e.g. jira"
                aria-invalid={name !== '' && !nameValid}
                aria-describedby="mcp-ext-name-hint"
              />
              <span
                id="mcp-ext-name-hint"
                className={name !== '' && !nameValid ? 'form-hint mcp-invalid' : 'form-hint'}
              >
                {name !== '' && !nameValid ? t('mcp.ext.nameInvalid') : t('mcp.ext.nameHint')}
              </span>
            </div>
          )}
          <div className="form-group form-group--full">
            <label htmlFor="mcp-ext-url">{t('mcp.ext.url')}</label>
            <input
              id="mcp-ext-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              maxLength={2048}
              placeholder="https://mcp.internal:8443/mcp"
              aria-describedby="mcp-ext-url-hint"
            />
            <span id="mcp-ext-url-hint" className="form-hint">{t('mcp.ext.urlHint')}</span>
            {urlChanged && server.tools.length > 0 && (
              <div className="mcp-warn-notice" role="status">{t('mcp.ext.urlChangeWarning')}</div>
            )}
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="mcp-ext-description">{t('mcp.ext.description')}</label>
            <textarea
              id="mcp-ext-description"
              className="mcp-textarea"
              rows={2}
              maxLength={4000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label htmlFor="mcp-ext-timeout">{t('mcp.ext.timeout')}</label>
            <input
              id="mcp-ext-timeout"
              type="number"
              min={TIMEOUT_RANGE.min}
              max={TIMEOUT_RANGE.max}
              value={timeout}
              onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
              aria-invalid={!timeoutValid}
              aria-describedby="mcp-ext-timeout-hint"
            />
            <span id="mcp-ext-timeout-hint" className={timeoutValid ? 'form-hint' : 'form-hint mcp-invalid'}>
              {timeoutValid ? t('mcp.ext.timeoutHint') : t('mcp.invalidRange', TIMEOUT_RANGE)}
            </span>
          </div>
          <div className="form-group">
            <label>&nbsp;</label>
            <label className="mcp-toggle mcp-modal-toggle">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                aria-describedby="mcp-ext-enabled-hint"
              />
              <span>{t('mcp.ext.enabled')}</span>
            </label>
            <span id="mcp-ext-enabled-hint" className="form-hint">{t('mcp.ext.enabledHint')}</span>
          </div>
          <fieldset className="form-group form-group--full mcp-ext-headers">
            <legend>{t('mcp.ext.headers')}</legend>
            <span className="form-hint">{t('mcp.ext.headersHint')}</span>
            {headers.map((row, index) => (
              <div key={row.id} className="mcp-ext-header-row">
                <input
                  type="text"
                  value={row.name}
                  onChange={(e) => updateHeader(row.id, { name: e.target.value })}
                  maxLength={64}
                  placeholder="Authorization"
                  aria-label={t('mcp.ext.headerName', { index: index + 1 })}
                />
                <input
                  type="password"
                  autoComplete="new-password"
                  value={row.value}
                  onChange={(e) => updateHeader(row.id, { value: e.target.value })}
                  maxLength={4096}
                  placeholder={
                    row.masked !== null ? t('mcp.ext.headerValueKeep', { masked: row.masked }) : ''
                  }
                  aria-label={t('mcp.ext.headerValue', { index: index + 1 })}
                />
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => setHeaders(headers.filter((item) => item.id !== row.id))}
                  aria-label={t('mcp.ext.headerRemove', { index: index + 1 })}
                >
                  {t('common.remove')}
                </button>
              </div>
            ))}
            <div>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => setHeaders([...headers, headerRow()])}
                disabled={headers.length >= MAX_HEADERS}
              >
                {t('mcp.ext.headerAdd')}
              </button>
            </div>
          </fieldset>
        </div>

        {error && <div className="form-error" role="alert">{error}</div>}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!nameValid || !timeoutValid || saveMut.isPending}
            aria-busy={saveMut.isPending}
          >
            {saveMut.isPending ? pendingLabel : server ? t('common.save') : t('common.create')}
          </button>
        </div>
      </form>
    </ResourceModal>
  );
}

function McpServerDeleteModal({
  server,
  onClose,
}: {
  server: McpRemoteServerInfo;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [error, setError] = useState('');

  const deleteMut = useMutation({
    mutationFn: () => deleteMcpServer(server.name),
    onSuccess: () => {
      queryClient.setQueryData<McpRemoteServerInfo[]>(SERVERS_KEY, (list) =>
        (list ?? []).filter((item) => item.name !== server.name),
      );
      queryClient.invalidateQueries({ queryKey: ['mcp-preview'] });
      // The grant is gone from every key that held it.
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      onClose();
    },
    onError: (err) => setError(apiErrorMessage(err) ?? t('mcp.ext.deleteFailed', { name: server.name })),
  });

  return (
    <ResourceModal
      title={t('mcp.ext.deleteTitle', { name: server.name })}
      onClose={onClose}
      closeLabel={t('common.close')}
    >
      <div className="mcp-modal-body">
        <p>{t('mcp.ext.deleteConfirm', { route: server.route_id })}</p>
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="modal-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="btn btn-danger"
          onClick={() => deleteMut.mutate()}
          disabled={deleteMut.isPending}
          aria-busy={deleteMut.isPending}
        >
          {deleteMut.isPending ? t('common.deleting') : t('common.delete')}
        </button>
      </div>
    </ResourceModal>
  );
}

function ReadOnlyHint({ value }: { value: boolean | null }) {
  const { t } = useTranslation();
  if (value === null) return <span className="mcp-api-notes--empty">—</span>;
  return <span className="mcp-nowrap">{value ? t('mcp.yes') : t('mcp.no')}</span>;
}

/** JSON with sorted keys, so a reordered schema does not read as changed. */
function canonicalJson(value: unknown, indent?: number): string {
  return JSON.stringify(
    value,
    (_key, item: unknown) =>
      item && typeof item === 'object' && !Array.isArray(item)
        ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
        : item,
    indent,
  );
}

function SchemaBlock({ schema }: { schema: Record<string, unknown> }) {
  return <pre className="mcp-code mcp-schema">{JSON.stringify(schema, null, 2)}</pre>;
}

/** A tool's annotations, or a dash when the server declares none. */
function AnnotationsBlock({ annotations }: { annotations: Record<string, unknown> | null }) {
  if (!annotations || Object.keys(annotations).length === 0) {
    return <span className="mcp-api-notes--empty">—</span>;
  }
  return <SchemaBlock schema={annotations} />;
}

/** One side of a JSON comparison: lines the other side lacks are marked. */
function JsonDiffBlock({
  schema,
  other,
  mark,
}: {
  schema: Record<string, unknown>;
  other: Record<string, unknown>;
  mark: 'add' | 'del';
}) {
  // A trailing comma comes and goes with the next line: compare without it.
  const bare = (line: string) => line.replace(/,$/, '');
  const otherLines = new Set(canonicalJson(other, 2).split('\n').map(bare));
  return (
    <pre className="mcp-code mcp-schema">
      {canonicalJson(schema, 2)
        .split('\n')
        .map((line, index) => (
          <span key={index} className={otherLines.has(bare(line)) ? undefined : `mcp-diff-${mark}`}>
            {line}
            {'\n'}
          </span>
        ))}
    </pre>
  );
}

function CompareField({
  label,
  changed,
  before,
  after,
}: {
  label: string;
  changed: boolean;
  before: ReactNode;
  after: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section
      className={changed ? 'mcp-compare mcp-compare--changed' : 'mcp-compare'}
      aria-label={label}
    >
      <h4 className="mcp-compare-title">
        {label}
        <span className={changed ? 'badge mcp-badge-warn' : 'badge badge-unknown'}>
          {changed ? t('mcp.ext.status.changed') : t('mcp.ext.compareSame')}
        </span>
      </h4>
      {changed ? (
        <div className="mcp-compare-grid">
          <div className="mcp-compare-side">
            <span className="mcp-compare-side-label">{t('mcp.ext.compareBefore')}</span>
            {before}
          </div>
          <div className="mcp-compare-side mcp-compare-side--now">
            <span className="mcp-compare-side-label">{t('mcp.ext.compareNow')}</span>
            {after}
          </div>
        </div>
      ) : (
        after
      )}
    </section>
  );
}

function McpToolComparison({
  tool,
  before,
}: {
  tool: McpRemoteToolInfo;
  before: McpRemoteToolVersion;
}) {
  const { t } = useTranslation();
  const hint = before.annotations?.readOnlyHint;
  const beforeReadOnly = typeof hint === 'boolean' ? hint : null;
  // No annotations and an empty object say the same thing.
  const beforeAnnotations = before.annotations ?? {};
  const nowAnnotations = tool.annotations ?? {};
  const titleChanged = before.title !== tool.title;
  const descriptionChanged = before.description !== tool.description;
  const readOnlyChanged = beforeReadOnly !== tool.read_only;
  const schemaChanged = canonicalJson(before.input_schema) !== canonicalJson(tool.input_schema);
  const annotationsChanged = canonicalJson(beforeAnnotations) !== canonicalJson(nowAnnotations);
  const text = (value: string, empty: string) =>
    value ? (
      <div className="mcp-tool-description">{value}</div>
    ) : (
      <span className="mcp-api-notes--empty">{empty}</span>
    );

  return (
    <>
      <div className="mcp-warn-notice" role="status">{t('mcp.ext.compareNotice')}</div>
      <CompareField
        label={t('mcp.ext.detailsTitle')}
        changed={titleChanged}
        before={text(before.title, '—')}
        after={text(tool.title, '—')}
      />
      <CompareField
        label={t('mcp.colDescription')}
        changed={descriptionChanged}
        before={text(before.description, t('mcp.ext.noDescription'))}
        after={text(tool.description, t('mcp.ext.noDescription'))}
      />
      <CompareField
        label={t('mcp.ext.colReadOnly')}
        changed={readOnlyChanged}
        before={<ReadOnlyHint value={beforeReadOnly} />}
        after={<ReadOnlyHint value={tool.read_only} />}
      />
      <CompareField
        label={t('mcp.ext.detailsSchema')}
        changed={schemaChanged}
        before={<JsonDiffBlock schema={before.input_schema} other={tool.input_schema} mark="del" />}
        after={<JsonDiffBlock schema={tool.input_schema} other={before.input_schema} mark="add" />}
      />
      <CompareField
        label={t('mcp.ext.compareAnnotations')}
        changed={annotationsChanged}
        before={<JsonDiffBlock schema={beforeAnnotations} other={nowAnnotations} mark="del" />}
        after={
          annotationsChanged ? (
            <JsonDiffBlock schema={nowAnnotations} other={beforeAnnotations} mark="add" />
          ) : (
            <AnnotationsBlock annotations={tool.annotations} />
          )
        }
      />
    </>
  );
}

function McpToolDetailsModal({ tool, onClose }: { tool: McpRemoteToolInfo; onClose: () => void }) {
  const { t } = useTranslation();
  const before = tool.status === 'changed' ? tool.approved_version : null;
  return (
    <ResourceModal
      title={before ? t('mcp.ext.compareTitle', { name: tool.exposed_name }) : tool.exposed_name}
      onClose={onClose}
      closeLabel={t('common.close')}
      style={{ width: before ? 960 : 720 }}
    >
      <div className="mcp-modal-body mcp-tool-details">
        <div className="mcp-warn-notice">{t('mcp.ext.detailsUntrusted')}</div>
        {before ? (
          <>
            <dl>
              <dt>{t('mcp.ext.detailsName')}</dt>
              <dd><code>{tool.name}</code></dd>
            </dl>
            <McpToolComparison tool={tool} before={before} />
          </>
        ) : (
          <>
            <dl>
              <dt>{t('mcp.ext.detailsName')}</dt>
              <dd><code>{tool.name}</code></dd>
              {tool.title && (
                <>
                  <dt>{t('mcp.ext.detailsTitle')}</dt>
                  <dd>{tool.title}</dd>
                </>
              )}
              <dt>{t('mcp.ext.colReadOnly')}</dt>
              <dd><ReadOnlyHint value={tool.read_only} /></dd>
              <dt>{t('mcp.colDescription')}</dt>
              <dd className="mcp-tool-description">{tool.description || t('mcp.ext.noDescription')}</dd>
            </dl>
            <h4 className="mcp-tools-title">{t('mcp.ext.detailsSchema')}</h4>
            <SchemaBlock schema={tool.input_schema} />
            <h4 className="mcp-tools-title mcp-details-annotations">{t('mcp.ext.compareAnnotations')}</h4>
            <AnnotationsBlock annotations={tool.annotations} />
          </>
        )}
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
    </ResourceModal>
  );
}

function McpServerTools({
  server,
  canWrite,
  onStored,
  onStale,
}: {
  server: McpRemoteServerInfo;
  canWrite: boolean;
  onStored: (server: McpRemoteServerInfo) => void;
  /** 409: a refresh changed some checked tools since they were loaded. */
  onStale: (detail: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [filter, setFilter] = useState('');
  const [details, setDetails] = useState<McpRemoteToolInfo | null>(null);
  // Changed tools start unchecked: approving them again is a deliberate act.
  const [selected, setSelected] = useState(
    () => new Set(server.tools.filter((tool) => tool.status === 'approved').map((tool) => tool.name)),
  );

  const approved = server.tools.filter((tool) => tool.status === 'approved').map((tool) => tool.name);
  const dirty = approved.length !== selected.size || approved.some((toolName) => !selected.has(toolName));
  const selectable = server.tools.filter((tool) => tool.status !== 'invalid_name');
  const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = server.tools.filter((tool) => {
    const haystack = `${tool.exposed_name}\n${tool.title}\n${tool.description}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
  const hasChanged = server.tools.some((tool) => tool.status === 'changed');

  const approveMut = useMutation({
    // Each digest is the one rendered here: the table remounts when a digest changes.
    mutationFn: () =>
      approveMcpServerTools(
        server.name,
        server.tools
          .filter((tool) => selected.has(tool.name))
          .map((tool) => ({ name: tool.name, digest: tool.digest }))
          .sort((a, b) => a.name.localeCompare(b.name)),
      ),
    onSuccess: (updated) => {
      addToast({ type: 'success', title: t('mcp.ext.approvalsSaved', { name: server.name }) });
      onStored(updated);
    },
    onError: (err) => {
      if (httpStatus(err) !== 409) return;
      // A refresh changed some of them since they were loaded; nothing was
      // approved. Reload the listing (this table starts over, so the panel
      // keeps the message) for a new review.
      onStale(apiErrorMessage(err) ?? '');
      queryClient.invalidateQueries({ queryKey: SERVERS_KEY });
    },
  });

  function setChecked(names: string[], on: boolean) {
    const next = new Set(selected);
    for (const toolName of names) {
      if (on) next.add(toolName);
      else next.delete(toolName);
    }
    setSelected(next);
  }

  const shownSelectable = shown.filter((tool) => tool.status !== 'invalid_name').map((tool) => tool.name);

  return (
    <div className="mcp-ext-tools">
      {hasChanged && <p className="form-hint">{t('mcp.ext.changedHint')}</p>}
      <div className="mcp-template-toolbar">
        <input
          type="search"
          className="mcp-template-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t('mcp.ext.toolsFilter')}
          aria-label={t('mcp.ext.toolsFilterFor', { name: server.name })}
        />
        <span className="mcp-template-count">
          {t('mcp.ext.selectedCount', { selected: selected.size, total: selectable.length })}
        </span>
        {canWrite && (
          <div className="mcp-template-bulk">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setChecked(shownSelectable, true)}
              disabled={shownSelectable.length === 0}
            >
              {t('mcp.ext.selectAll')}
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setChecked(shownSelectable, false)}
              disabled={shownSelectable.length === 0}
            >
              {t('mcp.ext.selectNone')}
            </button>
          </div>
        )}
      </div>
      <div className="table-container mcp-api-table-wrap mcp-ext-tools-wrap">
        <table
          className="data-table mcp-api-table mcp-ext-tools-table"
          aria-label={t('mcp.ext.toolsTable', { name: server.name })}
        >
          <thead>
            <tr>
              <th className="mcp-api-col-check"><span className="mcp-sr-only">{t('mcp.ext.colApprove')}</span></th>
              <th>{t('mcp.colTool')}</th>
              <th>{t('common.status')}</th>
              <th>{t('mcp.ext.colReadOnly')}</th>
              <th>{t('mcp.colDescription')}</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((tool) => {
              const invalid = tool.status === 'invalid_name';
              const checked = selected.has(tool.name);
              const rowClass = [
                checked ? 'mcp-api-row--on' : '',
                tool.status === 'changed' || tool.status === 'new' ? `mcp-tool-row--${tool.status}` : '',
              ].filter(Boolean).join(' ');
              return (
                <tr key={tool.name} className={rowClass || undefined}>
                  <td className="mcp-api-col-check">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => setChecked([tool.name], e.target.checked)}
                      disabled={!canWrite || invalid}
                      aria-label={tool.exposed_name}
                    />
                  </td>
                  <td className="mcp-api-col-name">
                    <code className="mcp-ellipsis mcp-ext-tool-name" title={tool.exposed_name}>
                      {tool.exposed_name}
                    </code>
                    {tool.title && (
                      <div className="mcp-ellipsis mcp-tool-title" title={tool.title}>{tool.title}</div>
                    )}
                  </td>
                  <td>
                    <span
                      className={STATUS_BADGE[tool.status]}
                      title={invalid ? t('mcp.ext.invalidNameHint') : undefined}
                    >
                      {t(`mcp.ext.status.${tool.status}`)}
                    </span>
                  </td>
                  <td><ReadOnlyHint value={tool.read_only} /></td>
                  <td className="mcp-api-col-notes">
                    <div className="mcp-api-notes">
                      <span
                        className={tool.description ? 'mcp-ellipsis' : 'mcp-ellipsis mcp-api-notes--empty'}
                        title={tool.description || undefined}
                      >
                        {tool.description || t('mcp.ext.noDescription')}
                      </span>
                      {tool.status === 'changed' && tool.approved_version ? (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm mcp-compare-button"
                          onClick={() => setDetails(tool)}
                          aria-label={t('mcp.ext.compareFor', { name: tool.exposed_name })}
                        >
                          {t('mcp.ext.compare')}
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => setDetails(tool)}
                          aria-label={t('mcp.ext.detailsFor', { name: tool.exposed_name })}
                        >
                          {t('mcp.ext.details')}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5} className="form-hint">{t('mcp.ext.toolsNoMatch')}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {canWrite && (
        <div className="mcp-ext-approve-bar">
          {approveMut.isError && httpStatus(approveMut.error) !== 409 && (
            <span className="save-error" role="alert">
              {apiErrorMessage(approveMut.error) ?? t('mcp.ext.approvalsFailed')}
            </span>
          )}
          <span className={dirty ? 'settings-save-status settings-save-status--changed' : 'settings-save-status'}>
            {dirty ? t('mcp.ext.approvalsUnsaved') : t('mcp.ext.approvalsClean')}
          </span>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setSelected(new Set(approved))}
            disabled={!dirty || approveMut.isPending}
          >
            {t('mcp.discardChanges')}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => approveMut.mutate()}
            disabled={!dirty || approveMut.isPending}
            aria-busy={approveMut.isPending}
          >
            {approveMut.isPending ? t('common.saving') : t('mcp.ext.saveApprovals')}
          </button>
        </div>
      )}
      {details && <McpToolDetailsModal tool={details} onClose={() => setDetails(null)} />}
    </div>
  );
}

function McpServerPanel({
  server,
  canWrite,
  expanded,
  onToggle,
  onEdit,
  onDelete,
}: {
  server: McpRemoteServerInfo;
  canWrite: boolean;
  expanded: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const storeServer = useStoreServer();
  // Outlives the tool table, which remounts on the reload a 409 triggers.
  const [staleDetail, setStaleDetail] = useState<string | null>(null);
  const bodyId = `mcp-ext-body-${server.name}`;

  function stored(updated: McpRemoteServerInfo) {
    setStaleDetail(null);
    storeServer(updated);
  }

  const refreshMut = useMutation({
    mutationFn: () => refreshMcpServer(server.name),
    onSuccess: (updated) => {
      stored(updated);
      addToast({
        type: 'success',
        title: t('mcp.ext.refreshed', { name: server.name, count: updated.tools.length }),
      });
    },
    onError: (err) => {
      // The failure is stored as last_error: read it back.
      queryClient.invalidateQueries({ queryKey: SERVERS_KEY });
      addToast({
        type: 'error',
        title: t('mcp.ext.refreshFailed', { name: server.name }),
        message: apiErrorMessage(err),
      });
    },
  });

  const approvedCount = server.tools.filter((tool) => tool.status === 'approved').length;
  // Remount the table when the listing or approvals change, so the checkboxes
  // start again from what is stored.
  const toolsKey = server.tools.map((tool) => `${tool.name}:${tool.status}:${tool.digest}`).join('\n');

  return (
    <section className="mcp-ext-server" aria-label={server.name}>
      <div className="mcp-ext-server-head">
        <button
          type="button"
          className="mcp-ext-toggle"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={onToggle}
        >
          <span className="mcp-ext-toggle-icon" aria-hidden="true">▸</span>
          <span className="mcp-ext-server-name">{server.name}</span>
        </button>
        <div className="mcp-ext-server-badges">
          <span className={server.enabled ? 'badge badge-ok' : 'badge badge-unknown'}>
            {server.enabled ? t('mcp.ext.on') : t('mcp.ext.off')}
          </span>
          {needsReview(server) && <span className="badge mcp-badge-warn">{t('mcp.ext.needsReview')}</span>}
          {server.last_error && <span className="badge badge-error">{t('mcp.ext.errorBadge')}</span>}
        </div>
        <code className="mcp-ellipsis mcp-ext-url" title={server.url}>{server.url}</code>
        <span
          className="mcp-ext-count"
          title={t('mcp.ext.toolCount', { approved: approvedCount, total: server.tools.length })}
        >
          {t('mcp.ext.toolCountShort', { approved: approvedCount, total: server.tools.length })}
        </span>
        {canWrite && (
          <div className="mcp-ext-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => refreshMut.mutate()}
              disabled={refreshMut.isPending}
              aria-busy={refreshMut.isPending}
              aria-label={t('mcp.ext.refreshServer', { name: server.name })}
            >
              {refreshMut.isPending ? t('mcp.ext.fetchingTools') : t('mcp.ext.refresh')}
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={onEdit}
              aria-label={t('mcp.ext.editServer', { name: server.name })}
            >
              {t('common.edit')}
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={onDelete}
              aria-label={t('mcp.ext.deleteServer', { name: server.name })}
            >
              {t('common.delete')}
            </button>
          </div>
        )}
      </div>
      {/* Hidden rather than unmounted: unsaved checkbox changes survive a collapse. */}
      <div id={bodyId} className="mcp-ext-server-body" hidden={!expanded}>
        {/* The row drops the URL on narrow screens; it shows here instead. */}
        <code className="mcp-ext-body-url">{server.url}</code>
        {server.description && <p className="mcp-ext-description">{server.description}</p>}
        <div className="mcp-ext-meta">
          <span>{t('mcp.ext.toolCount', { approved: approvedCount, total: server.tools.length })}</span>
          <span>
            {server.refreshed_at
              ? t('mcp.ext.refreshedAt', { time: formatKST(server.refreshed_at) })
              : t('mcp.ext.neverRefreshed')}
          </span>
          <span>
            {t('mcp.ext.grant')} <code>{server.route_id}</code>
          </span>
        </div>
        {server.last_error && (
          <div className="error-banner mcp-ext-error" role="alert">
            {t('mcp.ext.lastError', { error: server.last_error })}
          </div>
        )}
        {server.tools.length === 0 ? (
          <p className="form-hint">
            {server.refreshed_at ? t('mcp.ext.noToolsListed') : t('mcp.ext.noTools')}
          </p>
        ) : (
          <>
            {staleDetail !== null && (
              <div className="mcp-warn-notice mcp-ext-stale" role="alert">
                <strong>{t('mcp.ext.approvalsStale')}</strong>
                {staleDetail && <div>{staleDetail}</div>}
              </div>
            )}
            <McpServerTools
              key={toolsKey}
              server={server}
              canWrite={canWrite}
              onStored={stored}
              onStale={setStaleDetail}
            />
          </>
        )}
      </div>
    </section>
  );
}

// Past this many servers the list gets a search box and a review filter.
const SERVER_FILTER_FROM = 4;

function serverMatches(server: McpRemoteServerInfo, terms: string[]): boolean {
  const haystack = `${server.name} ${server.url} ${server.description}`.toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

function McpExternalServers({ canWrite, groupEnabled }: { canWrite: boolean; groupEnabled: boolean }) {
  const { t } = useTranslation();
  const storeServer = useStoreServer();
  // undefined: no modal; null: adding a server.
  const [editing, setEditing] = useState<McpRemoteServerInfo | null | undefined>(undefined);
  const [deleting, setDeleting] = useState<McpRemoteServerInfo | null>(null);
  // One server open at a time keeps the card short however many there are.
  // undefined: not chosen yet, so a lone server opens by itself.
  const [openName, setOpenName] = useState<string | null | undefined>(undefined);
  const [search, setSearch] = useState('');
  const [reviewOnly, setReviewOnly] = useState(false);
  const serversQuery = useQuery({ queryKey: SERVERS_KEY, queryFn: getMcpServers });
  const servers = serversQuery.data ?? [];
  const reviewCount = servers.filter(needsReview).length;
  const filtering = servers.length >= SERVER_FILTER_FROM;
  const terms = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = filtering
    ? servers.filter((server) => (!reviewOnly || needsReview(server)) && serverMatches(server, terms))
    : servers;
  const openServer = openName === undefined ? (servers.length === 1 ? servers[0].name : null) : openName;

  function saved(server: McpRemoteServerInfo) {
    storeServer(server);
    setOpenName(server.name);
  }

  return (
    <div className="settings-card">
      <div className="mcp-card-head">
        <h3>{t('mcp.ext.section')}</h3>
        {canWrite && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setEditing(null)}>
            {t('mcp.ext.add')}
          </button>
        )}
      </div>
      <p className="form-hint mcp-section-hint">{t('mcp.ext.hint')}</p>
      <ul className="form-hint mcp-ext-hints">
        <li>{t('mcp.ext.grantHint')}</li>
        <li>{t('mcp.ext.untrustedHint')}</li>
      </ul>
      {!groupEnabled && (
        <div className="settings-readonly-notice mcp-ext-group-off" role="status">{t('mcp.ext.groupOff')}</div>
      )}
      {serversQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {serversQuery.isError && <div className="error-banner" role="alert">{t('mcp.ext.loadFailed')}</div>}
      {serversQuery.data && servers.length === 0 && <p className="form-hint">{t('mcp.ext.empty')}</p>}
      {filtering && (
        <div className="mcp-ext-filter">
          <input
            type="search"
            className="mcp-template-filter mcp-ext-search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('mcp.ext.searchServers')}
            aria-label={t('mcp.ext.searchServers')}
          />
          <label className="mcp-ext-review-only">
            <input
              type="checkbox"
              checked={reviewOnly}
              onChange={(event) => setReviewOnly(event.target.checked)}
            />
            {t('mcp.ext.reviewOnly', { count: reviewCount })}
          </label>
          <span className="form-hint">{t('mcp.ext.serversShown', { shown: shown.length, total: servers.length })}</span>
        </div>
      )}
      {servers.length > 0 && (
        <div className="mcp-ext-list">
          {shown.map((server) => (
            <McpServerPanel
              key={server.name}
              server={server}
              canWrite={canWrite}
              expanded={openServer === server.name}
              onToggle={() => setOpenName(openServer === server.name ? null : server.name)}
              onEdit={() => setEditing(server)}
              onDelete={() => setDeleting(server)}
            />
          ))}
          {shown.length === 0 && <p className="form-hint">{t('mcp.ext.noServerMatches')}</p>}
        </div>
      )}
      {editing !== undefined && (
        <McpServerModal server={editing} onClose={() => setEditing(undefined)} onSaved={saved} />
      )}
      {deleting && <McpServerDeleteModal server={deleting} onClose={() => setDeleting(null)} />}
    </div>
  );
}

export default McpExternalServers;
