import { useTranslation } from 'react-i18next';

// Base URL of the bundled Grafana; injected at container start (entrypoint.sh),
// with a Vite env fallback for local dev against a locally-running stack.
const GRAFANA_URL =
  window.__RUNTIME_CONFIG__?.GRAFANA_URL ||
  import.meta.env.VITE_GRAFANA_URL ||
  'http://localhost:3300';

interface GrafanaLinkProps {
  /** Grafana dashboard UID this page mirrors (e.g. "unibridge-gateway"). */
  dashboard: string;
  /** Optional template-variable preselection, e.g. { 'var-host': name }. */
  vars?: Record<string, string>;
}

export default function GrafanaLink({ dashboard, vars }: GrafanaLinkProps) {
  const { t } = useTranslation();
  const query = vars ? `?${new URLSearchParams(vars).toString()}` : '';
  return (
    <a
      href={`${GRAFANA_URL}/d/${dashboard}${query}`}
      target="_blank"
      rel="noopener noreferrer"
      className="grafana-link-btn"
      aria-label={`${t('monitoring.openInGrafana')} ${t('common.opensInNewTab')}`}
    >
      {t('monitoring.openInGrafana')}
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ marginLeft: 4 }} aria-hidden="true">
        <path d="M3.5 1.5H10.5V8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M10.5 1.5L1.5 10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    </a>
  );
}
