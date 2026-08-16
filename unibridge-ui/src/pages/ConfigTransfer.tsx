import { useRef, useState, type ChangeEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  exportConfig,
  importConfig,
  type ConfigExportDocument,
  type ConfigImportResult,
} from '../api/client';
import { usePermissions } from '../components/usePermissions';
import { useToast } from '../components/useToast';
import { formatKST } from '../utils/time';
import {
  documentSections,
  dryRunKey,
  exportFileName,
  parseExportDocument,
  previewJson,
  type ParseFailure,
} from '../utils/configTransfer';
import './ConfigTransfer.css';

/** Below this many rows the results table is short enough to scan unaided. */
const RESULT_FILTER_MIN_ROWS = 10;

const PARSE_ERROR_KEYS: Record<ParseFailure, string> = {
  malformedJson: 'configTransfer.errorMalformedJson',
  unsupportedVersion: 'configTransfer.errorUnsupportedVersion',
  missingSections: 'configTransfer.errorMissingSections',
};

const SECTION_LABEL_KEYS: Record<string, string> = {
  upstreams: 'configTransfer.sectionUpstreams',
  routes: 'configTransfer.sectionRoutes',
  db_connections: 'configTransfer.sectionDbConnections',
  s3_connections: 'configTransfer.sectionS3Connections',
  nas_connections: 'configTransfer.sectionNasConnections',
  roles: 'configTransfer.sectionRoles',
  db_permissions: 'configTransfer.sectionDbPermissions',
  query_templates: 'configTransfer.sectionQueryTemplates',
  monitored_hosts: 'configTransfer.sectionMonitoredHosts',
  monitored_services: 'configTransfer.sectionMonitoredServices',
  alert_settings: 'configTransfer.sectionAlertSettings',
  alert_channels: 'configTransfer.sectionAlertChannels',
  alert_recipients: 'configTransfer.sectionAlertRecipients',
  system_settings: 'configTransfer.sectionSystemSettings',
};

const ACTION_LABEL_KEYS: Record<string, string> = {
  create: 'configTransfer.actionCreate',
  update: 'configTransfer.actionUpdate',
  skip: 'configTransfer.actionSkip',
  error: 'configTransfer.actionError',
};

/** Human label for a section/action; anything the backend adds later renders as-is. */
function labelFor(t: (key: string) => string, map: Record<string, string>, value: string): string {
  const key = map[value];
  return key ? t(key) : value;
}

interface LoadedFile {
  doc: ConfigExportDocument;
  fileName: string;
  /** Identity of this upload — a re-read of the same name is a different file. */
  token: string;
}

function ConfigTransfer() {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const { permissions } = usePermissions();
  const canRead = permissions.includes('admin.config.read');
  const canWrite = permissions.includes('admin.config.write');

  const fileSeq = useRef(0);
  /** Key the in-flight dry-run was submitted under, so a file or selection
   *  change during the request cannot unlock Apply for results it never covered. */
  const submittedKey = useRef<string | null>(null);
  const [loaded, setLoaded] = useState<LoadedFile | null>(null);
  const [parseError, setParseError] = useState<ParseFailure | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [expandedSection, setExpandedSection] = useState<string | null>(null);
  const [results, setResults] = useState<ConfigImportResult | null>(null);
  const [resultFilter, setResultFilter] = useState('');
  /** Dry-run identity the on-screen results belong to; null locks Apply. */
  const [previewedKey, setPreviewedKey] = useState<string | null>(null);

  const sections = loaded ? documentSections(loaded.doc) : [];
  const currentKey = loaded ? dryRunKey(loaded.token, selected) : null;
  const applyReady = previewedKey !== null && previewedKey === currentKey;

  function clearResults() {
    setResults(null);
    setResultFilter('');
    setPreviewedKey(null);
  }

  const exportMut = useMutation({
    mutationFn: exportConfig,
    onSuccess: (doc) => {
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = exportFileName();
      a.click();
      URL.revokeObjectURL(url);
    },
  });

  const dryRunMut = useMutation({
    mutationFn: importConfig,
    onSuccess: (res) => {
      setResults(res);
      setResultFilter('');
      setPreviewedKey(submittedKey.current);
    },
  });

  const applyMut = useMutation({
    mutationFn: importConfig,
    onSuccess: (res) => {
      setResults(res);
      setResultFilter('');
      // The server state just moved, so the preview no longer describes it:
      // a second apply needs its own dry-run.
      setPreviewedKey(null);
      if (res.summary.error > 0) {
        addToast({
          type: 'error',
          title: t('configTransfer.applyPartialFailed', { count: res.summary.error }),
        });
      } else {
        addToast({ type: 'success', title: t('configTransfer.applySuccess') });
      }
    },
    onError: () => {
      addToast({ type: 'error', title: t('configTransfer.applyFailed') });
    },
  });

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const file = input.files?.[0];
    // Clear the input so re-picking the same path re-reads an edited file.
    input.value = '';
    if (!file) return;

    clearResults();
    dryRunMut.reset();
    applyMut.reset();
    setExpandedSection(null);

    const parsed = parseExportDocument(await file.text());
    if (!parsed.ok) {
      setLoaded(null);
      setSelected([]);
      setParseError(parsed.reason);
      return;
    }
    fileSeq.current += 1;
    setParseError(null);
    setLoaded({ doc: parsed.doc, fileName: file.name, token: `${file.name}#${fileSeq.current}` });
    setSelected(documentSections(parsed.doc).map((s) => s.name));
  }

  function toggleSection(name: string) {
    setSelected((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));
  }

  function handleDryRun() {
    if (!loaded || selected.length === 0) return;
    submittedKey.current = currentKey;
    dryRunMut.mutate({ dry_run: true, sections: selected, data: loaded.doc });
  }

  function handleApply() {
    if (!loaded || !applyReady) return;
    if (!window.confirm(t('configTransfer.applyConfirm', { count: selected.length }))) return;
    applyMut.mutate({ dry_run: false, sections: selected, data: loaded.doc });
  }

  /** No picking a new file or selection while a request is deciding on the old one. */
  const busy = dryRunMut.isPending || applyMut.isPending;
  const rows = results?.results ?? [];
  const resultSections = [...new Set(rows.map((r) => r.section))];
  const visibleRows = resultFilter ? rows.filter((r) => r.section === resultFilter) : rows;
  const showResultFilter = rows.length > RESULT_FILTER_MIN_ROWS && resultSections.length > 1;

  return (
    <div className="config-transfer">
      <div className="page-header">
        <h1>{t('configTransfer.title')}</h1>
        <p className="page-subtitle">{t('configTransfer.subtitle')}</p>
      </div>

      <section className="settings-card">
        <h2>{t('configTransfer.exportTitle')}</h2>
        <p className="config-transfer__hint">{t('configTransfer.exportHint')}</p>
        {canRead ? (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => exportMut.mutate()}
            disabled={exportMut.isPending}
            aria-busy={exportMut.isPending}
          >
            {exportMut.isPending ? t('configTransfer.exporting') : t('configTransfer.exportButton')}
          </button>
        ) : (
          <div className="settings-readonly-notice">{t('configTransfer.exportNoPermission')}</div>
        )}
        {exportMut.isError && (
          <div className="error-banner" role="alert">{t('configTransfer.exportFailed')}</div>
        )}
      </section>

      <section className="settings-card">
        <h2>{t('configTransfer.importTitle')}</h2>
        {!canWrite ? (
          <div className="settings-readonly-notice">{t('configTransfer.importReadOnly')}</div>
        ) : (
          <>
            <p className="config-transfer__hint">{t('configTransfer.importHint')}</p>

            <div className="form-group">
              <label htmlFor="config-import-file">{t('configTransfer.fileLabel')}</label>
              <input
                id="config-import-file"
                type="file"
                accept=".json,application/json"
                onChange={handleFileChange}
                disabled={busy}
                aria-describedby="config-import-file-hint"
              />
              <span id="config-import-file-hint" className="form-hint">
                {t('configTransfer.fileHint')}
              </span>
            </div>

            {parseError && (
              <div className="error-banner" role="alert">{t(PARSE_ERROR_KEYS[parseError])}</div>
            )}

            {loaded && (
              <>
                <p className="config-transfer__file">
                  {t('configTransfer.loadedFile', {
                    name: loaded.fileName,
                    date: formatKST(loaded.doc.exported_at),
                  })}
                </p>

                {sections.length === 0 ? (
                  <div className="empty-state">
                    <h3>{t('configTransfer.noSections')}</h3>
                    <p>{t('configTransfer.noSectionsDesc')}</p>
                  </div>
                ) : (
                  <fieldset className="config-sections">
                    <legend>{t('configTransfer.sectionsLegend')}</legend>
                    <div className="config-sections__bulk">
                      <button
                        type="button"
                        className="btn btn-sm btn-secondary"
                        onClick={() => setSelected(sections.map((s) => s.name))}
                        disabled={busy || selected.length === sections.length}
                      >
                        {t('configTransfer.selectAll')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-secondary"
                        onClick={() => setSelected([])}
                        disabled={busy || selected.length === 0}
                      >
                        {t('configTransfer.clearAll')}
                      </button>
                    </div>
                    {sections.map((section) => {
                      const previewId = `config-section-preview-${section.name}`;
                      const isExpanded = expandedSection === section.name;
                      const sectionLabel = labelFor(t, SECTION_LABEL_KEYS, section.name);
                      const previewLabel = isExpanded
                        ? t('configTransfer.hidePreview')
                        : t('configTransfer.preview');
                      return (
                        <div key={section.name} className="config-section">
                          <div className="config-section__row">
                            <label className="config-section__label">
                              <input
                                type="checkbox"
                                checked={selected.includes(section.name)}
                                onChange={() => toggleSection(section.name)}
                                disabled={busy}
                              />
                              <span>{sectionLabel}</span>
                            </label>
                            <span className="config-section__count">
                              {t('configTransfer.itemCount', { count: section.count })}
                            </span>
                            <button
                              type="button"
                              className="btn btn-sm btn-secondary"
                              aria-expanded={isExpanded}
                              aria-controls={previewId}
                              aria-label={`${previewLabel}: ${sectionLabel}`}
                              onClick={() => setExpandedSection(isExpanded ? null : section.name)}
                            >
                              {previewLabel}
                            </button>
                          </div>
                          {isExpanded && (
                            <pre id={previewId} className="config-section__preview">
                              {previewJson(loaded.doc.sections[section.name])}
                            </pre>
                          )}
                        </div>
                      );
                    })}
                  </fieldset>
                )}

                <div className="config-transfer__actions">
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={handleDryRun}
                    disabled={selected.length === 0 || dryRunMut.isPending || applyMut.isPending}
                    aria-busy={dryRunMut.isPending}
                  >
                    {dryRunMut.isPending ? t('configTransfer.dryRunning') : t('configTransfer.dryRunButton')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleApply}
                    disabled={!applyReady || applyMut.isPending || dryRunMut.isPending}
                    aria-busy={applyMut.isPending}
                  >
                    {applyMut.isPending ? t('configTransfer.applying') : t('configTransfer.applyButton')}
                  </button>
                  {!applyReady && (
                    <span className="config-transfer__lock-hint">{t('configTransfer.applyLocked')}</span>
                  )}
                </div>
              </>
            )}

            {dryRunMut.isError && (
              <div className="error-banner" role="alert">{t('configTransfer.dryRunFailed')}</div>
            )}

            {results && (
              <div className="config-results">
                <div className="config-results__head">
                  <div>
                    <h3>
                      {results.dry_run
                        ? t('configTransfer.dryRunResultTitle')
                        : t('configTransfer.applyResultTitle')}
                    </h3>
                    <p className="config-results__summary">
                      {t('configTransfer.summaryCounts', { ...results.summary })}
                    </p>
                  </div>
                  {showResultFilter && (
                    <div className="filter-field">
                      <label htmlFor="config-result-section-filter">
                        {t('configTransfer.resultSectionFilter')}
                      </label>
                      <select
                        id="config-result-section-filter"
                        className="filter-select"
                        value={resultFilter}
                        onChange={(e) => setResultFilter(e.target.value)}
                      >
                        <option value="">{t('configTransfer.allSections')}</option>
                        {resultSections.map((name) => (
                          <option key={name} value={name}>
                            {labelFor(t, SECTION_LABEL_KEYS, name)}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>

                {rows.length === 0 ? (
                  <div className="empty-state">
                    <h3>{t('configTransfer.noResults')}</h3>
                    <p>{t('configTransfer.noResultsDesc')}</p>
                  </div>
                ) : (
                  <div className="table-container">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th scope="col">{t('configTransfer.resultSection')}</th>
                          <th scope="col">{t('common.name')}</th>
                          <th scope="col">{t('configTransfer.resultAction')}</th>
                          <th scope="col">{t('configTransfer.resultReason')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleRows.map((row, index) => (
                          <tr key={`${row.section}:${row.name}:${index}`}>
                            <td>{labelFor(t, SECTION_LABEL_KEYS, row.section)}</td>
                            <td className="mono">{row.name}</td>
                            <td>
                              <span className={`badge badge-import-${row.action}`}>
                                {labelFor(t, ACTION_LABEL_KEYS, row.action)}
                              </span>
                            </td>
                            <td className="config-results__reason">{row.reason ?? '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

export default ConfigTransfer;
