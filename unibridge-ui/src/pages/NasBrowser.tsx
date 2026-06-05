import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getNasEntries,
  downloadNasEntry,
  getNasEntryMetadata,
  type NasEntry,
  type NasEntryMetadata,
} from '../api/client';
import { useToast } from '../components/useToast';
import ResourceModal from '../components/ResourceModal';
import { formatKST } from '../utils/time';
import './NasBrowser.css';

function formatBytes(bytes: number | null): string {
  if (bytes === null) return '—';
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function entryLabel(entry: NasEntry): string {
  return entry.name;
}

interface NasApiExample {
  labelKey: string;
  curl: string;
}

function getExampleFilePath(currentPath: string, files: NasEntry[]): string {
  if (files.length > 0) {
    return files[0].path;
  }
  const normalizedPath = currentPath.replace(/^\/+|\/+$/g, '');
  return normalizedPath ? `${normalizedPath}/example.csv` : 'example.csv';
}

function buildNasApiUrl(alias: string, endpoint: string, params: Record<string, string | number>): string {
  const url = new URL(`/api/nas/${encodeURIComponent(alias)}/${endpoint}`, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    url.searchParams.set(key, String(value));
  });
  return url.toString();
}

function buildNasCurl(url: string): string {
  return `curl -k \\\n  -H 'apikey: <YOUR_API_KEY>' \\\n  '${url}'`;
}

function buildNasApiExamples(alias: string, currentPath: string, files: NasEntry[]): NasApiExample[] {
  const filePath = getExampleFilePath(currentPath, files);
  return [
    {
      labelKey: 'nas.listEntriesExample',
      curl: buildNasCurl(buildNasApiUrl(alias, 'entries', { path: currentPath, limit: 100 })),
    },
    {
      labelKey: 'nas.metadataExample',
      curl: buildNasCurl(buildNasApiUrl(alias, 'metadata', { path: filePath })),
    },
    {
      labelKey: 'nas.downloadExample',
      curl: buildNasCurl(buildNasApiUrl(alias, 'download', { path: filePath })),
    },
  ];
}

function NasBrowser() {
  const { t } = useTranslation();
  const { alias } = useParams<{ alias: string }>();
  const navigate = useNavigate();
  const { addToast } = useToast();

  const [path, setPath] = useState('');
  const [folders, setFolders] = useState<NasEntry[]>([]);
  const [files, setFiles] = useState<NasEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errored, setErrored] = useState(false);
  const [metadataModal, setMetadataModal] = useState<NasEntryMetadata | null>(null);
  const [apiExamplesOpen, setApiExamplesOpen] = useState(false);
  const [apiExamplesCopied, setApiExamplesCopied] = useState(false);

  const fetchEntries = useCallback(
    async (pth: string, cursor?: string | null) => {
      if (!alias) return;
      setLoading(true);
      try {
        const resp = await getNasEntries(alias, {
          path: pth,
          offset: cursor ? Number(cursor) : 0,
        });
        if (cursor) {
          setFolders((prev) => [...prev, ...resp.folders]);
          setFiles((prev) => [...prev, ...resp.files]);
        } else {
          setFolders(resp.folders);
          setFiles(resp.files);
        }
        setNextCursor(resp.next_cursor ?? null);
        setHasMore(resp.has_more);
        setErrored(false);
      } catch {
        setErrored(true);
        addToast({ type: 'error', title: t('nas.loadFailed') });
      } finally {
        setLoading(false);
      }
    },
    [alias, addToast, t],
  );

  useEffect(() => {
    setFolders([]);
    setFiles([]);
    setNextCursor(null);
    setHasMore(false);
    fetchEntries(path);
  }, [path, fetchEntries]);

  function navigateToFolder(folderPath: string) {
    setPath(folderPath);
  }

  function navigateUp() {
    const parts = path.replace(/\/$/, '').split('/').filter(Boolean);
    parts.pop();
    setPath(parts.join('/'));
  }

  const [downloadingPaths, setDownloadingPaths] = useState<Set<string>>(new Set());

  async function handleDownload(entryPath: string) {
    if (!alias) return;
    setDownloadingPaths((prev) => new Set(prev).add(entryPath));
    try {
      const { blob, filename } = await downloadNasEntry(alias, entryPath);
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
      addToast({ type: 'error', title: t('nas.downloadFailed') });
    } finally {
      setDownloadingPaths((prev) => {
        const next = new Set(prev);
        next.delete(entryPath);
        return next;
      });
    }
  }

  async function handleMetadata(entryPath: string) {
    if (!alias) return;
    try {
      const meta = await getNasEntryMetadata(alias, entryPath);
      setMetadataModal(meta);
    } catch {
      addToast({ type: 'error', title: t('nas.metadataFailed') });
    }
  }

  function openApiExamples() {
    setApiExamplesCopied(false);
    setApiExamplesOpen(true);
  }

  async function handleCopyApiExamples() {
    if (!alias) return;
    const examples = buildNasApiExamples(alias, path, files);
    try {
      await navigator.clipboard.writeText(
        examples.map((example) => `${t(example.labelKey)}\n${example.curl}`).join('\n\n'),
      );
      setApiExamplesCopied(true);
    } catch {
      addToast({ type: 'error', title: t('nas.copyFailed') });
    }
  }

  function loadMore() {
    if (nextCursor) {
      fetchEntries(path, nextCursor);
    }
  }

  // Build breadcrumb from path
  const breadcrumbs: { label: string; path: string }[] = [{ label: t('nas.root'), path: '' }];
  if (path) {
    const parts = path.replace(/\/$/, '').split('/').filter(Boolean);
    let acc = '';
    for (const part of parts) {
      acc = acc ? `${acc}/${part}` : part;
      breadcrumbs.push({ label: part, path: acc });
    }
  }

  const isEmpty = folders.length === 0 && files.length === 0 && !loading && !errored;
  const apiExamples = alias ? buildNasApiExamples(alias, path, files) : [];

  return (
    <div className="nas-browser">
      <div className="page-header page-header--with-actions">
        <div>
          <h1>{t('nas.browserTitle')} — {alias}</h1>
          <p className="page-subtitle">{t('nas.browserSubtitle')}</p>
        </div>
        <div className="page-header__actions">
          <button className="btn btn-secondary" onClick={openApiExamples}>
            {t('nas.apiExamples')}
          </button>
          <button className="btn btn-secondary" onClick={() => navigate('/nas')}>
            {t('nas.backToConnections')}
          </button>
        </div>
      </div>

      {errored && <div className="error-banner">{t('nas.loadFailed')}</div>}

      {/* Breadcrumbs */}
      <div className="nas-breadcrumbs">
        {breadcrumbs.map((bc, i) => (
          <span key={bc.path || 'root'}>
            {i > 0 && <span className="nas-breadcrumb-sep">/</span>}
            <button
              className={`nas-breadcrumb ${i === breadcrumbs.length - 1 ? 'nas-breadcrumb--active' : ''}`}
              onClick={() => setPath(bc.path)}
            >
              {bc.label}
            </button>
          </span>
        ))}
      </div>

      {/* Entry listing */}
      {loading && folders.length === 0 && files.length === 0 ? (
        <div className="loading-message">{t('nas.loadingEntries')}</div>
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('nas.fileName')}</th>
                <th>{t('nas.size')}</th>
                <th>{t('nas.lastModified')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {path && (
                <tr className="nas-row-folder" onClick={navigateUp} style={{ cursor: 'pointer' }}>
                  <td className="cell-alias">
                    <span className="nas-icon nas-icon-folder">..</span>
                  </td>
                  <td>—</td>
                  <td>—</td>
                  <td></td>
                </tr>
              )}
              {folders.map((f) => (
                <tr
                  key={f.path}
                  className="nas-row-folder"
                  onClick={() => navigateToFolder(f.path)}
                  style={{ cursor: 'pointer' }}
                >
                  <td className="cell-alias">
                    <span className="nas-icon nas-icon-folder">{entryLabel(f)}</span>
                  </td>
                  <td>—</td>
                  <td>{formatKST(f.modified_time)}</td>
                  <td></td>
                </tr>
              ))}
              {files.map((file) => (
                <tr key={file.path}>
                  <td className="cell-alias">
                    <span className="nas-icon nas-icon-file">{entryLabel(file)}</span>
                  </td>
                  <td className="mono">{formatBytes(file.size)}</td>
                  <td>{formatKST(file.modified_time)}</td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-primary"
                        onClick={(e) => { e.stopPropagation(); handleDownload(file.path); }}
                        disabled={downloadingPaths.has(file.path)}
                      >
                        {downloadingPaths.has(file.path) ? t('nas.downloading') : t('nas.download')}
                      </button>
                      <button
                        className="btn btn-sm btn-outline"
                        onClick={(e) => { e.stopPropagation(); handleMetadata(file.path); }}
                      >
                        {t('nas.metadata')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {isEmpty && (
            <div className="empty-state">
              <p>{t('nas.noEntries')}</p>
            </div>
          )}
        </div>
      )}

      {hasMore && (
        <div className="nas-load-more">
          <button className="btn btn-secondary" onClick={loadMore} disabled={loading}>
            {loading ? t('common.loading') : t('nas.loadMore')}
          </button>
        </div>
      )}

      {/* Metadata modal */}
      {metadataModal && (
        <ResourceModal
          title={t('nas.metadata')}
          onClose={() => setMetadataModal(null)}
          closeLabel={t('common.close')}
          className="modal--sm"
        >
          <div className="nas-metadata-body">
            <table className="nas-metadata-table">
              <tbody>
                <tr><td>{t('nas.fileName')}</td><td className="mono">{metadataModal.name}</td></tr>
                <tr><td>{t('nas.path')}</td><td className="mono">{metadataModal.path}</td></tr>
                <tr><td>{t('nas.size')}</td><td>{formatBytes(metadataModal.size)}</td></tr>
                <tr><td>{t('nas.contentType')}</td><td>{metadataModal.content_type ?? '—'}</td></tr>
                <tr><td>{t('nas.lastModified')}</td><td>{formatKST(metadataModal.modified_time)}</td></tr>
              </tbody>
            </table>
          </div>
        </ResourceModal>
      )}

      {apiExamplesOpen && (
        <ResourceModal
          title={t('nas.apiExamplesTitle')}
          onClose={() => setApiExamplesOpen(false)}
          closeLabel={t('common.close')}
          className="nas-api-modal"
        >
          <div className="nas-api-examples">
            {apiExamples.map((example) => (
              <section key={example.labelKey} className="nas-api-example">
                <h3>{t(example.labelKey)}</h3>
                <pre>{example.curl}</pre>
              </section>
            ))}
            <div className="nas-api-example-actions">
              <button className="btn btn-sm btn-secondary" onClick={handleCopyApiExamples}>
                {apiExamplesCopied ? t('nas.apiExamplesCopied') : t('nas.apiExamplesCopy')}
              </button>
            </div>
          </div>
        </ResourceModal>
      )}
    </div>
  );
}

export default NasBrowser;
