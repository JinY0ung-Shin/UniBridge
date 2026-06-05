import { useState, useEffect, useCallback, useRef } from 'react';
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

/* ── In-browser preview ──────────────────────────────────────────────────── */

const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif', 'apng',
]);
const TEXT_EXTS = new Set([
  'txt', 'log', 'md', 'markdown', 'csv', 'tsv', 'json', 'yaml', 'yml', 'xml',
  'html', 'htm', 'css', 'js', 'jsx', 'ts', 'tsx', 'py', 'rb', 'go', 'rs',
  'java', 'c', 'h', 'cpp', 'hpp', 'sh', 'bash', 'zsh', 'ini', 'conf', 'cfg',
  'toml', 'env', 'sql', 'properties', 'gitignore',
]);

// Images are decoded fully into memory for the lightbox; text is read in full
// then truncated for display. Keep both well below the proxy download ceiling.
const IMAGE_PREVIEW_MAX_BYTES = 25 * 1024 * 1024;
const TEXT_PREVIEW_MAX_BYTES = 2 * 1024 * 1024;
const TEXT_PREVIEW_MAX_CHARS = 200_000;

type PreviewKind = 'image' | 'text';

function getExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
}

function getPreviewKind(name: string): PreviewKind | null {
  const ext = getExtension(name);
  if (IMAGE_EXTS.has(ext)) return 'image';
  if (TEXT_EXTS.has(ext)) return 'text';
  return null;
}

function iconClassFor(entry: NasEntry): string {
  if (entry.is_dir) return 'nas-icon-folder';
  const kind = getPreviewKind(entry.name);
  if (kind === 'image') return 'nas-icon-image';
  if (kind === 'text') return 'nas-icon-text';
  return 'nas-icon-file';
}

interface PreviewState {
  entry: NasEntry;
  kind: PreviewKind;
  status: 'loading' | 'ready' | 'error' | 'toolarge';
  url?: string;
  text?: string;
  truncated?: boolean;
}

/* ── Icons ───────────────────────────────────────────────────────────────── */

function DownloadIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M8 1a.75.75 0 0 1 .75.75v6.69l2.22-2.22a.75.75 0 1 1 1.06 1.06l-3.5 3.5a.75.75 0 0 1-1.06 0l-3.5-3.5a.75.75 0 1 1 1.06-1.06l2.22 2.22V1.75A.75.75 0 0 1 8 1ZM2.75 12a.75.75 0 0 1 .75.75v.75h9v-.75a.75.75 0 0 1 1.5 0v1.5a.75.75 0 0 1-.75.75h-10.5a.75.75 0 0 1-.75-.75v-1.5a.75.75 0 0 1 .75-.75Z"
      />
    </svg>
  );
}

function InfoIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm9-3.25a1 1 0 1 1-2 0 1 1 0 0 1 2 0ZM7.25 7a.75.75 0 0 0 0 1.5h.25v2.5a.75.75 0 0 0 1.5 0V7.75A.75.75 0 0 0 8.25 7h-1Z"
      />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M6.75 1.5a5.25 5.25 0 1 0 3.166 9.44l3.072 3.07a.75.75 0 1 0 1.06-1.06l-3.07-3.072A5.25 5.25 0 0 0 6.75 1.5ZM3 6.75a3.75 3.75 0 1 1 7.5 0 3.75 3.75 0 0 1-7.5 0Z"
      />
    </svg>
  );
}

/* ── API examples (unchanged behaviour) ──────────────────────────────────── */

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
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [folders, setFolders] = useState<NasEntry[]>([]);
  const [files, setFiles] = useState<NasEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errored, setErrored] = useState(false);
  const [metadataModal, setMetadataModal] = useState<NasEntryMetadata | null>(null);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [downloadingPaths, setDownloadingPaths] = useState<Set<string>>(new Set());
  const [apiExamplesOpen, setApiExamplesOpen] = useState(false);
  const [apiExamplesCopied, setApiExamplesCopied] = useState(false);

  // Monotonic id so out-of-order list responses can be discarded.
  const fetchReqId = useRef(0);

  // Debounce the search box so each keystroke does not fire a request.
  useEffect(() => {
    const handle = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const fetchEntries = useCallback(
    async (pth: string, query: string, cursor?: string | null) => {
      if (!alias) return;
      // Discard out-of-order responses: a slow earlier request must not clobber
      // a newer view (e.g. an unfiltered list landing after a search).
      const reqId = ++fetchReqId.current;
      setLoading(true);
      try {
        const resp = await getNasEntries(alias, {
          path: pth,
          offset: cursor ? Number(cursor) : 0,
          q: query || undefined,
        });
        if (fetchReqId.current !== reqId) return; // superseded
        if (cursor) {
          setFolders((prev) => [...prev, ...resp.folders]);
          setFiles((prev) => [...prev, ...resp.files]);
        } else {
          setFolders(resp.folders);
          setFiles(resp.files);
        }
        setNextCursor(resp.next_cursor ?? null);
        setHasMore(resp.has_more);
        setTruncated(resp.truncated ?? false);
        setErrored(false);
      } catch {
        if (fetchReqId.current !== reqId) return; // superseded
        setErrored(true);
        addToast({ type: 'error', title: t('nas.loadFailed') });
      } finally {
        if (fetchReqId.current === reqId) setLoading(false);
      }
    },
    [alias, addToast, t],
  );

  useEffect(() => {
    setFolders([]);
    setFiles([]);
    setNextCursor(null);
    setHasMore(false);
    setTruncated(false);
    fetchEntries(path, search);
  }, [path, search, fetchEntries]);

  // Changing directory clears any active filter — results were scoped to the
  // directory we are leaving.
  function goToPath(target: string) {
    setSearchInput('');
    setSearch('');
    setPath(target);
  }

  function navigateUp() {
    const parts = path.replace(/\/$/, '').split('/').filter(Boolean);
    parts.pop();
    goToPath(parts.join('/'));
  }

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

  // Guards against a slow preview fetch landing after the user opened another.
  const previewReqId = useRef(0);

  async function openPreview(entry: NasEntry) {
    if (!alias) return;
    const kind = getPreviewKind(entry.name);
    if (!kind) {
      handleDownload(entry.path);
      return;
    }
    const max = kind === 'image' ? IMAGE_PREVIEW_MAX_BYTES : TEXT_PREVIEW_MAX_BYTES;
    if (entry.size != null && entry.size > max) {
      setPreview({ entry, kind, status: 'toolarge' });
      return;
    }
    const reqId = ++previewReqId.current;
    setPreview({ entry, kind, status: 'loading' });
    try {
      const { blob } = await downloadNasEntry(alias, entry.path);
      if (previewReqId.current !== reqId) return; // superseded
      if (kind === 'image') {
        const url = URL.createObjectURL(blob);
        setPreview({ entry, kind, status: 'ready', url });
      } else {
        const raw = await blob.text();
        if (previewReqId.current !== reqId) return;
        const truncated = raw.length > TEXT_PREVIEW_MAX_CHARS;
        setPreview({
          entry,
          kind,
          status: 'ready',
          text: truncated ? raw.slice(0, TEXT_PREVIEW_MAX_CHARS) : raw,
          truncated,
        });
      }
    } catch {
      if (previewReqId.current === reqId) {
        setPreview({ entry, kind, status: 'error' });
      }
    }
  }

  function closePreview() {
    previewReqId.current += 1; // invalidate any in-flight fetch
    setPreview(null);
  }

  // Release the blob URL when the preview changes or the page unmounts.
  useEffect(() => {
    const url = preview?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [preview?.url]);

  function activateFile(entry: NasEntry) {
    if (getPreviewKind(entry.name)) {
      openPreview(entry);
    } else {
      handleDownload(entry.path);
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
      fetchEntries(path, search, nextCursor);
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

  const shownCount = folders.length + files.length;
  const searching = search.length > 0;
  const isEmpty = shownCount === 0 && !loading && !errored;
  const countHasMore = hasMore || truncated;
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
              onClick={() => goToPath(bc.path)}
            >
              {bc.label}
            </button>
          </span>
        ))}
      </div>

      {/* Toolbar: per-folder search + count */}
      <div className="nas-toolbar">
        <div className="nas-search">
          <span className="nas-search-icon"><SearchIcon /></span>
          <input
            type="text"
            className="nas-search-input"
            placeholder={t('nas.searchPlaceholder')}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            aria-label={t('nas.searchPlaceholder')}
          />
          {searchInput && (
            <button
              type="button"
              className="nas-search-clear"
              aria-label={t('nas.clearSearch')}
              title={t('nas.clearSearch')}
              onClick={() => setSearchInput('')}
            >
              &times;
            </button>
          )}
        </div>
        {!loading && !errored && shownCount > 0 && (
          <span className="nas-count">
            {countHasMore
              ? t('nas.entryCountMore', { count: shownCount })
              : t('nas.entryCount', { count: shownCount })}
          </span>
        )}
      </div>

      {truncated && (
        <div className="nas-notice" role="status">{t('nas.truncatedNotice')}</div>
      )}

      {/* Entry listing */}
      {loading && shownCount === 0 ? (
        <div className="loading-message">{t('nas.loadingEntries')}</div>
      ) : (
        <div className="table-container">
          <table className="data-table nas-table">
            <thead>
              <tr>
                <th>{t('nas.fileName')}</th>
                <th>{t('nas.size')}</th>
                <th>{t('nas.lastModified')}</th>
                <th className="nas-actions-col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {path && !searching && (
                <tr className="nas-row-folder" onClick={navigateUp} style={{ cursor: 'pointer' }}>
                  <td className="cell-alias">
                    <button
                      type="button"
                      className="nas-name-btn nas-icon nas-icon-folder nas-icon--up"
                      onClick={(e) => { e.stopPropagation(); navigateUp(); }}
                      aria-label={t('nas.parentDirectory')}
                      title={t('nas.parentDirectory')}
                    >
                      ..
                    </button>
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
                  onClick={() => goToPath(f.path)}
                  style={{ cursor: 'pointer' }}
                >
                  <td className="cell-alias">
                    <button
                      type="button"
                      className="nas-name-btn nas-icon nas-icon-folder"
                      onClick={(e) => { e.stopPropagation(); goToPath(f.path); }}
                      aria-label={`${t('nas.folder')}: ${f.name}`}
                    >
                      {f.name}
                    </button>
                  </td>
                  <td>—</td>
                  <td>{formatKST(f.modified_time)}</td>
                  <td></td>
                </tr>
              ))}
              {files.map((file) => {
                const previewable = getPreviewKind(file.name) !== null;
                const busy = downloadingPaths.has(file.path);
                return (
                  <tr key={file.path}>
                    <td className="cell-alias">
                      <button
                        type="button"
                        className={`nas-name-btn nas-icon ${iconClassFor(file)}`}
                        onClick={() => activateFile(file)}
                        title={previewable ? t('nas.preview') : t('nas.download')}
                        aria-label={`${previewable ? t('nas.preview') : t('nas.download')}: ${file.name}`}
                      >
                        {file.name}
                      </button>
                    </td>
                    <td className="mono">{formatBytes(file.size)}</td>
                    <td>{formatKST(file.modified_time)}</td>
                    <td>
                      <div className="action-buttons nas-actions">
                        <button
                          className="nas-action-btn"
                          onClick={(e) => { e.stopPropagation(); handleDownload(file.path); }}
                          disabled={busy}
                          aria-busy={busy}
                          aria-label={busy ? t('nas.downloading') : t('nas.download')}
                          title={busy ? t('nas.downloading') : t('nas.download')}
                        >
                          {busy ? <span className="nas-spinner" aria-hidden="true" /> : <DownloadIcon />}
                        </button>
                        <button
                          className="nas-action-btn"
                          onClick={(e) => { e.stopPropagation(); handleMetadata(file.path); }}
                          aria-label={t('nas.metadata')}
                          title={t('nas.metadata')}
                        >
                          <InfoIcon />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {isEmpty && (
            <div className="empty-state">
              <p>{searching ? t('nas.noResults') : t('nas.noEntries')}</p>
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

      {/* Preview lightbox */}
      {preview && (
        <ResourceModal
          title={preview.entry.name}
          onClose={closePreview}
          closeLabel={t('common.close')}
          className={`nas-preview-modal ${preview.kind === 'image' ? 'nas-preview-modal--image' : ''}`}
        >
          <div className="nas-preview-body">
            <div role="status" aria-live="polite" className="nas-preview-status">
              {preview.status === 'loading' && (
                <div className="loading-message">{t('nas.previewLoading')}</div>
              )}
              {preview.status === 'error' && (
                <div className="nas-preview-message">{t('nas.previewFailed')}</div>
              )}
              {preview.status === 'toolarge' && (
                <div className="nas-preview-message">{t('nas.previewTooLarge')}</div>
              )}
            </div>
            {preview.status === 'ready' && preview.kind === 'image' && preview.url && (
              <img
                className="nas-preview-image"
                src={preview.url}
                alt={preview.entry.name}
                onError={() => setPreview((p) => (p ? { ...p, status: 'error' } : p))}
              />
            )}
            {preview.status === 'ready' && preview.kind === 'text' && (
              <>
                <pre className="nas-preview-text">{preview.text}</pre>
                {preview.truncated && (
                  <p className="nas-preview-truncated">{t('nas.previewTruncated')}</p>
                )}
              </>
            )}
          </div>
          <div className="nas-preview-footer">
            <span className="nas-preview-size">{formatBytes(preview.entry.size)}</span>
            <button
              className="btn btn-sm btn-primary"
              onClick={() => handleDownload(preview.entry.path)}
              disabled={downloadingPaths.has(preview.entry.path)}
            >
              {downloadingPaths.has(preview.entry.path) ? t('nas.downloading') : t('nas.download')}
            </button>
          </div>
        </ResourceModal>
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
