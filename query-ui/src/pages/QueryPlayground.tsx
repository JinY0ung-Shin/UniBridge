import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getDatabases, executeQuery, type QueryResult } from '../api/client';
import './QueryPlayground.css';

function QueryPlayground() {
  const { t } = useTranslation();
  const [selectedDb, setSelectedDb] = useState('');
  const [sql, setSql] = useState('');
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  function handleExecute() {
    if (!selectedDb || !sql.trim()) return;
    setResult(null);
    setError(null);
    execMutation.mutate({ database: selectedDb, sql });
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    // Ctrl/Cmd + Enter to execute
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      handleExecute();
    }
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
        >
          <option value="">{t('queryPlayground.selectDatabase')}</option>
          {databases.map((db) => (
            <option key={db.alias} value={db.alias}>
              {db.alias}
            </option>
          ))}
        </select>
        <button
          className="btn btn-primary btn-execute"
          onClick={handleExecute}
          disabled={!selectedDb || !sql.trim() || execMutation.isPending}
        >
          {execMutation.isPending ? t('queryPlayground.executing') : t('queryPlayground.execute')}
        </button>
        <span className="shortcut-hint">{t('queryPlayground.shortcutHint')}</span>
      </div>

      <div className="editor-container">
        <div className="editor-topbar">
          <span className="editor-topbar-label">SQL</span>
          <div className="editor-topbar-dots">
            <span className="editor-topbar-dot" />
            <span className="editor-topbar-dot" />
            <span className="editor-topbar-dot" />
          </div>
        </div>
        <textarea
          className="sql-editor"
          placeholder="SELECT * FROM users LIMIT 10;"
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={10}
          spellCheck={false}
        />
      </div>

      {/* Error */}
      {error && (
        <div className="query-error">
          <strong>{t('common.error')}:</strong> {error}
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="query-results">
          <div className="results-meta">
            <span>{t('queryPlayground.rowsReturned', { count: result.row_count })}</span>
            <span>{result.elapsed_ms}ms</span>
          </div>

          {result.truncated && (
            <div className="truncated-warning">
              {t('queryPlayground.truncatedWarning', { count: result.row_count })}
            </div>
          )}

          {result.columns.length > 0 && result.rows.length > 0 ? (
            <div className="results-table-container">
              <table className="results-table">
                <thead>
                  <tr>
                    {result.columns.map((col, idx) => (
                      <th key={`${col}-${idx}`}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, idx) => (
                    <tr key={idx}>
                      {result.columns.map((_col, colIdx) => (
                        <td key={colIdx} className="mono">
                          {row[colIdx] === null ? (
                            <span className="null-value">NULL</span>
                          ) : typeof row[colIdx] === 'object' ? (
                            JSON.stringify(row[colIdx])
                          ) : (
                            String(row[colIdx])
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="no-rows">{t('queryPlayground.noRows')}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default QueryPlayground;
