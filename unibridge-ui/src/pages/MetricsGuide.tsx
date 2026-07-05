import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
// Synced copy of docs/api-metrics-convention.md — a vitest guards that the two
// files stay identical (the canonical file lives outside the docker build
// context, so it can't be imported directly).
import guideMd from '../content/api-metrics-convention.md?raw';
import './MetricsGuide.css';

/** In-app view of the API metrics convention guide for external services. */
function MetricsGuide() {
  const { t } = useTranslation();
  return (
    <div className="metrics-guide">
      <div className="page-header">
        <div>
          <h1>{t('metricsGuide.title')}</h1>
          <p className="page-subtitle">{t('metricsGuide.subtitle')}</p>
        </div>
        <div className="page-header__actions">
          <Link to="/servers" className="btn btn-secondary">{t('metricsGuide.goRegister')}</Link>
          <Link to="/external/monitoring" className="btn btn-secondary">{t('metricsGuide.goMonitoring')}</Link>
        </div>
      </div>
      <article className="markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{guideMd}</ReactMarkdown>
      </article>
    </div>
  );
}

export default MetricsGuide;
