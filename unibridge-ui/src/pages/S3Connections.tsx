import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getS3Connections,
  createS3Connection,
  updateS3Connection,
  deleteS3Connection,
  testS3Connection,
  type S3ConnectionConfig,
} from '../api/client';
import { useToast } from '../components/ToastContext';
import './Connections.css';

const emptyForm: S3ConnectionConfig = {
  alias: '',
  endpoint_url: '',
  region: '',
  access_key_id: '',
  secret_access_key: '',
  default_bucket: '',
  use_ssl: true,
};

function S3Connections() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [showModal, setShowModal] = useState(false);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [form, setForm] = useState<S3ConnectionConfig>({ ...emptyForm });
  const [testResults, setTestResults] = useState<Record<string, { status: string }>>({});

  const connQuery = useQuery({
    queryKey: ['s3-connections'],
    queryFn: getS3Connections,
  });

  const createMutation = useMutation({
    mutationFn: (data: S3ConnectionConfig) => createS3Connection(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['s3-connections'] });
      closeModal();
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ alias, data }: { alias: string; data: Partial<S3ConnectionConfig> }) =>
      updateS3Connection(alias, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['s3-connections'] });
      closeModal();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (alias: string) => deleteS3Connection(alias),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['s3-connections'] });
    },
    onError: (_err, alias) => {
      addToast({ type: 'error', title: `${alias} — ${t('s3.deleteFailed')}` });
    },
  });

  const testMutation = useMutation({
    mutationFn: (alias: string) => testS3Connection(alias),
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
      addToast({ type: 'error', title: `${alias} — ${t('s3.testFailed')}` });
    },
  });

  const [curlModal, setCurlModal] = useState<{ alias: string; curl: string } | null>(null);
  const [curlCopied, setCurlCopied] = useState(false);

  const connections = connQuery.data ?? [];

  function openCreate() {
    setForm({ ...emptyForm });
    setEditingAlias(null);
    setShowModal(true);
  }

  function openEdit(conn: S3ConnectionConfig) {
    setForm({
      ...conn,
      access_key_id: '',
      secret_access_key: '',
      endpoint_url: conn.endpoint_url ?? '',
      default_bucket: conn.default_bucket ?? '',
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
      const { secret_access_key, access_key_id, ...rest } = form;
      const data: Partial<S3ConnectionConfig> = { ...rest };
      if (access_key_id) data.access_key_id = access_key_id;
      if (secret_access_key) data.secret_access_key = secret_access_key;
      updateMutation.mutate({ alias: editingAlias, data });
    } else {
      createMutation.mutate(form);
    }
  }

  function handleDelete(alias: string) {
    if (window.confirm(t('s3.deleteConfirm', { alias }))) {
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

  function handleCurl(alias: string, defaultBucket: string | null | undefined) {
    const base = `${window.location.origin}/api/s3`;
    const bucket = defaultBucket || '<BUCKET>';
    const curl = [
      `# ${t('s3.browserSubtitle')}`,
      ``,
      `# 1. ${t('s3.loadingBuckets')}`,
      `curl -k -H 'apikey: <YOUR_API_KEY>' \\`,
      `  '${base}/${alias}/buckets'`,
      ``,
      `# 2. ${t('s3.loadingObjects')}`,
      `curl -k -H 'apikey: <YOUR_API_KEY>' \\`,
      `  '${base}/${alias}/objects?bucket=${bucket}&prefix=&delimiter=/'`,
      ``,
      `# 3. ${t('s3.metadata')}`,
      `curl -k -H 'apikey: <YOUR_API_KEY>' \\`,
      `  '${base}/${alias}/objects/metadata?bucket=${bucket}&key=<FILE_KEY>'`,
      ``,
      `# 4. ${t('s3.download')} (presigned URL)`,
      `curl -k -H 'apikey: <YOUR_API_KEY>' \\`,
      `  '${base}/${alias}/objects/presigned-url?bucket=${bucket}&key=<FILE_KEY>'`,
    ].join('\n');
    setCurlModal({ alias, curl });
    setCurlCopied(false);
  }

  function handleCurlCopy() {
    if (curlModal) {
      navigator.clipboard.writeText(curlModal.curl);
      setCurlCopied(true);
      setTimeout(() => setCurlCopied(false), 2000);
    }
  }

  function updateField<K extends keyof S3ConnectionConfig>(key: K, value: S3ConnectionConfig[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <h1>{t('s3.title')}</h1>
          <p className="page-subtitle">{t('s3.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>
          {t('s3.addConnection')}
        </button>
      </div>

      {connQuery.isLoading && <div className="loading-message">{t('s3.loadingConnections')}</div>}
      {connQuery.isError && <div className="error-banner">{t('s3.loadFailed')}</div>}

      {connections.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('s3.alias')}</th>
                <th>{t('s3.endpointUrl')}</th>
                <th>{t('s3.region')}</th>
                <th>{t('s3.defaultBucket')}</th>
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
                    <td className="mono">{conn.endpoint_url || 'AWS S3'}</td>
                    <td>{conn.region}</td>
                    <td>{conn.default_bucket || '—'}</td>
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
                          className="btn btn-sm btn-outline"
                          onClick={() => handleCurl(conn.alias, conn.default_bucket)}
                        >
                          cURL
                        </button>
                        <button
                          className="btn btn-sm btn-primary"
                          onClick={() => navigate(`/s3/browse/${encodeURIComponent(conn.alias)}`)}
                        >
                          {t('s3.browse')}
                        </button>
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
          <h3>{t('s3.noConnections')}</h3>
          <p>{t('s3.noConnectionsDesc')}</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingAlias ? t('s3.editAlias', { alias: editingAlias }) : t('s3.addTitle')}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group">
                  <label>{t('s3.alias')}</label>
                  <input
                    type="text"
                    value={form.alias}
                    onChange={(e) => updateField('alias', e.target.value)}
                    required
                    disabled={!!editingAlias}
                    placeholder="e.g., my-s3"
                  />
                </div>
                <div className="form-group">
                  <label>{t('s3.region')}</label>
                  <input
                    type="text"
                    value={form.region}
                    onChange={(e) => updateField('region', e.target.value)}
                    required
                    placeholder="us-east-1"
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>{t('s3.endpointUrl')} <span className="hint">{t('s3.endpointUrlHint')}</span></label>
                  <input
                    type="text"
                    value={form.endpoint_url ?? ''}
                    onChange={(e) => updateField('endpoint_url', e.target.value)}
                    placeholder="https://minio.example.com:9000"
                  />
                </div>
                <div className="form-group">
                  <label>
                    {t('s3.accessKeyId')}
                    {editingAlias && <span className="hint"> {t('s3.secretAccessKeyHint')}</span>}
                  </label>
                  <input
                    type="text"
                    value={form.access_key_id ?? ''}
                    onChange={(e) => updateField('access_key_id', e.target.value)}
                    required={!editingAlias}
                    placeholder={editingAlias ? (form.access_key_id_masked || 'AKIAIOSFODNN7EXAMPLE') : 'AKIAIOSFODNN7EXAMPLE'}
                  />
                </div>
                <div className="form-group">
                  <label>
                    {t('s3.secretAccessKey')}
                    {editingAlias && <span className="hint"> {t('s3.secretAccessKeyHint')}</span>}
                  </label>
                  <input
                    type="password"
                    value={form.secret_access_key ?? ''}
                    onChange={(e) => updateField('secret_access_key', e.target.value)}
                    placeholder="********"
                    required={!editingAlias}
                  />
                </div>
                <div className="form-group">
                  <label>{t('s3.defaultBucket')} <span className="hint">{t('s3.defaultBucketHint')}</span></label>
                  <input
                    type="text"
                    value={form.default_bucket ?? ''}
                    onChange={(e) => updateField('default_bucket', e.target.value)}
                    placeholder="my-bucket"
                  />
                </div>
                <div className="form-group">
                  <label>&nbsp;</label>
                  <label className="method-check">
                    <input
                      type="checkbox"
                      checked={form.use_ssl}
                      onChange={(e) => updateField('use_ssl', e.target.checked)}
                    />
                    {t('s3.useSsl')}
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
          </div>
        </div>
      )}

      {curlModal && (
        <div className="modal-overlay" onClick={() => setCurlModal(null)}>
          <div className="modal modal--sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>cURL — {curlModal.alias}</h2>
              <button className="modal-close" onClick={() => setCurlModal(null)}>&times;</button>
            </div>
            <div className="curl-block">
              <pre className="curl-code">{curlModal.curl}</pre>
              <button className="btn btn-sm btn-secondary curl-copy-btn" onClick={handleCurlCopy}>
                {curlCopied ? t('gatewayRoutes.curlCopied') : t('gatewayRoutes.curlCopy')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default S3Connections;
