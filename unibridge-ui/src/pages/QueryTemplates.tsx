import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  createQueryTemplate,
  deleteQueryTemplate,
  executeQueryTemplate,
  getDatabases,
  getQueryTemplates,
  updateQueryTemplate,
  type QueryResult,
  type QueryTemplate,
  type QueryTemplateCreate,
  type QueryTemplateUpdate,
} from '../api/client';
import { usePermissions } from '../components/usePermissions';
import './QueryTemplates.css';

interface TemplateFormState {
  path: string;
  name: string;
  description: string;
  database: string;
  sql: string;
  default_limit: string;
  timeout: string;
  enabled: boolean;
}

const emptyForm: TemplateFormState = {
  path: '',
  name: '',
  description: '',
  database: '',
  sql: 'SELECT * FROM users WHERE id = :id',
  default_limit: '100',
  timeout: '',
  enabled: true,
};

function errorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const axiosErr = err as { response?: { data?: { detail?: string } } };
    return axiosErr.response?.data?.detail ?? fallback;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return fallback;
}

function templateToForm(template: QueryTemplate): TemplateFormState {
  return {
    path: template.path,
    name: template.name,
    description: template.description,
    database: template.database,
    sql: template.sql,
    default_limit: template.default_limit ? String(template.default_limit) : '',
    timeout: template.timeout ? String(template.timeout) : '',
    enabled: template.enabled,
  };
}

function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  return trimmed ? Number(trimmed) : null;
}

function toCreatePayload(form: TemplateFormState): QueryTemplateCreate {
  return {
    path: form.path.trim(),
    name: form.name.trim(),
    description: form.description.trim(),
    database: form.database,
    sql: form.sql.trim(),
    default_limit: toNumberOrNull(form.default_limit),
    timeout: toNumberOrNull(form.timeout),
    enabled: form.enabled,
  };
}

function toUpdatePayload(form: TemplateFormState): QueryTemplateUpdate {
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    database: form.database,
    sql: form.sql.trim(),
    default_limit: toNumberOrNull(form.default_limit),
    timeout: toNumberOrNull(form.timeout),
    enabled: form.enabled,
  };
}

function ResultTable({ result }: { result: QueryResult }) {
  const { t } = useTranslation();

  return (
    <div className="template-run-result">
      <div className="results-meta" role="status" aria-live="polite">
        <span>{t('queryPlayground.rowsReturned', { count: result.row_count })}</span>
        <span>{result.elapsed_ms}ms</span>
      </div>

      {result.truncated && (
        <div className="truncated-warning" role="alert">
          {t('queryPlayground.truncatedWarning', { count: result.row_count })}
        </div>
      )}

      {result.columns.length > 0 && result.rows.length > 0 ? (
        <div className="results-table-container">
          <table className="results-table">
            <thead>
              <tr>
                {result.columns.map((column, index) => (
                  <th key={`${column}-${index}`} scope="col">{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {result.columns.map((_column, columnIndex) => (
                    <td key={columnIndex} className="mono">
                      {row[columnIndex] === null ? (
                        <span className="null-value">NULL</span>
                      ) : typeof row[columnIndex] === 'object' ? (
                        JSON.stringify(row[columnIndex])
                      ) : (
                        String(row[columnIndex])
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="no-rows" role="status">{t('queryPlayground.noRows')}</div>
      )}
    </div>
  );
}

function QueryTemplates() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { permissions } = usePermissions();
  const canWrite = permissions.includes('query.settings.write');
  const canExecute = permissions.includes('query.execute');

  const [form, setForm] = useState<TemplateFormState>(emptyForm);
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [paramsText, setParamsText] = useState('{\n  "id": 1\n}');
  const [limit, setLimit] = useState('');
  const [timeout, setTimeoutValue] = useState('');
  const [result, setResult] = useState<QueryResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [templateSearch, setTemplateSearch] = useState('');

  const templatesQuery = useQuery({
    queryKey: ['query-templates'],
    queryFn: getQueryTemplates,
  });

  const databasesQuery = useQuery({
    queryKey: ['databases'],
    queryFn: getDatabases,
  });

  const templates = useMemo(() => templatesQuery.data ?? [], [templatesQuery.data]);
  const databases = useMemo(() => databasesQuery.data ?? [], [databasesQuery.data]);
  const filteredTemplates = useMemo(() => {
    const normalizedSearch = templateSearch.trim().toLowerCase();
    if (!normalizedSearch) return templates;
    return templates.filter((template) => [
      template.name,
      template.path,
      template.description,
      template.database,
      template.enabled ? t('common.active') : t('common.disabled'),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(normalizedSearch));
  }, [templateSearch, templates, t]);
  const selectedTemplate = useMemo(
    () => templates.find((template) => template.path === selectedPath) ?? templates[0],
    [selectedPath, templates],
  );

  const saveMutation = useMutation({
    mutationFn: (payload: { path: string | null; form: TemplateFormState }) => {
      if (payload.path) {
        return updateQueryTemplate(payload.path, toUpdatePayload(payload.form));
      }
      return createQueryTemplate(toCreatePayload(payload.form));
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<QueryTemplate[]>(['query-templates'], (current) => {
        const templates = current ?? [];
        const existingIndex = templates.findIndex((template) => template.path === saved.path);
        if (existingIndex === -1) return [saved, ...templates];
        return templates.map((template, index) => (index === existingIndex ? saved : template));
      });
      setSelectedPath(saved.path);
      setEditingPath(null);
      setForm(emptyForm);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteQueryTemplate,
    onSuccess: async (_data, deletedPath) => {
      await queryClient.invalidateQueries({ queryKey: ['query-templates'] });
      if (selectedPath === deletedPath) {
        setSelectedPath(null);
        setResult(null);
        setRunError(null);
      }
      if (editingPath === deletedPath) {
        setEditingPath(null);
        setForm(emptyForm);
      }
    },
  });
  const selectedTemplateDeleting =
    !!selectedTemplate && deleteMutation.isPending && deleteMutation.variables === selectedTemplate.path;

  const runMutation = useMutation({
    mutationFn: (path: string) => {
      let params: Record<string, unknown> | undefined;
      const trimmedParams = paramsText.trim();
      if (trimmedParams) {
        const parsed = JSON.parse(trimmedParams);
        if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
          throw new Error(t('queryTemplates.paramsMustBeObject'));
        }
        params = parsed as Record<string, unknown>;
      }
      return executeQueryTemplate(path, {
        params,
        limit: limit.trim() ? Number(limit) : undefined,
        timeout: timeout.trim() ? Number(timeout) : undefined,
      });
    },
    onSuccess: (data) => {
      setResult(data);
      setRunError(null);
    },
    onError: (err) => {
      setResult(null);
      setRunError(errorMessage(err, t('queryTemplates.runFailed')));
    },
  });

  function updateForm<K extends keyof TemplateFormState>(key: K, value: TemplateFormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function handleEdit(template: QueryTemplate) {
    setEditingPath(template.path);
    setForm(templateToForm(template));
  }

  function handleCancelEdit() {
    setEditingPath(null);
    setForm(emptyForm);
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canWrite) return;
    saveMutation.mutate({ path: editingPath, form });
  }

  function handleDelete(template: QueryTemplate) {
    if (!canWrite) return;
    if (window.confirm(t('queryTemplates.deleteConfirm', { path: template.path }))) {
      deleteMutation.mutate(template.path);
    }
  }

  function handleRun() {
    if (!selectedTemplate || !canExecute) return;
    setResult(null);
    setRunError(null);
    runMutation.mutate(selectedTemplate.path);
  }

  return (
    <div className="query-templates">
      <div className="page-header">
        <h1>{t('queryTemplates.title')}</h1>
        <p className="page-subtitle">{t('queryTemplates.subtitle')}</p>
      </div>

      {templatesQuery.isLoading && <div className="loading-message" role="status">{t('queryTemplates.loading')}</div>}
      {templatesQuery.isError && <div className="error-banner" role="alert">{t('queryTemplates.loadFailed')}</div>}

      <div className="template-layout">
        <section className="template-list-panel">
          <div className="template-section-header">
            <h2>{t('queryTemplates.savedTemplates')}</h2>
            <div className="template-list-actions">
              {templates.length > 0 && (
                <input
                  className="template-search-input"
                  type="search"
                  value={templateSearch}
                  onChange={(event) => setTemplateSearch(event.target.value)}
                  placeholder={t('queryTemplates.searchPlaceholder')}
                  aria-label={t('queryTemplates.searchPlaceholder')}
                />
              )}
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                aria-label={t('queryTemplates.refreshTemplates')}
                title={t('queryTemplates.refreshTemplates')}
                onClick={() => templatesQuery.refetch()}
              >
                {t('common.refresh')}
              </button>
            </div>
          </div>

          {templates.length > 0 && filteredTemplates.length > 0 ? (
            <div className="template-list">
              {filteredTemplates.map((template) => (
                <button
                  key={template.path}
                  type="button"
                  className={`template-row ${selectedTemplate?.path === template.path ? 'template-row--active' : ''}`}
                  aria-pressed={selectedTemplate?.path === template.path}
                  onClick={() => setSelectedPath(template.path)}
                >
                  <span className="template-row-main">
                    <span className="template-row-name">{template.name}</span>
                    <span className="template-row-path">/api/query/templates/{template.path}</span>
                  </span>
                  <span className={`status-pill ${template.enabled ? 'status-pill--active' : 'status-pill--disabled'}`}>
                    {template.enabled ? t('common.active') : t('common.disabled')}
                  </span>
                </button>
              ))}
            </div>
          ) : templates.length > 0 ? (
            <div className="empty-state">
              <h3>{t('queryTemplates.noSearchResults')}</h3>
              <p>{t('queryTemplates.noSearchResultsDesc')}</p>
              <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setTemplateSearch('')}>
                {t('common.clearSearch')}
              </button>
            </div>
          ) : (
            !templatesQuery.isLoading && (
              <div className="empty-state">
                <h3>{t('queryTemplates.noTemplates')}</h3>
                <p>{t('queryTemplates.noTemplatesDesc')}</p>
              </div>
            )
          )}
        </section>

        <section className="template-form-panel">
          <div className="template-section-header">
            <h2>{editingPath ? t('queryTemplates.editTitle') : t('queryTemplates.createTitle')}</h2>
            {editingPath && (
              <button type="button" className="btn btn-secondary btn-sm" onClick={handleCancelEdit}>
                {t('common.cancel')}
              </button>
            )}
          </div>

          <form className="template-form" onSubmit={handleSubmit}>
            <div className="template-form-grid">
              <div className="form-group">
                <label htmlFor="template-path">{t('queryTemplates.path')}</label>
                <input
                  id="template-path"
                  value={form.path}
                  disabled={Boolean(editingPath)}
                  onChange={(event) => updateForm('path', event.target.value)}
                  placeholder="reports/users"
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="template-name">{t('common.name')}</label>
                <input
                  id="template-name"
                  value={form.name}
                  onChange={(event) => updateForm('name', event.target.value)}
                  placeholder="User lookup"
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="template-database">{t('connections.database')}</label>
                <select
                  id="template-database"
                  value={form.database}
                  onChange={(event) => updateForm('database', event.target.value)}
                  required
                >
                  <option value="">{t('queryPlayground.selectDatabase')}</option>
                  {databases.map((database) => (
                    <option key={database.alias} value={database.alias}>
                      {database.alias}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label htmlFor="template-description">{t('apiKeys.description')}</label>
                <input
                  id="template-description"
                  value={form.description}
                  onChange={(event) => updateForm('description', event.target.value)}
                  placeholder="Lookup active users"
                />
              </div>
              <div className="form-group">
                <label htmlFor="template-limit">{t('queryTemplates.defaultLimit')}</label>
                <input
                  id="template-limit"
                  type="number"
                  min={1}
                  value={form.default_limit}
                  onChange={(event) => updateForm('default_limit', event.target.value)}
                />
              </div>
              <div className="form-group">
                <label htmlFor="template-timeout">{t('queryTemplates.timeout')}</label>
                <input
                  id="template-timeout"
                  type="number"
                  min={1}
                  value={form.timeout}
                  onChange={(event) => updateForm('timeout', event.target.value)}
                />
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="template-sql">SQL</label>
              <textarea
                id="template-sql"
                className="template-sql-editor"
                value={form.sql}
                onChange={(event) => updateForm('sql', event.target.value)}
                rows={8}
                spellCheck={false}
                aria-describedby="template-sql-help"
                required
              />
              <div id="template-sql-help" className="template-parameter-help">
                <span>{t('queryTemplates.parameterSyntaxTitle')}</span>
                <p>{t('queryTemplates.parameterSyntaxIntro')}</p>
                <p className="template-parameter-examples">
                  <code>:id</code> Postgres / MSSQL
                  <code>{'{id:UInt64}'}</code> ClickHouse
                  <code>$id</code> Neo4j
                </p>
                <p>{t('queryTemplates.parameterSyntaxWarning')}</p>
              </div>
            </div>

            <label className="template-toggle">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(event) => updateForm('enabled', event.target.checked)}
              />
              <span>{t('queryTemplates.enabled')}</span>
            </label>

            <div className="template-form-actions">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={!canWrite || saveMutation.isPending}
                aria-busy={saveMutation.isPending}
              >
                {saveMutation.isPending
                  ? t('common.saving')
                  : editingPath
                    ? t('common.update')
                    : t('common.create')}
              </button>
              {saveMutation.isError && (
                <span className="save-error" role="alert">{errorMessage(saveMutation.error, t('queryTemplates.saveFailed'))}</span>
              )}
            </div>
          </form>
        </section>
      </div>

      {selectedTemplate && (
        <section className="template-detail-panel">
          <div className="template-detail-header">
            <div>
              <h2>{selectedTemplate.name}</h2>
              <span>/api/query/templates/{selectedTemplate.path}</span>
            </div>
            <div className="template-detail-actions">
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                aria-label={t('queryTemplates.editTemplate', { name: selectedTemplate.name })}
                onClick={() => handleEdit(selectedTemplate)}
              >
                {t('common.edit')}
              </button>
              {canWrite && (
                <button
                  type="button"
                  className="btn btn-danger btn-sm"
                  aria-label={t('queryTemplates.deleteTemplate', { name: selectedTemplate.name })}
                  onClick={() => handleDelete(selectedTemplate)}
                  disabled={deleteMutation.isPending}
                  aria-busy={selectedTemplateDeleting}
                >
                  {selectedTemplateDeleting ? t('common.deleting') : t('common.delete')}
                </button>
              )}
            </div>
          </div>

          <div className="template-detail-grid">
            <div className="template-sql-preview">
              <div className="editor-topbar">
                <span className="editor-topbar-label">{selectedTemplate.database}</span>
                <span className="template-muted">
                  {selectedTemplate.default_limit
                    ? t('queryTemplates.limitValue', { value: selectedTemplate.default_limit })
                    : t('queryTemplates.noLimitOverride')}
                </span>
              </div>
              <pre>{selectedTemplate.sql}</pre>
            </div>

            <div className="template-run-panel">
              <h3>{t('queryTemplates.runTemplate')}</h3>
              <div className="form-group">
                <label htmlFor="template-run-params">{t('queryTemplates.paramsJson')}</label>
                <textarea
                  id="template-run-params"
                  value={paramsText}
                  onChange={(event) => setParamsText(event.target.value)}
                  rows={7}
                  spellCheck={false}
                  aria-describedby="template-run-params-help"
                />
                <div id="template-run-params-help" className="template-parameter-help">
                  <span>{t('queryTemplates.parameterSyntaxTitle')}</span>
                  <p>{t('queryTemplates.paramsJsonHelp')}</p>
                  <p className="template-parameter-examples">
                    <code>{'{"id": 1}'}</code>
                    <code>:id</code> Postgres / MSSQL
                    <code>{'{id:UInt64}'}</code> ClickHouse
                    <code>$id</code> Neo4j
                  </p>
                  <p>{t('queryTemplates.parameterSyntaxWarning')}</p>
                </div>
              </div>
              <div className="template-run-options">
                <div className="form-group">
                  <label htmlFor="template-run-limit">{t('queryTemplates.limit')}</label>
                  <input
                    id="template-run-limit"
                    type="number"
                    min={1}
                    value={limit}
                    onChange={(event) => setLimit(event.target.value)}
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="template-run-timeout">{t('queryTemplates.timeout')}</label>
                  <input
                    id="template-run-timeout"
                    type="number"
                    min={1}
                    value={timeout}
                    onChange={(event) => setTimeoutValue(event.target.value)}
                  />
                </div>
              </div>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleRun}
                disabled={!canExecute || !selectedTemplate.enabled || runMutation.isPending}
                aria-busy={runMutation.isPending}
              >
                {runMutation.isPending ? t('queryPlayground.executing') : t('queryTemplates.run')}
              </button>
              {!selectedTemplate.enabled && (
                <span className="template-run-note">{t('queryTemplates.disabledCannotRun')}</span>
              )}
            </div>
          </div>

          {runError && (
            <div className="query-error" role="alert">
              <strong>{t('common.error')}:</strong> {runError}
            </div>
          )}

          {result && <ResultTable result={result} />}
        </section>
      )}
    </div>
  );
}

export default QueryTemplates;
