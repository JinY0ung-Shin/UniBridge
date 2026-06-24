import { useState, useEffect, useCallback, useMemo, type KeyboardEvent } from 'react';
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
import ResourceModal from '../components/ResourceModal';
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

  const [selectedBucketChoice, setSelectedBucket] = useState<string>('');
  const [prefix, setPrefix] = useState('');
  const [folders, setFolders] = useState<S3Folder[]>([]);
  const [objects, setObjects] = useState<S3Object[]>([]);
  const [continuationToken, setContinuationToken] = useState<string | null>(null);
  const [isTruncated, setIsTruncated] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [metadataModal, setMetadataModal] = useState<S3ObjectMetadata | null>(null);
  const [objectFilter, setObjectFilter] = useState('');

  const bucketsQuery = useQuery({
    queryKey: ['s3-buckets', alias],
    queryFn: () => getS3Buckets(alias!),
    enabled: !!alias,
  });

  const buckets: S3Bucket[] = bucketsQuery.data ?? [];
  const selectedBucket = buckets.some((bucket) => bucket.name === selectedBucketChoice)
    ? selectedBucketChoice
    : buckets[0]?.name ?? '';

  const fetchObjects = useCallback(async (bucket: string, pfx: string, token?: string | null) => {
    if (!alias || !bucket) return;
    setLoadingObjects(true);
    if (!token) {
      setFolders([]);
      setObjects([]);
      setContinuationToken(null);
      setIsTruncated(false);
    }
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
      const handle = window.setTimeout(() => {
        void fetchObjects(selectedBucket, prefix);
      }, 0);
      return () => window.clearTimeout(handle);
    }
    return undefined;
  }, [selectedBucket, prefix, fetchObjects]);

  function navigateToFolder(folderPrefix: string) {
    setPrefix(folderPrefix);
    setObjectFilter('');
  }

  function navigateUp() {
    const parts = prefix.replace(/\/$/, '').split('/');
    parts.pop();
    setPrefix(parts.length > 0 ? parts.join('/') + '/' : '');
    setObjectFilter('');
  }

  function handleFolderRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, action: () => void) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    action();
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

  const objectRows = useMemo(
    () => objects.filter((o) => o.key !== prefix),
    [objects, prefix],
  );
  const filterText = objectFilter.trim().toLowerCase();
  const filteringObjects = filterText.length > 0;
  const visibleFolders = useMemo(
    () => folders.filter((folder) => {
      if (!filterText) return true;
      return folder.prefix.replace(prefix, '').replace(/\/$/, '').toLowerCase().includes(filterText);
    }),
    [folders, filterText, prefix],
  );
  const visibleObjects = useMemo(
    () => objectRows.filter((object) => {
      if (!filterText) return true;
      return object.key.replace(prefix, '').toLowerCase().includes(filterText);
    }),
    [objectRows, filterText, prefix],
  );
  const loadedEntryCount = folders.length + objectRows.length;
  const visibleEntryCount = visibleFolders.length + visibleObjects.length;

  return (
    <div className="s3-browser">
      <div className="page-header">
        <div>
          <h1>{t('s3.browserTitle')} — {alias}</h1>
          <p className="page-subtitle">{t('s3.browserSubtitle')}</p>
        </div>
        <button type="button" className="btn btn-secondary" onClick={() => navigate('/s3')}>
          {t('s3.backToConnections')}
        </button>
      </div>

      {/* Bucket selector */}
      <div className="s3-toolbar">
        <div className="s3-bucket-select">
          <label htmlFor="s3-bucket-select">{t('s3.selectBucket')}</label>
          {bucketsQuery.isLoading ? (
            <span className="loading-message" role="status">{t('s3.loadingBuckets')}</span>
          ) : (
            <select
              id="s3-bucket-select"
              value={selectedBucket}
              onChange={(e) => {
                setSelectedBucket(e.target.value);
                setPrefix('');
                setObjectFilter('');
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
        <div className="error-banner" role="alert">{t('s3.loadFailed')}</div>
      )}

      {!bucketsQuery.isLoading && buckets.length === 0 && !bucketsQuery.isError && (
        <div className="empty-state">
          <h3>{t('s3.noBuckets')}</h3>
        </div>
      )}

      {selectedBucket && (
        <>
          {/* Breadcrumbs */}
          <nav className="s3-breadcrumbs" aria-label={t('s3.breadcrumbs')}>
            {breadcrumbs.map((bc, i) => (
              <span key={bc.prefix}>
                {i > 0 && <span className="s3-breadcrumb-sep" aria-hidden="true">/</span>}
                <button
                  type="button"
                  className={`s3-breadcrumb ${i === breadcrumbs.length - 1 ? 's3-breadcrumb--active' : ''}`}
                  aria-current={i === breadcrumbs.length - 1 ? 'page' : undefined}
                  onClick={() => {
                    setPrefix(bc.prefix);
                    setObjectFilter('');
                  }}
                >
                  {bc.label}
                </button>
              </span>
            ))}
          </nav>

          <div className="s3-list-toolbar">
            <div className="s3-object-filter">
              <input
                type="search"
                className="s3-object-filter-input"
                placeholder={t('s3.objectFilterPlaceholder')}
                aria-label={t('s3.objectFilterPlaceholder')}
                value={objectFilter}
                onChange={(e) => setObjectFilter(e.target.value)}
              />
              {objectFilter && (
                <button
                  type="button"
                  className="s3-object-filter-clear"
                  aria-label={t('s3.clearFilter')}
                  title={t('s3.clearFilter')}
                  onClick={() => setObjectFilter('')}
                >
                  &times;
                </button>
              )}
            </div>
            {!loadingObjects && visibleEntryCount > 0 && (
              <span className="s3-entry-count">
                {!filteringObjects && isTruncated
                  ? t('s3.entryCountMore', { count: visibleEntryCount })
                  : t('s3.entryCount', { count: visibleEntryCount })}
              </span>
            )}
          </div>

          {/* Object listing */}
          {loadingObjects && loadedEntryCount === 0 ? (
            <div className="loading-message" role="status">{t('s3.loadingObjects')}</div>
          ) : (
            <div className="table-container">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">{t('s3.fileName')}</th>
                    <th scope="col">{t('s3.size')}</th>
                    <th scope="col">{t('s3.lastModified')}</th>
                    <th scope="col">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {prefix && !filteringObjects && (
                    <tr
                      className="s3-row-folder"
                      role="button"
                      tabIndex={0}
                      aria-label={t('s3.parentFolder')}
                      onClick={navigateUp}
                      onKeyDown={(event) => handleFolderRowKeyDown(event, navigateUp)}
                    >
                      <td className="cell-alias">
                        <span className="s3-icon s3-icon-folder">..</span>
                      </td>
                      <td>—</td>
                      <td>—</td>
                      <td></td>
                    </tr>
                  )}
                  {visibleFolders.map((f) => {
                    const folderName = f.prefix.replace(prefix, '').replace(/\/$/, '');
                    return (
                      <tr
                        key={f.prefix}
                        className="s3-row-folder"
                        role="button"
                        tabIndex={0}
                        aria-label={t('s3.openFolder', { name: folderName })}
                        onClick={() => navigateToFolder(f.prefix)}
                        onKeyDown={(event) => handleFolderRowKeyDown(event, () => navigateToFolder(f.prefix))}
                      >
                        <td className="cell-alias">
                          <span className="s3-icon s3-icon-folder">
                            {folderName}
                          </span>
                        </td>
                        <td>—</td>
                        <td>—</td>
                        <td></td>
                      </tr>
                    );
                  })}
                  {visibleObjects.map((obj) => {
                    const objectName = obj.key.replace(prefix, '');
                    const isDownloading = downloadingKeys.has(obj.key);
                    return (
                    <tr key={obj.key}>
                      <td className="cell-alias">
                        <span className="s3-icon s3-icon-file">
                          {objectName}
                        </span>
                      </td>
                      <td className="mono">{formatBytes(obj.size)}</td>
                      <td>{formatKST(obj.last_modified)}</td>
                      <td>
                        <div className="action-buttons">
                          <button
                            type="button"
                            className="btn btn-sm btn-primary"
                            onClick={(e) => { e.stopPropagation(); handleDownload(obj.key); }}
                            disabled={isDownloading}
                            aria-busy={isDownloading}
                            aria-label={isDownloading
                              ? t('s3.downloadingFile', { name: objectName })
                              : t('s3.downloadFile', { name: objectName })}
                            title={isDownloading
                              ? t('s3.downloadingFile', { name: objectName })
                              : t('s3.downloadFile', { name: objectName })}
                          >
                            {isDownloading ? t('s3.downloading') : t('s3.download')}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-outline"
                            onClick={(e) => { e.stopPropagation(); handleMetadata(obj.key); }}
                            aria-label={t('s3.metadataFile', { name: objectName })}
                            title={t('s3.metadataFile', { name: objectName })}
                          >
                            {t('s3.metadata')}
                          </button>
                        </div>
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>

              {visibleEntryCount === 0 && !loadingObjects && (
                <div className="empty-state">
                  <p>{filteringObjects ? t('s3.noFilterResults') : t('s3.noObjects')}</p>
                </div>
              )}
            </div>
          )}

          {isTruncated && (
            <div className="s3-load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={loadMore}
                disabled={loadingObjects}
                aria-busy={loadingObjects}
              >
                {loadingObjects ? t('common.loading') : t('s3.loadMore')}
              </button>
            </div>
          )}
        </>
      )}

      {/* Metadata modal */}
      {metadataModal && (
        <ResourceModal
          title={t('s3.metadata')}
          onClose={() => setMetadataModal(null)}
          closeLabel={t('common.close')}
          className="modal--sm"
        >
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
        </ResourceModal>
      )}
    </div>
  );
}

export default S3Browser;
