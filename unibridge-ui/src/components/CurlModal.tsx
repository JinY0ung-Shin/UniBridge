import { useTranslation } from 'react-i18next';
import ResourceModal from './ResourceModal';
import { useCopyFeedback } from './useCopyFeedback';
import { useToast } from './useToast';

interface CurlModalProps {
  title: string;
  curl: string;
  /** Optional line above the command, e.g. the route it calls. */
  caption?: string;
  onClose: () => void;
}

/** A ready-to-run cURL command with a copy button. */
function CurlModal({ title, curl, caption, onClose }: CurlModalProps) {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const { copied, copy } = useCopyFeedback(() => addToast({ type: 'error', title: t('common.curlCopyFailed') }));

  return (
    <ResourceModal title={title} onClose={onClose} closeLabel={t('common.close')} className="modal--sm">
      <div className="curl-block">
        {caption && <div className="curl-route-name">{caption}</div>}
        <pre className="curl-code">{curl}</pre>
        <button
          type="button"
          className="btn btn-sm btn-secondary curl-copy-btn"
          onClick={() => void copy(curl)}
          aria-label={copied ? t('common.curlCopied') : t('common.copyCurl')}
        >
          {copied ? t('common.copied') : t('common.copy')}
        </button>
        <span className="visually-hidden" role="status" aria-live="polite">
          {copied ? t('common.copied') : ''}
        </span>
      </div>
    </ResourceModal>
  );
}

export default CurlModal;
