import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getQueryTemplateGuide } from '../../api/client';
import ResourceModal from '../../components/ResourceModal';
import { extractErrorDetail } from '../../components/useResourceMutation';
import { usePermissions } from '../../components/usePermissions';
import { useToast } from '../../components/useToast';
import './AgentApiModal.css';

// Gateway route IDs provisioned at boot (unibridge-service main.py). query-api
// covers all of /api/query/* (template discovery and runs, but also ad-hoc
// /api/query/execute); creating, editing, and deleting needs the write route.
const READ_ROUTE = 'query-api';
const WRITE_ROUTE = 'query-template-write-api';

const ENDPOINTS = [
  { method: 'GET', path: '/api/query/templates', purposeKey: 'queryTemplates.agentApiOpList', route: READ_ROUTE },
  { method: 'POST', path: '/api/query/templates/{path}', purposeKey: 'queryTemplates.agentApiOpRun', route: READ_ROUTE },
  { method: 'PUT', path: '/api/query/templates/{path}', purposeKey: 'queryTemplates.agentApiOpCreate', route: WRITE_ROUTE },
  { method: 'PATCH', path: '/api/query/templates/{path}', purposeKey: 'queryTemplates.agentApiOpEdit', route: WRITE_ROUTE },
  { method: 'DELETE', path: '/api/query/templates/{path}', purposeKey: 'queryTemplates.agentApiOpDelete', route: WRITE_ROUTE },
  { method: 'GET', path: '/api/query/templates/guide', purposeKey: 'queryTemplates.agentApiOpGuide', route: READ_ROUTE },
] as const;

type CopyTarget = 'handoff' | 'curl';

/** How API-key agents drive query templates through the gateway, with copyable starters. */
function AgentApiModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const { permissions } = usePermissions();
  const { addToast } = useToast();
  const [copied, setCopied] = useState<CopyTarget | null>(null);
  const copyTimeoutRef = useRef<number | null>(null);
  const [guideOpen, setGuideOpen] = useState(false);

  const guideQuery = useQuery({
    queryKey: ['query-template-agent-guide'],
    queryFn: getQueryTemplateGuide,
    enabled: guideOpen,
    staleTime: Infinity,
    // The likely failure is a 403 (no query.execute); show it at once. Collapsing
    // and expanding again refetches.
    retry: false,
  });

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current !== null) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  const base = `${window.location.origin}/api/query/templates`;
  const handoffText = [
    t('queryTemplates.agentApiHandoffPrompt'),
    `curl -k -H 'apikey: <YOUR_API_KEY>' '${base}/guide'`,
  ].join('\n');
  const curlText = [
    `# ${t('queryTemplates.agentApiOpList')}`,
    `curl -k -H 'apikey: <YOUR_API_KEY>' \\`,
    `  '${base}'`,
    ``,
    `# ${t('queryTemplates.agentApiOpRun')}`,
    `curl -k -X POST \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H 'apikey: <YOUR_API_KEY>' \\`,
    `  '${base}/reports/users' \\`,
    `  -d '{"params": {"id": 42}, "limit": 50}'`,
    ``,
    `# ${t('queryTemplates.agentApiOpCreate')}`,
    `curl -k -X PUT \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H 'apikey: <YOUR_API_KEY>' \\`,
    `  '${base}/reports/new-users' \\`,
    `  -d '{"name": "New users report", "database": "<DATABASE>", "sql": "SELECT id, name FROM users WHERE created_at >= :since"}'`,
    ``,
    `# ${t('queryTemplates.agentApiOpEdit')}`,
    `curl -k -X PATCH \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H 'apikey: <YOUR_API_KEY>' \\`,
    `  '${base}/reports/new-users' \\`,
    `  -d '{"description": "Users created since :since", "expected_updated_at": "<UPDATED_AT>"}'`,
    ``,
    `# ${t('queryTemplates.agentApiOpDelete')}`,
    `curl -k -X DELETE -H 'apikey: <YOUR_API_KEY>' \\`,
    `  '${base}/reports/new-users?expected_updated_at=<UPDATED_AT>'`,
  ].join('\n');

  const canManageKeys = permissions.includes('apikeys.read');
  // Self-service keys are pinned to query-api + s3-api (unibridge-service
  // api_keys.py SELF_ALLOWED_ROUTES), so they never get the write route.
  const hasSelfKeyOnly = !canManageKeys && permissions.includes('apikeys.self');
  const apiKeysLink = canManageKeys
    ? { to: '/api-keys', label: t('nav.apiKeys') }
    : hasSelfKeyOnly
      ? { to: '/my-api-key', label: t('nav.myApiKey') }
      : null;
  const guideErrorDetail = guideQuery.isError ? extractErrorDetail(guideQuery.error) : undefined;

  function clearCopyTimeout() {
    if (copyTimeoutRef.current !== null) {
      window.clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = null;
    }
  }

  async function handleCopy(target: CopyTarget, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      // Cleared after the await so an earlier copy that resolved meanwhile
      // cannot leave its timer running and reset this one early.
      clearCopyTimeout();
      setCopied(target);
      copyTimeoutRef.current = window.setTimeout(() => {
        setCopied(null);
        copyTimeoutRef.current = null;
      }, 2000);
    } catch {
      clearCopyTimeout();
      setCopied(null);
      addToast({ type: 'error', title: t('queryTemplates.agentApiCopyFailed') });
    }
  }

  return (
    <ResourceModal
      title={t('queryTemplates.agentApiTitle')}
      onClose={onClose}
      closeLabel={t('common.close')}
      className="agent-api-modal"
    >
      <div className="agent-api-body">
        <p className="agent-api-intro">{t('queryTemplates.agentApiIntro')}</p>

        <section className="agent-api-section">
          <div className="agent-api-section-header">
            <h3>{t('queryTemplates.agentApiHandoffTitle')}</h3>
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() => handleCopy('handoff', handoffText)}
              aria-label={
                copied === 'handoff' ? t('queryTemplates.agentApiCopiedHandoff') : t('queryTemplates.agentApiCopyHandoff')
              }
            >
              {copied === 'handoff' ? t('queryTemplates.agentApiCopied') : t('queryTemplates.agentApiCopy')}
            </button>
          </div>
          <p className="agent-api-hint">{t('queryTemplates.agentApiHandoffHint')}</p>
          <pre className="agent-api-code">{handoffText}</pre>
        </section>

        <section className="agent-api-section">
          <div className="agent-api-section-header">
            <h3>{t('queryTemplates.agentApiRoutesTitle')}</h3>
            {apiKeysLink && (
              <Link to={apiKeysLink.to} className="btn btn-sm btn-secondary">
                {apiKeysLink.label}
              </Link>
            )}
          </div>
          <dl className="agent-api-routes">
            <div>
              <dt><code>{READ_ROUTE}</code></dt>
              <dd>{t('queryTemplates.agentApiRouteRead')}</dd>
            </div>
            <div>
              <dt><code>{WRITE_ROUTE}</code></dt>
              <dd>{t('queryTemplates.agentApiRouteWrite')}</dd>
            </div>
          </dl>
          <p className="agent-api-hint">{t('queryTemplates.agentApiRoutesNote')}</p>
          {hasSelfKeyOnly && <p className="agent-api-hint">{t('queryTemplates.agentApiSelfKeyNote')}</p>}
        </section>

        <section className="agent-api-section">
          <h3 id="agent-api-endpoints-title">{t('queryTemplates.agentApiEndpointsTitle')}</h3>
          <div className="agent-api-table-scroll" tabIndex={0} role="region" aria-labelledby="agent-api-endpoints-title">
            <table className="agent-api-table">
              <thead>
                <tr>
                  <th scope="col">{t('queryTemplates.agentApiColMethod')}</th>
                  <th scope="col">{t('queryTemplates.agentApiColPath')}</th>
                  <th scope="col">{t('queryTemplates.agentApiColPurpose')}</th>
                  <th scope="col">{t('queryTemplates.agentApiColRoute')}</th>
                </tr>
              </thead>
              <tbody>
                {ENDPOINTS.map((endpoint) => (
                  <tr key={`${endpoint.method} ${endpoint.path}`}>
                    <td>
                      <span className={`method-badge method-badge--${endpoint.method.toLowerCase()}`}>
                        {endpoint.method}
                      </span>
                    </td>
                    <td><code>{endpoint.path}</code></td>
                    <td>{t(endpoint.purposeKey)}</td>
                    <td><code>{endpoint.route}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="agent-api-hint">{t('queryTemplates.agentApiConcurrencyNote')}</p>
          <p className="agent-api-hint">{t('queryTemplates.agentApiDisabledNote')}</p>
        </section>

        <section className="agent-api-section">
          <div className="agent-api-section-header">
            <h3>{t('queryTemplates.agentApiCurlTitle')}</h3>
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() => handleCopy('curl', curlText)}
              aria-label={copied === 'curl' ? t('gatewayRoutes.curlCopiedLabel') : t('gatewayRoutes.curlCopyLabel')}
            >
              {copied === 'curl' ? t('queryTemplates.agentApiCopied') : t('queryTemplates.agentApiCopy')}
            </button>
          </div>
          <pre className="agent-api-code">{curlText}</pre>
        </section>

        <section className="agent-api-section">
          <button
            type="button"
            className="agent-api-disclosure"
            aria-expanded={guideOpen}
            aria-controls="agent-api-guide"
            onClick={() => setGuideOpen((open) => !open)}
          >
            <span className="agent-api-disclosure-icon" aria-hidden="true">▸</span>
            {t('queryTemplates.agentApiFullGuide')}
          </button>
          <div id="agent-api-guide" className="agent-api-guide" hidden={!guideOpen}>
            {guideQuery.isLoading && (
              <div className="loading-message" role="status">{t('queryTemplates.agentApiGuideLoading')}</div>
            )}
            {guideQuery.isError && (
              <div className="error-banner" role="alert">
                {t('queryTemplates.agentApiGuideLoadFailed')}
                {guideErrorDetail ? `: ${guideErrorDetail}` : ''}
              </div>
            )}
            {guideQuery.data && (
              <article className="markdown-body">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    table: ({ children }) => (
                      <div
                        className="markdown-table-scroll"
                        tabIndex={0}
                        role="region"
                        aria-label={t('queryTemplates.agentApiGuideTableRegion')}
                      >
                        <table>{children}</table>
                      </div>
                    ),
                  }}
                >
                  {guideQuery.data}
                </ReactMarkdown>
              </article>
            )}
          </div>
        </section>

        <span className="visually-hidden" role="status" aria-live="polite">
          {copied ? t('queryTemplates.agentApiCopied') : ''}
        </span>
      </div>
    </ResourceModal>
  );
}

export default AgentApiModal;
