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
        {canWrite && (
          <button className="btn btn-primary" onClick={openCreate}>
            {t('nas.addConnection')}
          </button>
        )}
      </div>

      {connQuery.isLoading && <div className="loading-message">{t('nas.loadingConnections')}</div>}
      {connQuery.isError && <div className="error-banner">{t('nas.loadFailed')}</div>}

      {connections.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('nas.alias')}</th>
                <th>{t('nas.basePath')}</th>
                <th>{t('nas.showHidden')}</th>
                <th>{t('nas.followSymlinks')}</th>
                <th>{t('common.status')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((conn) => {
                const testResult = testResults[conn.alias];
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
                          className="btn btn-sm btn-secondary"
                          onClick={() => handleTest(conn.alias)}
                          disabled={testMutation.isPending}
                        >
                          {t('common.test')}
                        </button>
                        <button
                          className="btn btn-sm btn-primary"
                          onClick={() => navigate(`/nas/browse/${encodeURIComponent(conn.alias)}`)}
                        >
                          {t('nas.browse')}
                        </button>
                        {canWrite && (
                          <>
                            <button
                              className="btn btn-sm btn-secondary"
                              onClick={() => openEdit(conn)}
                            >
                              {t('common.edit')}
                            </button>
                            <button
                              className="btn btn-sm btn-danger"
                              onClick={() => handleDelete(conn.alias)}
                              disabled={deleteMutation.isPending}
                            >
                              {t('common.delete')}
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
                <label>{t('nas.alias')}</label>
                <input
                  type="text"
                  value={form.alias}
                  onChange={(e) => updateField('alias', e.target.value)}
                  required
                  disabled={!!editingAlias}
                  placeholder="e.g., my-nas"
                />
              </div>
              <div className="form-group form-group--full">
                <label>{t('nas.basePath')} <span className="hint">{t('nas.basePathHint')}</span></label>
                <input
                  type="text"
                  value={form.base_path}
                  onChange={(e) => updateField('base_path', e.target.value)}
                  required
                  placeholder="/mnt/share/data"
                />
              </div>
              <div className="form-group">
                <label>{t('nas.maxDownloadBytes')} <span className="hint">{t('nas.maxDownloadBytesHint')}</span></label>
                <input
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
              <div className="form-error">
                {(createMutation.error as Error)?.message ||
                  (updateMutation.error as Error)?.message ||
                  t('common.errorOccurred')}
              </div>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving}>
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
