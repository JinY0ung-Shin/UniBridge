import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getMcpPreview,
  getMcpRoutes,
  getMcpSettings,
  getMcpTemplates,
  getScopedApiKeys,
  updateMcpSettings,
  type McpExposedRoute,
  type McpSettings,
  type McpTemplateInfo,
  type McpToolGroup,
} from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import { useToast } from '../components/useToast';
import './QuerySettings.css';
import './McpSettings.css';

const MCP_TOOL_GROUPS: McpToolGroup[] = ['templates', 'db_explore', 'run_query', 's3', 'nas', 'apis'];

const MAX_ROWS_RANGE = { min: 1, max: 10000 };
const MAX_RESULT_BYTES_RANGE = { min: 1024, max: 1048576 };

function inRange(value: number, range: { min: number; max: number }): boolean {
  return Number.isInteger(value) && value >= range.min && value <= range.max;
}

function settingsKey(settings: McpSettings): string {
  return [
    settings.enabled,
    ...MCP_TOOL_GROUPS.map((group) => settings.tool_groups[group]),
    [...settings.excluded_templates].sort().join('\n'),
    JSON.stringify(
      [...settings.exposed_routes].sort((a, b) => a.route_id.localeCompare(b.route_id)),
    ),
    settings.max_rows,
    settings.max_result_bytes,
  ].join(':');
}

function httpStatus(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'response' in error) {
    return (error as { response?: { status?: number } }).response?.status;
  }
  return undefined;
}

function templateMatches(template: McpTemplateInfo, terms: string[]): boolean {
  const haystack = `${template.path}\n${template.name}\n${template.database}`.toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

function McpTemplateExposure({
  excluded,
  onChange,
  canWrite,
  groupEnabled,
}: {
  excluded: string[];
  onChange: (next: string[]) => void;
  canWrite: boolean;
  groupEnabled: boolean;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState('');
  const templatesQuery = useQuery({ queryKey: ['mcp-templates'], queryFn: getMcpTemplates });

  const templates = templatesQuery.data ?? [];
  const excludedSet = new Set(excluded);
  const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = templates.filter((template) => templateMatches(template, terms));
  const exposedCount = templates.filter((tpl) => tpl.enabled && !excludedSet.has(tpl.path)).length;
  const enabledCount = templates.filter((tpl) => tpl.enabled).length;

  function setExposed(paths: string[], exposed: boolean) {
    const next = new Set(excluded);
    for (const path of paths) {
      if (exposed) next.delete(path);
      else next.add(path);
    }
    onChange([...next].sort());
  }

  return (
    <div className="settings-card">
      <h3>{t('mcp.templatesSection')}</h3>
      <p className="form-hint mcp-section-hint">{t('mcp.templatesHint')}</p>
      {!groupEnabled && (
        <div className="settings-readonly-notice" role="status">{t('mcp.templatesGroupOff')}</div>
      )}
      {templatesQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {templatesQuery.isError && (
        <div className="error-banner" role="alert">{t('mcp.templatesLoadFailed')}</div>
      )}
      {templatesQuery.data && templates.length === 0 && (
        <p className="form-hint">{t('mcp.templatesEmpty')}</p>
      )}
      {templates.length > 0 && (
        <>
          <div className="mcp-template-toolbar">
            <input
              type="search"
              className="mcp-template-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={t('mcp.templatesFilter')}
              aria-label={t('mcp.templatesFilter')}
            />
            <span className="mcp-template-count">
              {t('mcp.templatesExposedCount', { exposed: exposedCount, total: enabledCount })}
            </span>
            {canWrite && (
              <div className="mcp-template-bulk">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => setExposed(shown.map((tpl) => tpl.path), true)}
                  disabled={shown.length === 0}
                >
                  {t('mcp.templatesExposeAll')}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => setExposed(shown.map((tpl) => tpl.path), false)}
                  disabled={shown.length === 0}
                >
                  {t('mcp.templatesHideAll')}
                </button>
              </div>
            )}
          </div>
          <ul className="mcp-template-list" aria-label={t('mcp.templatesSection')}>
            {shown.map((template) => (
              <li key={template.path} className="mcp-template-item">
                <label className="mcp-toggle">
                  <input
                    type="checkbox"
                    checked={!excludedSet.has(template.path)}
                    onChange={(e) => setExposed([template.path], e.target.checked)}
                    disabled={!canWrite}
                    aria-label={template.path}
                  />
                  <code>{template.path}</code>
                  <span className="mcp-template-meta">
                    {template.name} · {template.database}
                  </span>
                  {!template.enabled && (
                    <span className="badge badge-error">{t('mcp.templateDisabled')}</span>
                  )}
                </label>
              </li>
            ))}
            {shown.length === 0 && <li className="form-hint">{t('mcp.templatesNoMatch')}</li>}
          </ul>
        </>
      )}
    </div>
  );
}

function McpApiExposure({
  exposed,
  onChange,
  canWrite,
  groupEnabled,
}: {
  exposed: McpExposedRoute[];
  onChange: (next: McpExposedRoute[]) => void;
  canWrite: boolean;
  groupEnabled: boolean;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState('');
  const routesQuery = useQuery({ queryKey: ['mcp-routes'], queryFn: getMcpRoutes, retry: false });

  const routes = routesQuery.data ?? [];
  const exposedById = new Map(exposed.map((route) => [route.route_id, route]));
  const known = new Set(routes.map((route) => route.route_id));
  const missing = routesQuery.data ? exposed.filter((route) => !known.has(route.route_id)) : [];
  const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = routes.filter((route) => {
    const haystack = `${route.route_id}\n${route.name}\n${route.paths.join(' ')}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });

  function setExposed(routeId: string, on: boolean) {
    const rest = exposed.filter((route) => route.route_id !== routeId);
    onChange(on ? [...rest, { route_id: routeId, description: '' }] : rest);
  }

  function setDescription(routeId: string, description: string) {
    onChange(exposed.map((route) => (route.route_id === routeId ? { ...route, description } : route)));
  }

  return (
    <div className="settings-card">
      <h3>{t('mcp.apisSection')}</h3>
      <p className="form-hint mcp-section-hint">{t('mcp.apisHint')}</p>
      {!groupEnabled && (
        <div className="settings-readonly-notice" role="status">{t('mcp.apisGroupOff')}</div>
      )}
      {routesQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {routesQuery.isError && <div className="error-banner" role="alert">{t('mcp.apisLoadFailed')}</div>}
      {routesQuery.data && routes.length === 0 && <p className="form-hint">{t('mcp.apisEmpty')}</p>}
      {missing.length > 0 && (
        <div className="settings-readonly-notice mcp-api-missing" role="status">
          <span>{t('mcp.apisMissing', { count: missing.length })}</span>
          {canWrite && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onChange(exposed.filter((route) => known.has(route.route_id)))}
            >
              {t('mcp.apisRemoveMissing')}
            </button>
          )}
        </div>
      )}
      {routes.length > 0 && (
        <>
          <div className="mcp-template-toolbar">
            <input
              type="search"
              className="mcp-template-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={t('mcp.apisFilter')}
              aria-label={t('mcp.apisFilter')}
            />
            <span className="mcp-template-count">
              {t('mcp.apisExposedCount', {
                exposed: routes.filter((route) => exposedById.has(route.route_id)).length,
                total: routes.length,
              })}
            </span>
          </div>
          <ul className="mcp-template-list mcp-api-list" aria-label={t('mcp.apisSection')}>
            {shown.map((route) => {
              const entry = exposedById.get(route.route_id);
              const descriptionId = `mcp-api-description-${route.route_id}`;
              return (
                <li key={route.route_id} className="mcp-template-item">
                  <label className="mcp-toggle">
                    <input
                      type="checkbox"
                      checked={entry !== undefined}
                      onChange={(e) => setExposed(route.route_id, e.target.checked)}
                      disabled={!canWrite}
                      aria-label={route.name}
                    />
                    <span className="mcp-api-head">
                      <span>{route.name}</span>
                      <code>{route.paths.join(', ')}</code>
                      <span className="mcp-template-meta">{route.methods.join(' · ')}</span>
                      <span className={route.key_auth ? 'badge badge-ok' : 'badge badge-error'}>
                        {route.key_auth ? t('mcp.apiKeyAuth') : t('mcp.apiPublic')}
                      </span>
                    </span>
                  </label>
                  {entry !== undefined && (
                    <div className="mcp-api-description">
                      <label htmlFor={descriptionId}>{t('mcp.apiDescription')}</label>
                      <textarea
                        id={descriptionId}
                        rows={3}
                        maxLength={4000}
                        value={entry.description}
                        onChange={(e) => setDescription(route.route_id, e.target.value)}
                        placeholder={route.description || t('mcp.apiDescriptionPlaceholder')}
                        disabled={!canWrite}
                      />
                    </div>
                  )}
                </li>
              );
            })}
            {shown.length === 0 && <li className="form-hint">{t('mcp.apisNoMatch')}</li>}
          </ul>
        </>
      )}
    </div>
  );
}

function McpSettingsForm({ settings }: { settings: McpSettings }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const canWrite = useCanWrite('admin.config.write');
  const [enabled, setEnabled] = useState(settings.enabled);
  const [toolGroups, setToolGroups] = useState(settings.tool_groups);
  const [excludedTemplates, setExcludedTemplates] = useState(settings.excluded_templates);
  const [exposedRoutes, setExposedRoutes] = useState(settings.exposed_routes);
  const [maxRows, setMaxRows] = useState(settings.max_rows);
  const [maxResultBytes, setMaxResultBytes] = useState(settings.max_result_bytes);

  const draft: McpSettings = {
    enabled,
    tool_groups: toolGroups,
    excluded_templates: excludedTemplates,
    exposed_routes: exposedRoutes,
    max_rows: maxRows,
    max_result_bytes: maxResultBytes,
  };
  const hasChanges = settingsKey(draft) !== settingsKey(settings);
  const maxRowsValid = inRange(maxRows, MAX_ROWS_RANGE);
  const maxResultBytesValid = inRange(maxResultBytes, MAX_RESULT_BYTES_RANGE);
  const isValid = maxRowsValid && maxResultBytesValid;

  function applySettings(next: McpSettings) {
    setEnabled(next.enabled);
    setToolGroups(next.tool_groups);
    setExcludedTemplates(next.excluded_templates);
    setExposedRoutes(next.exposed_routes);
    setMaxRows(next.max_rows);
    setMaxResultBytes(next.max_result_bytes);
  }

  const updateMut = useMutation({
    mutationFn: updateMcpSettings,
    onSuccess: (updated) => {
      queryClient.setQueryData(['mcp-settings'], updated);
      queryClient.invalidateQueries({ queryKey: ['mcp-preview'] });
      applySettings(updated);
    },
  });

  function handleSave() {
    if (!hasChanges || !isValid || !canWrite) return;
    updateMut.mutate(draft);
  }

  function handleDiscard() {
    applySettings(settings);
    updateMut.reset();
  }

  return (
    <div className="settings-form">
      {!canWrite && (
        <div className="settings-readonly-notice" role="status">
          {t('mcp.readOnlyNotice')}
        </div>
      )}

      <div className="settings-card">
        <h3>{t('mcp.settingsSection')}</h3>
        <label className="mcp-toggle">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            disabled={!canWrite}
            aria-describedby="mcp-enabled-hint"
          />
          <span>{t('mcp.enabled')}</span>
        </label>
        <span id="mcp-enabled-hint" className="form-hint mcp-toggle-hint">{t('mcp.enabledHint')}</span>
      </div>

      <div className="settings-card">
        <h3>{t('mcp.toolGroupsSection')}</h3>
        <p className="form-hint mcp-section-hint">{t('mcp.toolGroupsHint')}</p>
        {MCP_TOOL_GROUPS.map((group) => (
          <div key={group} className="mcp-toggle-row">
            <label className="mcp-toggle">
              <input
                type="checkbox"
                checked={toolGroups[group]}
                onChange={(e) => setToolGroups({ ...toolGroups, [group]: e.target.checked })}
                disabled={!canWrite}
                aria-describedby={`mcp-group-${group}-hint`}
              />
              <span>{t(`mcp.groups.${group}.label`)}</span>
            </label>
            <span id={`mcp-group-${group}-hint`} className="form-hint mcp-toggle-hint">
              {t(`mcp.groups.${group}.hint`)}
            </span>
          </div>
        ))}
      </div>

      <McpTemplateExposure
        excluded={excludedTemplates}
        onChange={setExcludedTemplates}
        canWrite={canWrite}
        groupEnabled={toolGroups.templates}
      />

      <McpApiExposure
        exposed={exposedRoutes}
        onChange={setExposedRoutes}
        canWrite={canWrite}
        groupEnabled={toolGroups.apis}
      />

      <div className="settings-card">
        <h3>{t('mcp.limitsSection')}</h3>
        <div className="form-group">
          <label htmlFor="mcp-max-rows">{t('mcp.maxRows')}</label>
          <input
            id="mcp-max-rows"
            type="number"
            min={MAX_ROWS_RANGE.min}
            max={MAX_ROWS_RANGE.max}
            value={maxRows}
            aria-invalid={!maxRowsValid}
            aria-describedby="mcp-max-rows-hint"
            onChange={(e) => setMaxRows(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="mcp-max-rows-hint" className={maxRowsValid ? 'form-hint' : 'form-hint mcp-invalid'}>
            {maxRowsValid ? t('mcp.maxRowsHint') : t('mcp.invalidRange', MAX_ROWS_RANGE)}
          </span>
        </div>
        <div className="form-group">
          <label htmlFor="mcp-max-result-bytes">{t('mcp.maxResultBytes')}</label>
          <input
            id="mcp-max-result-bytes"
            type="number"
            min={MAX_RESULT_BYTES_RANGE.min}
            max={MAX_RESULT_BYTES_RANGE.max}
            value={maxResultBytes}
            aria-invalid={!maxResultBytesValid}
            aria-describedby="mcp-max-result-bytes-hint"
            onChange={(e) => setMaxResultBytes(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span
            id="mcp-max-result-bytes-hint"
            className={maxResultBytesValid ? 'form-hint' : 'form-hint mcp-invalid'}
          >
            {maxResultBytesValid ? t('mcp.maxResultBytesHint') : t('mcp.invalidRange', MAX_RESULT_BYTES_RANGE)}
          </span>
        </div>
      </div>

      {canWrite && (
        <div className="settings-save-bar">
          <span className={hasChanges ? 'settings-save-status settings-save-status--changed' : 'settings-save-status'}>
            {hasChanges ? t('mcp.unsavedChanges') : t('mcp.noChanges')}
          </span>
          <div className="settings-save-actions">
            <button
              className="btn btn-secondary"
              type="button"
              onClick={handleDiscard}
              disabled={!hasChanges || updateMut.isPending}
            >
              {t('mcp.discardChanges')}
            </button>
            <button
              className="btn btn-primary"
              type="button"
              onClick={handleSave}
              disabled={!hasChanges || !isValid || updateMut.isPending}
              aria-busy={updateMut.isPending}
            >
              {updateMut.isPending ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </div>
      )}

      {updateMut.isSuccess && !hasChanges && (
        <span className="save-success" role="status">{t('mcp.saved')}</span>
      )}
      {updateMut.isError && (
        <span className="save-error" role="alert">{t('mcp.saveFailed')}</span>
      )}
    </div>
  );
}

function CopyBlock({ label, text }: { label: string; text: string }) {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => {
        setCopied(false);
        timerRef.current = null;
      }, 2000);
    } catch {
      setCopied(false);
      addToast({ type: 'error', title: t('mcp.copyFailed') });
    }
  }

  return (
    <section className="mcp-snippet">
      <div className="mcp-snippet-header">
        <h4>{label}</h4>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={handleCopy}
          aria-label={`${t('mcp.copy')}: ${label}`}
        >
          {copied ? t('mcp.copied') : t('mcp.copy')}
        </button>
      </div>
      <pre className="mcp-code">{text}</pre>
    </section>
  );
}

function McpConnectionGuide() {
  const { t } = useTranslation();
  const url = `${window.location.origin}/api/mcp`;
  const claudeCode = `claude mcp add --transport http unibridge ${url} --header "apikey: <API_KEY>"`;
  const mcpJson = JSON.stringify(
    { mcpServers: { unibridge: { type: 'http', url, headers: { apikey: '<API_KEY>' } } } },
    null,
    2,
  );
  const codex = `[mcp_servers.unibridge]\nurl = "${url}"\nhttp_headers = { apikey = "<API_KEY>" }`;

  return (
    <div className="settings-card">
      <h3>{t('mcp.guideSection')}</h3>
      <CopyBlock label={t('mcp.endpoint')} text={url} />
      <CopyBlock label={t('mcp.claudeCode')} text={claudeCode} />
      <CopyBlock label={t('mcp.mcpJson')} text={mcpJson} />
      <CopyBlock label={t('mcp.codex')} text={codex} />
      <p className="form-hint">{t('mcp.grantNote')}</p>
    </div>
  );
}

function YesNo({ value }: { value: boolean }) {
  const { t } = useTranslation();
  return (
    <span className={value ? 'badge badge-ok' : 'badge badge-error'}>
      {value ? t('mcp.yes') : t('mcp.no')}
    </span>
  );
}

function McpToolPreview() {
  const { t } = useTranslation();
  const canListKeys = useCanWrite('apikeys.read');
  const [consumer, setConsumer] = useState('');
  const [typedConsumer, setTypedConsumer] = useState('');

  const keysQuery = useQuery({
    queryKey: ['api-keys', 'list', 'all'],
    queryFn: () => getScopedApiKeys('all'),
    enabled: canListKeys,
  });

  const previewQuery = useQuery({
    queryKey: ['mcp-preview', consumer],
    queryFn: () => getMcpPreview(consumer),
    enabled: consumer !== '',
    retry: false,
  });

  const preview = previewQuery.data;
  const previewStatus = httpStatus(previewQuery.error);

  return (
    <div className="settings-card">
      <h3>{t('mcp.previewSection')}</h3>
      <p className="form-hint mcp-section-hint">{t('mcp.previewHint')}</p>

      {canListKeys ? (
        <div className="form-group">
          <label htmlFor="mcp-preview-key">{t('mcp.previewKey')}</label>
          <select
            id="mcp-preview-key"
            className="mcp-select"
            value={consumer}
            onChange={(e) => setConsumer(e.target.value)}
          >
            <option value="">{t('mcp.previewKeyPlaceholder')}</option>
            {(keysQuery.data ?? []).map((key) => (
              <option key={key.name} value={key.name}>{key.name}</option>
            ))}
          </select>
        </div>
      ) : (
        <form
          className="form-group mcp-preview-form"
          onSubmit={(e) => {
            e.preventDefault();
            setConsumer(typedConsumer.trim());
          }}
        >
          <label htmlFor="mcp-preview-consumer">{t('mcp.previewConsumerInput')}</label>
          <div className="mcp-preview-input-row">
            <input
              id="mcp-preview-consumer"
              type="text"
              value={typedConsumer}
              onChange={(e) => setTypedConsumer(e.target.value)}
            />
            <button type="submit" className="btn btn-secondary" disabled={typedConsumer.trim() === ''}>
              {t('mcp.previewButton')}
            </button>
          </div>
        </form>
      )}

      {consumer !== '' && previewQuery.isLoading && (
        <div className="loading-message" role="status">{t('common.loading')}</div>
      )}
      {previewQuery.isError && (
        <div className="error-banner" role="alert">
          {previewStatus === 404 ? t('mcp.previewNotFound') : t('mcp.previewFailed')}
        </div>
      )}

      {preview && (
        <div className="mcp-preview">
          {!preview.mcp_enabled && (
            <div className="settings-readonly-notice" role="status">{t('mcp.previewDisabled')}</div>
          )}
          {!preview.has_mcp_grant && (
            <div className="settings-readonly-notice" role="status">{t('mcp.previewNoGrant')}</div>
          )}

          <div className="table-container">
            <table className="data-table" aria-label={t('mcp.groupsTable')}>
              <thead>
                <tr>
                  <th>{t('mcp.colGroup')}</th>
                  <th>{t('mcp.colEnabled')}</th>
                  <th>{t('mcp.colGranted')}</th>
                  <th>{t('mcp.colRequiredRoute')}</th>
                </tr>
              </thead>
              <tbody>
                {preview.groups.map((group) => (
                  <tr key={group.group}>
                    <td>{t(`mcp.groups.${group.group}.label`)}</td>
                    <td><YesNo value={group.enabled} /></td>
                    <td><YesNo value={group.granted} /></td>
                    <td>
                      {group.group === 'apis'
                        ? t('mcp.perRouteGrant')
                        : <code>{group.required_route}</code>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {preview.tools.some((tool) => tool.group === 'templates') && (
            <>
              <h4 className="mcp-tools-title">
                {t('mcp.previewTemplateCount', { count: preview.templates.length })}
              </h4>
              {preview.templates.length === 0 ? (
                <p className="form-hint">{t('mcp.previewNoTemplates')}</p>
              ) : (
                <div className="table-container">
                  <table className="data-table" aria-label={t('mcp.previewTemplatesTable')}>
                    <thead>
                      <tr>
                        <th>{t('mcp.colTemplate')}</th>
                        <th>{t('mcp.colName')}</th>
                        <th>{t('mcp.colDatabase')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.templates.map((template) => (
                        <tr key={template.path}>
                          <td><code>{template.path}</code></td>
                          <td>{template.name}</td>
                          <td>{template.database}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}

          {preview.apis_error && (
            <div className="error-banner" role="alert">{t('mcp.previewApisFailed')}</div>
          )}
          {preview.apis.length > 0 && (
            <>
              <h4 className="mcp-tools-title">{t('mcp.previewApiCount', { count: preview.apis.length })}</h4>
              <div className="table-container">
                <table className="data-table" aria-label={t('mcp.previewApisTable')}>
                  <thead>
                    <tr>
                      <th>{t('mcp.colApi')}</th>
                      <th>{t('mcp.colMethods')}</th>
                      <th>{t('mcp.colPaths')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.apis.map((api) => (
                      <tr key={api.route_id}>
                        <td>{api.name}</td>
                        <td>{api.methods.join(' · ')}</td>
                        <td><code>{api.paths.join(', ')}</code></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <h4 className="mcp-tools-title">{t('mcp.toolCount', { count: preview.tools.length })}</h4>
          {preview.tools.length === 0 ? (
            <p className="form-hint">{t('mcp.noTools')}</p>
          ) : (
            <div className="table-container">
              <table className="data-table" aria-label={t('mcp.toolsTable')}>
                <thead>
                  <tr>
                    <th>{t('mcp.colTool')}</th>
                    <th>{t('mcp.colGroup')}</th>
                    <th>{t('mcp.colDescription')}</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.tools.map((tool) => (
                    <tr key={tool.name}>
                      <td>
                        <code>{tool.name}</code>
                        {tool.title && tool.title !== tool.name && (
                          <div className="mcp-tool-title">{tool.title}</div>
                        )}
                      </td>
                      <td>{t(`mcp.groups.${tool.group}.label`, { defaultValue: tool.group })}</td>
                      <td className="mcp-tool-description">{tool.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function McpSettingsPage() {
  const { t } = useTranslation();

  const settingsQuery = useQuery({
    queryKey: ['mcp-settings'],
    queryFn: getMcpSettings,
  });

  return (
    <div className="mcp-settings">
      <div className="page-header">
        <h1>{t('mcp.title')}</h1>
        <p className="page-subtitle">{t('mcp.subtitle')}</p>
      </div>

      {settingsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}

      {settingsQuery.isError && (
        <div className="error-banner" role="alert">{t('mcp.loadFailed')}</div>
      )}

      <div className="mcp-sections">
        {settingsQuery.data && <McpSettingsForm settings={settingsQuery.data} />}
        <McpConnectionGuide />
        <McpToolPreview />
      </div>
    </div>
  );
}

export default McpSettingsPage;
