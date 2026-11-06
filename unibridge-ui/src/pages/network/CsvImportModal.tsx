import { useId, useState, type ChangeEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  importNetworkCsv,
  type NetworkImportKind,
  type NetworkImportResult,
} from '../../api/network';
import ResourceModal from '../../components/ResourceModal';
import { CSV_COLUMNS, decodeCsvBytes, readFileBytes, sampleHeader, type CsvEncoding } from './csvText';
import { NETWORK_KEY, networkErrorMessage } from './networkUtils';

const ACTION_BADGE: Record<string, string> = {
  create: 'badge-ok',
  skip: 'badge-unknown',
  error: 'badge-error',
};

function ImportResultView({ result }: { result: NetworkImportResult }) {
  const { t } = useTranslation();
  return (
    <div className="network-import-result">
      <div className="network-import-counts" role="status">
        <span className="badge badge-ok">{t('network.import.countCreate', { count: result.created })}</span>
        <span className="badge badge-unknown">{t('network.import.countSkip', { count: result.skipped })}</span>
        <span className="badge badge-error">{t('network.import.countError', { count: result.errors })}</span>
      </div>
      {result.rows.length > 0 && (
        <div className="table-container network-import-table">
          <table className="data-table network-table" aria-label={t('network.import.rowsLabel')}>
            <thead>
              <tr>
                <th scope="col">{t('network.import.line')}</th>
                <th scope="col">{t('network.import.action')}</th>
                <th scope="col">{t('network.import.key')}</th>
                <th scope="col">{t('network.import.message')}</th>
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, i) => (
                <tr key={`${row.line}-${i}`}>
                  <td className="mono">{row.line}</td>
                  <td>
                    <span className={`badge ${ACTION_BADGE[row.action] ?? 'badge-unknown'}`}>
                      {t(`network.import.actions.${row.action}`, { defaultValue: row.action })}
                    </span>
                  </td>
                  <td className="mono">{row.key}</td>
                  <td>{row.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

interface CsvImportModalProps {
  kind: NetworkImportKind;
  onClose: () => void;
}

/** Two steps: a dry run shows what each line would do, then the same text is imported. */
export function CsvImportModal({ kind, onClose }: CsvImportModalProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const fileId = useId();
  const textId = useId();
  const [csv, setCsv] = useState('');
  const [file, setFile] = useState<{ name: string; encoding: CsvEncoding } | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [preview, setPreview] = useState<NetworkImportResult | null>(null);
  const [applied, setApplied] = useState<NetworkImportResult | null>(null);

  const run = useMutation({
    mutationFn: (dryRun: boolean) => importNetworkCsv({ kind, csv, dry_run: dryRun }),
    onSuccess: (result) => {
      if (result.dry_run) {
        setPreview(result);
      } else {
        setApplied(result);
        queryClient.invalidateQueries({ queryKey: NETWORK_KEY });
      }
    },
  });

  function changeText(value: string) {
    setCsv(value);
    // A preview only stands for the text it was made from.
    setPreview(null);
    run.reset();
  }

  async function pickFile(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0];
    if (!picked) return;
    setReadError(null);
    try {
      const decoded = decodeCsvBytes(await readFileBytes(picked));
      setFile({ name: picked.name, encoding: decoded.encoding });
      changeText(decoded.text);
    } catch {
      setFile(null);
      setReadError(t('network.import.readFailed'));
    }
  }

  const lang = i18n.resolvedLanguage === 'ko' ? 'ko' : 'en';
  const error = run.isError ? networkErrorMessage(run.error, t('network.import.failed')) : readError;
  const title = t('network.import.title', { kind: t(`network.import.kinds.${kind}`) });

  return (
    <ResourceModal title={title} onClose={onClose} closeLabel={t('common.close')} className="network-import-modal">
      <div className="network-import">
        {applied ? (
          <>
            <p className="network-import__done">{t('network.import.done', { count: applied.created })}</p>
            <ImportResultView result={applied} />
          </>
        ) : (
          <>
            <p className="page-subtitle">{t('network.import.hint')}</p>
            <div className="network-import__columns">
              <h3 className="network-subtitle">{t('network.import.columns')}</h3>
              <ul>
                {CSV_COLUMNS[kind].map((c) => (
                  <li key={c.field}>
                    <span className="mono">{c.field}</span>
                    {c.required && <span className="network-import__required">{t('network.import.required')}</span>}
                    <span className="network-sub"> — {[...c.en, ...c.ko].join(', ')}</span>
                  </li>
                ))}
              </ul>
              {kind === 'openings' && <p className="network-sub">{t('network.import.openingsHint')}</p>}
              <p className="network-sub">
                {t('network.import.sampleHeader')} <code>{sampleHeader(kind, lang)}</code>
              </p>
            </div>

            <div className="form-group">
              <label htmlFor={fileId}>{t('network.import.file')}</label>
              <input id={fileId} type="file" accept=".csv,.txt,text/csv,text/plain" onChange={pickFile} />
              {file && (
                <div className="network-sub">
                  {t('network.import.fileRead', { name: file.name, encoding: file.encoding.toUpperCase() })}
                </div>
              )}
            </div>
            <div className="form-group">
              <label htmlFor={textId}>{t('network.import.text')}</label>
              <textarea
                id={textId}
                className="network-import__text mono"
                rows={8}
                value={csv}
                onChange={(e) => changeText(e.target.value)}
                placeholder={sampleHeader(kind, lang)}
                spellCheck={false}
              />
            </div>

            {preview && (
              <section aria-label={t('network.import.previewTitle')}>
                <h3 className="network-subtitle">{t('network.import.previewTitle')}</h3>
                <ImportResultView result={preview} />
              </section>
            )}
          </>
        )}
        {error && <div className="form-error" role="alert">{error}</div>}
      </div>

      <div className="modal-actions">
        {applied ? (
          <button type="button" className="btn btn-primary" onClick={onClose}>{t('common.close')}</button>
        ) : (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose}>{t('common.cancel')}</button>
            <button
              type="button"
              className={preview ? 'btn btn-secondary' : 'btn btn-primary'}
              disabled={!csv.trim() || run.isPending}
              aria-busy={run.isPending && run.variables === true}
              onClick={() => run.mutate(true)}
            >
              {t('network.import.preview')}
            </button>
            {preview && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={preview.created === 0 || run.isPending}
                aria-busy={run.isPending && run.variables === false}
                onClick={() => run.mutate(false)}
              >
                {t('network.import.apply')}
              </button>
            )}
          </>
        )}
      </div>
    </ResourceModal>
  );
}

/** The toolbar button that opens the import modal (callers show it to network.write holders). */
export function CsvImportButton({ kind }: { kind: NetworkImportKind }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
        {t('network.import.button')}
      </button>
      {open && <CsvImportModal kind={kind} onClose={() => setOpen(false)} />}
    </>
  );
}
