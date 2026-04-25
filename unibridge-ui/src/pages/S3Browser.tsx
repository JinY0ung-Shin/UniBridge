import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getS3Buckets,
  getS3Objects,
  downloadS3Object,
  getS3ObjectMetadata,
  type S3Bucket,
  type S3Object,
  type S3Folder,
  type S3ObjectMetadata,
} from '../api/client';
import { useToast } from '../components/useToast';
import { formatKST } from '../utils/time';
import './S3Browser.css';

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function S3Browser() {
  const { t } = useTranslation();
  const { alias } = useParams<{ alias: string }>();
  const navigate = useNavigate();
  const { addToast } = useToast();

  const [selectedBucket, setSelectedBucket] = useState<string>('');
  const [prefix, setPrefix] = useState('');
  const [folders, setFolders] = useState<S3Folder[]>([]);
  const [objects, setObjects] = useState<S3Object[]>([]);
  const [continuationToken, setContinuationToken] = useState<string | null>(null);
  const [isTruncated, setIsTruncated] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [metadataModal, setMetadataModal] = useState<S3ObjectMetadata | null>(null);

  const bucketsQuery = useQuery({
    queryKey: ['s3-buckets', alias],
    queryFn: () => getS3Buckets(alias!),
    enabled: !!alias,
  });

  const buckets: S3Bucket[] = bucketsQuery.data ?? [];

  // Auto-select first bucket when loaded
  useEffect(() => {
    if (bucketsQuery.data && bucketsQuery.data.length > 0 && !selectedBucket) {
      setSelectedBucket(bucketsQuery.data[0].name);
    }
  }, [bucketsQuery.data, selectedBucket]);

  const fetchObjects = useCallback(async (bucket: string, pfx: string, token?: string | null) => {
    if (!alias || !bucket) return;
    setLoadingObjects(true);
    try {
      const resp = await getS3Objects(alias, {
        bucket,
        prefix: pfx,
        continuation_token: token || undefined,
      });
      if (token) {
        setFolders((prev) => [...prev, ...resp.folders]);
        setObjects((prev) => [...prev, ...resp.objects]);
      } else {
        setFolders(resp.folders);
        setObjects(resp.objects);
      }
      setContinuationToken(resp.next_continuation_token ?? null);
      setIsTruncated(resp.is_truncated);
    } catch {
      addToast({ type: 'error', title: t('s3.loadFailed') });
    } finally {
      setLoadingObjects(false);
    }
  }, [alias, addToast, t]);

  useEffect(() => {
    if (selectedBucket) {
      setFolders([]);
      setObjects([]);
      setContinuationToken(null);
      fetchObjects(selectedBucket, prefix);
    }
  }, [selectedBucket, prefix, fetchObjects]);

  function navigateToFolder(folderPrefix: string) {
    setPrefix(folderPrefix);
  }

  function navigateUp() {
    const parts = prefix.replace(/\/$/, '').split('/');
    parts.pop();
    setPrefix(parts.length > 0 ? parts.join('/') + '/' : '');
  }

  const [downloadingKeys, setDownloadingKeys] = useState<Set<string>>(new Set());

  async function handleDownload(key: string) {
    if (!alias || !selectedBucket) return;
    setDownloadingKeys((prev) => new Set(prev).add(key));
    try {
      const { blob, filename } = await downloadS3Object(alias, {
        bucket: selectedBucket,
        key,
      });
      const url = window.URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } finally {
        setTimeout(() => window.URL.revokeObjectURL(url), 1000);
      }
    } catch {
      addToast({ type: 'error', title: t('s3.downloadFailed') });
    } finally {
      setDownloadingKeys((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  }

  async function handleMetadata(key: string) {
    if (!alias || !selectedBucket) return;
    try {
      const meta = await getS3ObjectMetadata(alias, { bucket: selectedBucket, key });
      setMetadataModal(meta);
    } catch {
      addToast({ type: 'error', title: t('s3.metadataFailed') });
    }
  }

  function loadMore() {
    if (continuationToken && selectedBucket) {
      fetchObjects(selectedBucket, prefix, continuationToken);
    }
  }

  // Build breadcrumb from prefix
  const breadcrumbs: { label: string; prefix: string }[] = [{ label: t('s3.root'), prefix: '' }];
  if (prefix) {
    const parts = prefix.replace(/\/$/, '').split('/');
    let acc = '';
    for (const part of parts) {
      acc += part + '/';
      breadcrumbs.push({ label: part, prefix: acc });
    }
  }

  return (
    <div className="s3-browser">
      <div className="page-header">
        <div>
          <h1>{t('s3.browserTitle')} — {alias}</h1>
          <p className="page-subtitle">{t('s3.browserSubtitle')}</p>
        </div>
        <button className="btn btn-secondary" onClick={() => navigate('/s3')}>
          {t('s3.backToConnections')}
        </button>
      </div>

      {/* Bucket selector */}
      <div className="s3-toolbar">
        <div className="s3-bucket-select">
          <label>{t('s3.selectBucket')}</label>
          {bucketsQuery.isLoading ? (
            <span className="loading-message">{t('s3.loadingBuckets')}</span>
          ) : (
            <select
              value={selectedBucket}
              onChange={(e) => {
                setSelectedBucket(e.target.value);
                setPrefix('');
              }}
            >
              <option value="">{t('s3.selectBucket')}</option>
              {buckets.map((b) => (
                <option key={b.name} value={b.name}>{b.name}</option>
              ))}
            </select>
          )}
        </div>
      </div>

      {bucketsQuery.isError && (
        <div className="error-banner">{t('s3.loadFailed')}</div>
      )}

      {!bucketsQuery.isLoading && buckets.length === 0 && !bucketsQuery.isError && (
        <div className="empty-state">
          <h3>{t('s3.noBuckets')}</h3>
        </div>
      )}

      {selectedBucket && (
        <>
          {/* Breadcrumbs */}
          <div className="s3-breadcrumbs">
            {breadcrumbs.map((bc, i) => (
              <span key={bc.prefix}>
                {i > 0 && <span className="s3-breadcrumb-sep">/</span>}
                <button
                  className={`s3-breadcrumb ${i === breadcrumbs.length - 1 ? 's3-breadcrumb--active' : ''}`}
                  onClick={() => setPrefix(bc.prefix)}
                >
                  {bc.label}
                </button>
              </span>
            ))}
          </div>

          {/* Object listing */}
          {loadingObjects && folders.length === 0 && objects.length === 0 ? (
            <div className="loading-message">{t('s3.loadingObjects')}</div>
          ) : (
            <div className="table-container">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('s3.fileName')}</th>
                    <th>{t('s3.size')}</th>
                    <th>{t('s3.lastModified')}</th>
                    <th>{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {prefix && (
                    <tr className="s3-row-folder" onClick={navigateUp} style={{ cursor: 'pointer' }}>
                      <td className="cell-alias">
                        <span className="s3-icon s3-icon-folder">..</span>
                      </td>
                      <td>—</td>
                      <td>—</td>
                      <td></td>
                    </tr>
                  )}
                  {folders.map((f) => (
                    <tr
                      key={f.prefix}
                      className="s3-row-folder"
                      onClick={() => navigateToFolder(f.prefix)}
                      style={{ cursor: 'pointer' }}
                    >
                      <td className="cell-alias">
                        <span className="s3-icon s3-icon-folder">
                          {f.prefix.replace(prefix, '').replace(/\/$/, '')}
                        </span>
                      </td>
                      <td>—</td>
                      <td>—</td>
                      <td></td>
                    </tr>
                  ))}
                  {objects
                    .filter((o) => o.key !== prefix)
                    .map((obj) => (
                    <tr key={obj.key}>
                      <td className="cell-alias">
                        <span className="s3-icon s3-icon-file">
                          {obj.key.replace(prefix, '')}
                        </span>
                      </td>
                      <td className="mono">{formatBytes(obj.size)}</td>
                      <td>{formatKST(obj.last_modified)}</td>
                      <td>
                        <div className="action-buttons">
                          <button
                            className="btn btn-sm btn-primary"
                            onClick={(e) => { e.stopPropagation(); handleDownload(obj.key); }}
                            disabled={downloadingKeys.has(obj.key)}
                          >
                            {downloadingKeys.has(obj.key) ? t('s3.downloading') : t('s3.download')}
                          </button>
                          <button
                            className="btn btn-sm btn-outline"
                            onClick={(e) => { e.stopPropagation(); handleMetadata(obj.key); }}
                          >
                            {t('s3.metadata')}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {folders.length === 0 && objects.filter((o) => o.key !== prefix).length === 0 && !loadingObjects && (
                <div className="empty-state">
                  <p>{t('s3.noObjects')}</p>
                </div>
              )}
            </div>
          )}

          {isTruncated && (
            <div className="s3-load-more">
              <button
                className="btn btn-secondary"
                onClick={loadMore}
                disabled={loadingObjects}
              >
                {loadingObjects ? t('common.loading') : t('s3.loadMore')}
              </button>
            </div>
          )}
        </>
      )}

      {/* Metadata modal */}
      {metadataModal && (
        <div className="modal-overlay" onClick={() => setMetadataModal(null)}>
          <div className="modal modal--sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{t('s3.metadata')}</h2>
              <button className="modal-close" onClick={() => setMetadataModal(null)}>&times;</button>
            </div>
            <div className="s3-metadata-body">
              <table className="s3-metadata-table">
                <tbody>
                  <tr><td>Key</td><td className="mono">{metadataModal.key}</td></tr>
                  <tr><td>Size</td><td>{formatBytes(metadataModal.size)}</td></tr>
                  <tr><td>Content-Type</td><td>{metadataModal.content_type}</td></tr>
                  <tr><td>Last Modified</td><td>{formatKST(metadataModal.last_modified)}</td></tr>
                  <tr><td>ETag</td><td className="mono">{metadataModal.etag}</td></tr>
                  {metadataModal.storage_class && (
                    <tr><td>Storage Class</td><td>{metadataModal.storage_class}</td></tr>
                  )}
                  {Object.entries(metadataModal.metadata).map(([k, v]) => (
                    <tr key={k}><td>x-amz-meta-{k}</td><td>{v}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default S3Browser;
