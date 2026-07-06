import { useTranslation } from 'react-i18next';
import type { TimeSelection } from '../utils/timeRange';
import { useAuth } from './useAuth';

// Same-origin path to the bundled Grafana (proxied by this nginx under
// /grafana so it shares the UI's TLS); injected at container start
// (entrypoint.sh) with a Vite env fallback for local dev. Trailing slashes
// are stripped so an override like "https://grafana.example.com/" doesn't
// produce "//d/..." hrefs.
function grafanaBase(): string {
  const raw =
    window.__RUNTIME_CONFIG__?.GRAFANA_URL ||
    import.meta.env.VITE_GRAFANA_URL ||
    '/grafana';
  return raw.replace(/\/+$/, '');
}

/** Map the page's time selection to Grafana's from/to query params. */
function grafanaTime(sel?: TimeSelection): Record<string, string> {
  if (!sel) return {};
  return sel.kind === 'preset'
    ? { from: `now-${sel.value}`, to: 'now' }
    : { from: String(sel.start * 1000), to: String(sel.end * 1000) };
}

interface GrafanaLinkProps {
  /** Grafana dashboard UID this page mirrors (e.g. "unibridge-gateway"). */
  dashboard: string;
  /** Template-variable preselection, e.g. { 'var-host': name }. Empty/null values are dropped. */
  vars?: Record<string, string | null | undefined>;
  /** Current time selection, carried over as Grafana's from/to. */
  time?: TimeSelection;
}

export default function GrafanaLink({ dashboard, vars, time }: GrafanaLinkProps) {
  const { t } = useTranslation();
  const { appRole } = useAuth();
  // Grafana SSO is admin-only (ROLE_ATTRIBUTE_STRICT, no Viewer fallback), so
  // don't render a link that would dead-end at a rejected login.
  if (appRole !== 'admin') return null;
  const params: Record<string, string> = grafanaTime(time);
  for (const [key, value] of Object.entries(vars ?? {})) {
    if (value) params[key] = value;
  }
  const qs = new URLSearchParams(params).toString();
  return (
    <a
      href={`${grafanaBase()}/d/${dashboard}${qs ? `?${qs}` : ''}`}
      target="_blank"
      rel="noopener noreferrer"
      className="grafana-link-btn"
      title={t('monitoring.grafanaSsoHint')}
      aria-label={`${t('monitoring.openInGrafana')} ${t('common.opensInNewTab')}`}
    >
      {/* Bar-chart mark distinguishes this from sibling external-link buttons
          (e.g. the LiteLLM admin button), which share the same pill style. */}
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ marginRight: 5 }} aria-hidden="true">
        <path d="M2 10.5V6.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <path d="M6 10.5V2.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <path d="M10 10.5V4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
      {t('monitoring.openInGrafana')}
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ marginLeft: 4 }} aria-hidden="true">
        <path d="M3.5 1.5H10.5V8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M10.5 1.5L1.5 10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    </a>
  );
}
