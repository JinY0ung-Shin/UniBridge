import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { getDatabases, executeQuery, type QueryResult } from '../api/client';
import './QueryPlayground.css';

function QueryPlayground() {
  const [selectedDb, setSelectedDb] = useState('');
  const [sql, setSql] = useState('');
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dbsQuery = useQuery({
    queryKey: ['databases'],
    queryFn: getDatabases,
  });

  const execMutation = useMutation({
    mutationFn: () => executeQuery({ database: selectedDb, sql }),
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (err: unknown) => {
      setResult(null);
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Query execution failed');
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('Query execution failed');
      }
    },
  });

  const databases = dbsQuery.data ?? [];

  function handleExecute() {
    if (!selectedDb || !sql.trim()) return;
    setResult(null);
    setError(null);
    execMutation.mutate();
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
        <h1>Query Playground</h1>
        <p className="page-subtitle">Execute SQL queries against connected databases</p>
      </div>

      <div className="playground-controls">
        <select
          value={selectedDb}
          onChange={(e) => setSelectedDb(e.target.value)}
          className="db-selector"
        >
          <option value="">Select a database...</option>
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
          {execMutation.isPending ? 'Executing...' : 'Execute'}
        </button>
        <span className="shortcut-hint">Ctrl+Enter to run</span>
      </div>

      <div className="editor-area">
        <textarea
          className="sql-editor mono"
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
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="query-results">
          <div className="results-meta">
            <span>{result.row_count} row{result.row_count !== 1 ? 's' : ''} returned</span>
            <span>{result.elapsed_ms}ms</span>
          </div>

          {result.columns.length > 0 && result.rows.length > 0 ? (
            <div className="results-table-container">
              <table className="results-table">
                <thead>
                  <tr>
                    {result.columns.map((col) => (
                      <th key={col}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, idx) => (
                    <tr key={idx}>
                      {result.columns.map((col, colIdx) => (
                        <td key={col} className="mono">
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
            <div className="no-rows">Query executed successfully. No rows returned.</div>
          )}
        </div>
      )}
    </div>
  );
}

export default QueryPlayground;
