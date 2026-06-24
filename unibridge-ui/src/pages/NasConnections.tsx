import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getNasConnections,
  createNasConnection,
  updateNasConnection,
  deleteNasConnection,
  testNasConnection,
  type NasConnectionConfig,
} from '../api/client';
import ResourceModal from '../components/ResourceModal';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import './Connections.css';

const emptyForm: NasConnectionConfig = {
  alias: '',
  base_path: '',
  read_only: true,
  max_download_bytes: null,
  show_hidden: false,
  follow_symlinks: false,
};

function NasConnections() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('nas.connections.write');

  const [showModal, setShowModal] = useState(false);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [form, setForm] = useState<NasConnectionConfig>({ ...emptyForm });
  const [testResults, setTestResults] = useState<Record<string, { status: string }>>({});
  const [connectionSearch, setConnectionSearch] = useState('');

  const connQuery = useQuery({
    queryKey: ['nas-connections'],
    queryFn: getNasConnections,
  });

  const createMutation = useMutation({
    mutationFn: (data: NasConnectionConfig) => createNasConnection(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nas-connections'] });
      closeModal();
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ alias, data }: { alias: string; data: Partial<NasConnectionConfig> }) =>
      updateNasConnection(alias, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nas-connections'] });
      closeModal();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (alias: string) => deleteNasConnection(alias),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nas-connections'] });
    },
    onError: (_err, alias) => {
      addToast({ type: 'error', title: `${alias} — ${t('nas.deleteFailed')}` });
    },
  });

  const testMutation = useMutation({
    mutationFn: (alias: string) => testNasConnection(alias),
    onSuccess: (data, alias) => {
      setTestResults((prev) => ({ ...prev, [alias]: { status: data.status } }));
      addToast({
        type: data.status === 'ok' ? 'success' : 'error',
        title: `${alias} — ${data.status === 'ok' ? t('common.ok') : t('common.error')}`,
        message: data.message,
      });
    },
    onError: (_err, alias) => {
      setTestResults((prev) => ({ ...prev, [alias]: { status: 'error' } }));
      addToast({ type: 'error', title: `${alias} — ${t('nas.testFailed')}` });
    },
  });

  const connections = connQuery.data ?? [];
  const normalizedConnectionSearch = connectionSearch.trim().toLowerCase();
  const filteredConnections = normalizedConnectionSearch
    ? connections.filter((conn) => [
        conn.alias,
        conn.base_path,
        conn.show_hidden ? t('nas.showHidden') : '',
        conn.follow_symlinks ? t('nas.followSymlinks') : '',
        testResults[conn.alias]?.status,
        conn.status,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedConnectionSearch))
    : connections;

  function openCreate() {
    setForm({ ...emptyForm });
    setEditingAlias(null);
    setShowModal(true);
  }

  function openEdit(conn: NasConnectionConfig) {
    setForm({
      ...conn,
      base_path: conn.base_path ?? '',
      max_download_bytes: conn.max_download_bytes ?? null,
    });
    setEditingAlias(conn.alias);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingAlias(null);
    setForm({ ...emptyForm });
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (editingAlias) {
      const data: Partial<NasConnectionConfig> = {
        base_path: form.base_path,
        max_download_bytes: form.max_download_bytes ?? null,
        show_hidden: form.show_hidden,
        follow_symlinks: form.follow_symlinks,
      };
      updateMutation.mutate({ alias: editingAlias, data });
    } else {
      createMutation.mutate(form);
    }
  }

  function handleDelete(alias: string) {
    if (window.confirm(t('nas.deleteConfirm', { alias }))) {
      deleteMutation.mutate(alias);
    }
  }

  function handleTest(alias: string) {
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[alias];
      return next;
    });
    testMutation.mutate(alias);
  }

  function updateField<K extends keyof NasConnectionConfig>(key: K, value: NasConnectionConfig[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <h1>{t('nas.title')}</h1>
          <p className="page-subtitle">{t('nas.subtitle')}</p>
        </div>
        <div className="page-header__actions connections-header-actions">
          {connections.length > 0 && (
            <input
              className="connection-search-input"
              type="search"
              value={connectionSearch}
              onChange={(event) => setConnectionSearch(event.target.value)}
              placeholder={t('nas.connectionSearchPlaceholder')}
              aria-label={t('nas.connectionSearchPlaceholder')}
            />
          )}
          {canWrite && (
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              {t('nas.addConnection')}
            </button>
          )}
        </div>
      </div>

      {connQuery.isLoading && <div className="loading-message" role="status">{t('nas.loadingConnections')}</div>}
      {connQuery.isError && <div className="error-banner" role="alert">{t('nas.loadFailed')}</div>}

      {connections.length > 0 && filteredConnections.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('nas.alias')}</th>
                <th scope="col">{t('nas.basePath')}</th>
                <th scope="col">{t('nas.showHidden')}</th>
                <th scope="col">{t('nas.followSymlinks')}</th>
                <th scope="col">{t('common.status')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredConnections.map((conn) => {
                const testResult = testResults[conn.alias];
                const isTesting = testMutation.isPending && testMutation.variables === conn.alias;
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === conn.alias;
                return (
                  <tr key={conn.alias}>
                    <td className="cell-alias">{conn.alias}</td>
                    <td className="mono">{conn.base_path}</td>
                    <td>{conn.show_hidden ? '✓' : '—'}</td>
                    <td>{conn.follow_symlinks ? '✓' : '—'}</td>
                    <td>
                      {testResult ? (
                        <span className={`badge ${testResult.status === 'error' ? 'badge-error' : 'badge-ok'}`}>
                          {testResult.status === 'error' ? t('common.error') : t('common.ok')}
                        </span>
                      ) : (
                        <span className={`badge ${conn.status === 'registered' ? 'badge-unknown' : 'badge-error'}`}>
                          {conn.status === 'registered' ? '--' : conn.status}
                        </span>
                      )}
                    </td>
                    <td>
                      <div className="action-buttons">
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('nas.testConnection', { alias: conn.alias })}
                          onClick={() => handleTest(conn.alias)}
                          disabled={testMutation.isPending}
                          aria-busy={isTesting}
                        >
                          {isTesting ? t('common.testing') : t('common.test')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          aria-label={t('nas.browseConnection', { alias: conn.alias })}
                          onClick={() => navigate(`/nas/browse/${encodeURIComponent(conn.alias)}`)}
                        >
                          {t('nas.browse')}
                        </button>
                        {canWrite && (
                          <>
                            <button
                              type="button"
                              className="btn btn-sm btn-secondary"
                              aria-label={t('nas.editConnection', { alias: conn.alias })}
                              onClick={() => openEdit(conn)}
                            >
                              {t('common.edit')}
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              aria-label={t('nas.deleteConnection', { alias: conn.alias })}
                              onClick={() => handleDelete(conn.alias)}
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

      {!connQuery.isLoading && connections.length > 0 && filteredConnections.length === 0 && !connQuery.isError && (
        <div className="empty-state">
          <h3>{t('nas.noSearchResults')}</h3>
          <p>{t('nas.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setConnectionSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!connQuery.isLoading && connections.length === 0 && !connQuery.isError && (
        <div className="empty-state">
          <h3>{t('nas.noConnections')}</h3>
          <p>{t('nas.noConnectionsDesc')}</p>
        </div>
      )}

      {canWrite && showModal && (
        <ResourceModal
          title={editingAlias ? t('nas.editAlias', { alias: editingAlias }) : t('nas.addTitle')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handleSubmit}>
            <div className="form-grid">
              <div className="form-group">
                <label htmlFor="nas-alias">{t('nas.alias')}</label>
                <input
                  id="nas-alias"
                  type="text"
                  value={form.alias}
                  onChange={(e) => updateField('alias', e.target.value)}
                  required
                  disabled={!!editingAlias}
                  placeholder="e.g., my-nas"
                  aria-label={t('nas.alias')}
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="nas-base-path">
                  {t('nas.basePath')}{' '}
                  <span id="nas-base-path-hint" className="hint">{t('nas.basePathHint')}</span>
                </label>
                <input
                  id="nas-base-path"
                  type="text"
                  value={form.base_path}
                  onChange={(e) => updateField('base_path', e.target.value)}
                  required
                  placeholder="/mnt/share/data"
                  aria-label={t('nas.basePath')}
                  aria-describedby="nas-base-path-hint"
                />
              </div>
              <div className="form-group">
                <label htmlFor="nas-max-download-bytes">
                  {t('nas.maxDownloadBytes')}{' '}
                  <span id="nas-max-download-bytes-hint" className="hint">{t('nas.maxDownloadBytesHint')}</span>
                </label>
                <input
                  id="nas-max-download-bytes"
                  type="number"
                  min={1}
                  value={form.max_download_bytes ?? ''}
                  onChange={(e) =>
                    updateField(
                      'max_download_bytes',
                      e.target.value === '' ? null : Number(e.target.value),
                    )
                  }
                  placeholder="524288000"
                  aria-label={t('nas.maxDownloadBytes')}
                  aria-describedby="nas-max-download-bytes-hint"
                />
              </div>
              <div className="form-group">
                <label>&nbsp;</label>
                <label className="method-check">
                  <input
                    type="checkbox"
                    checked={form.show_hidden}
                    onChange={(e) => updateField('show_hidden', e.target.checked)}
                  />
                  {t('nas.showHidden')}
                </label>
              </div>
              <div className="form-group">
                <label>&nbsp;</label>
                <label className="method-check">
                  <input
                    type="checkbox"
                    checked={form.follow_symlinks}
                    onChange={(e) => updateField('follow_symlinks', e.target.checked)}
                  />
                  {t('nas.followSymlinks')}
                </label>
              </div>
            </div>

            {(createMutation.isError || updateMutation.isError) && (
              <div className="form-error" role="alert">
                {(createMutation.error as Error)?.message ||
                  (updateMutation.error as Error)?.message ||
                  t('common.errorOccurred')}
              </div>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving} aria-busy={isSaving}>
                {isSaving ? t('common.saving') : editingAlias ? t('common.update') : t('common.create')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}

export default NasConnections;
