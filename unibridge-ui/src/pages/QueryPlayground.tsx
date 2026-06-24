import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getDatabases, executeQuery, type QueryResult } from '../api/client';
import QueryHistoryPanel from '../components/QueryHistoryPanel';
import SavedQueriesPanel from '../components/SavedQueriesPanel';
import SaveQueryModal from '../components/SaveQueryModal';
import './QueryPlayground.css';

type WorkspaceTab = 'history' | 'saved';

function formatResultValue(value: unknown): string {
  if (value === null) return 'NULL';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function QueryPlayground() {
  const { t } = useTranslation();
  const [selectedDb, setSelectedDb] = useState('');
  const [sql, setSql] = useState('');
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('history');
  const [saveModalOpen, setSaveModalOpen] = useState(false);
  const workspaceTabs: WorkspaceTab[] = ['history', 'saved'];

  const dbsQuery = useQuery({
    queryKey: ['databases'],
    queryFn: getDatabases,
  });

  const execMutation = useMutation({
    mutationFn: (req: { database: string; sql: string }) => executeQuery(req),
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (err: unknown) => {
      setResult(null);
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? t('queryPlayground.executionFailed'));
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError(t('queryPlayground.executionFailed'));
      }
    },
  });

  const databases = dbsQuery.data ?? [];
  const selectedDbType = databases.find((d) => d.alias === selectedDb)?.db_type;
  const isSparql = selectedDbType === 'graphdb';
  const editorLabel = isSparql ? 'SPARQL' : 'SQL';
  const editorPlaceholder = isSparql
    ? 'SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10'
    : 'SELECT * FROM users LIMIT 10;';
  const hasQueryState = Boolean(sql.trim() || result || error);

  function handleExecute() {
    if (!selectedDb || !sql.trim()) return;
    setResult(null);
    setError(null);
    execMutation.mutate({ database: selectedDb, sql });
  }

  function handleClear() {
    setSql('');
    setResult(null);
    setError(null);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    // Ctrl/Cmd + Enter to execute
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      handleExecute();
    }
  }

  function focusWorkspaceTab(tab: WorkspaceTab) {
    window.requestAnimationFrame(() => {
      document.getElementById(`query-workspace-tab-${tab}`)?.focus();
    });
  }

  function handleWorkspaceTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, tab: WorkspaceTab) {
    const currentIndex = workspaceTabs.indexOf(tab);
    const nextIndex = (() => {
      if (event.key === 'ArrowRight') return (currentIndex + 1) % workspaceTabs.length;
      if (event.key === 'ArrowLeft') return (currentIndex - 1 + workspaceTabs.length) % workspaceTabs.length;
      if (event.key === 'Home') return 0;
      if (event.key === 'End') return workspaceTabs.length - 1;
      return null;
    })();

    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = workspaceTabs[nextIndex];
    setActiveTab(nextTab);
    focusWorkspaceTab(nextTab);
  }

  function handleLoadQuery(loadedSql: string, databaseAlias: string | null) {
    setSql(loadedSql);
    if (databaseAlias && databases.some((db) => db.alias === databaseAlias)) {
      setSelectedDb(databaseAlias);
    }
    setResult(null);
    setError(null);
  }

  return (
    <div className="query-playground">
      <div className="page-header">
        <h1>{t('queryPlayground.title')}</h1>
        <p className="page-subtitle">{t('queryPlayground.subtitle')}</p>
      </div>

      <div className="playground-controls">
        <select
          value={selectedDb}
          onChange={(e) => setSelectedDb(e.target.value)}
          className="db-selector"
          aria-label={t('queryPlayground.databaseLabel')}
          disabled={dbsQuery.isLoading}
        >
          <option value="">
            {dbsQuery.isLoading ? t('common.loading') : t('queryPlayground.selectDatabase')}
          </option>
          {databases.map((db) => (
            <option key={db.alias} value={db.alias}>
              {db.alias}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn-primary btn-execute"
          onClick={handleExecute}
          disabled={!selectedDb || !sql.trim() || execMutation.isPending}
          aria-busy={execMutation.isPending}
        >
          {execMutation.isPending ? t('queryPlayground.executing') : t('queryPlayground.execute')}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => setSaveModalOpen(true)}
          disabled={!sql.trim()}
        >
          {t('queryPlayground.saveQuery')}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={handleClear}
          disabled={!hasQueryState || execMutation.isPending}
        >
          {t('queryPlayground.clear')}
        </button>
        <span className="shortcut-hint">{t('queryPlayground.shortcutHint')}</span>
      </div>

      {dbsQuery.isError && (
        <div className="error-banner" role="alert">{t('queryPlayground.databasesLoadFailed')}</div>
      )}

      <div className="editor-container">
        <div className="editor-topbar">
          <span className="editor-topbar-label">{editorLabel}</span>
          <div className="editor-topbar-dots" aria-hidden="true">
            <span className="editor-topbar-dot" />
            <span className="editor-topbar-dot" />
            <span className="editor-topbar-dot" />
          </div>
        </div>
        <textarea
          className="sql-editor"
          placeholder={editorPlaceholder}
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={handleKeyDown}
          aria-label={isSparql ? t('queryPlayground.sparqlEditorLabel') : t('queryPlayground.sqlEditorLabel')}
          rows={10}
          spellCheck={false}
        />
      </div>

      {/* Error */}
      {error && (
        <div className="query-error" role="alert">
          <strong>{t('common.error')}:</strong> {error}
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="query-results">
          <div className="results-meta" role="status" aria-live="polite">
            <span>{t('queryPlayground.rowsReturned', { count: result.row_count })}</span>
            <span>{result.elapsed_ms}ms</span>
          </div>

          {result.truncated && (
            <div className="truncated-warning" role="alert">
              {t('queryPlayground.truncatedWarning', { count: result.row_count })}
            </div>
          )}

          {result.graph ? (
            <pre className="rdf-graph">{result.graph}</pre>
          ) : result.columns.length > 0 && result.rows.length > 0 ? (
            <div className="results-table-container">
              <table className="results-table">
                <thead>
                  <tr>
                    {result.columns.map((col, idx) => (
                      <th key={`${col}-${idx}`} scope="col">{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, idx) => (
                    <tr key={idx}>
                      {result.columns.map((_col, colIdx) => {
                        const cellValue = row[colIdx];
                        const formattedValue = formatResultValue(cellValue);
                        return (
                          <td key={colIdx} className="mono" title={formattedValue}>
                            {cellValue === null ? (
                              <span className="null-value">NULL</span>
                            ) : (
                              formattedValue
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="no-rows" role="status">{t('queryPlayground.noRows')}</div>
          )}
        </div>
      )}

      {/* History & saved queries */}
      <div className="playground-workspace">
        <div className="workspace-tabs" role="tablist" aria-label={t('queryPlayground.workspaceTabs')}>
          <button
            id="query-workspace-tab-history"
            type="button"
            role="tab"
            aria-selected={activeTab === 'history'}
            aria-controls="query-workspace-panel-history"
            tabIndex={activeTab === 'history' ? 0 : -1}
            className={`workspace-tab ${activeTab === 'history' ? 'workspace-tab--active' : ''}`}
            onClick={() => setActiveTab('history')}
            onKeyDown={(event) => handleWorkspaceTabKeyDown(event, 'history')}
          >
            {t('queryPlayground.historyTab')}
          </button>
          <button
            id="query-workspace-tab-saved"
            type="button"
            role="tab"
            aria-selected={activeTab === 'saved'}
            aria-controls="query-workspace-panel-saved"
            tabIndex={activeTab === 'saved' ? 0 : -1}
            className={`workspace-tab ${activeTab === 'saved' ? 'workspace-tab--active' : ''}`}
            onClick={() => setActiveTab('saved')}
            onKeyDown={(event) => handleWorkspaceTabKeyDown(event, 'saved')}
          >
            {t('queryPlayground.savedTab')}
          </button>
        </div>
        {activeTab === 'history' ? (
          <div
            id="query-workspace-panel-history"
            role="tabpanel"
            aria-labelledby="query-workspace-tab-history"
          >
            <QueryHistoryPanel onLoad={handleLoadQuery} />
          </div>
        ) : (
          <div
            id="query-workspace-panel-saved"
            role="tabpanel"
            aria-labelledby="query-workspace-tab-saved"
          >
            <SavedQueriesPanel onLoad={handleLoadQuery} />
          </div>
        )}
      </div>

      {saveModalOpen && (
        <SaveQueryModal
          sql={sql}
          databaseAlias={selectedDb || null}
          onClose={() => setSaveModalOpen(false)}
        />
      )}
    </div>
  );
}

export default QueryPlayground;
