import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { checkNetworkAccess, type AccessEndpoint, type NetworkProtocol } from '../../api/network';
import { useToast } from '../../components/useToast';
import { intOrUndefined, networkErrorMessage } from './networkUtils';
import { OpeningsTable, UsagesTable } from './NetworkTables';
import { useAllNetworkHosts } from './useNetworkActions';

const STATUS_BADGE: Record<string, string> = {
  open: 'badge-ok',
  blocked: 'badge-error',
  unknown: 'badge-warn',
};

function EndpointSummary({ label, endpoint }: { label: string; endpoint: AccessEndpoint }) {
  const { t } = useTranslation();
  return (
    <div className="network-endpoint">
      <div className="network-sub">{label}</div>
      <div>{endpoint.host_name ?? endpoint.input}</div>
      {endpoint.ip && <div className="network-sub mono">{endpoint.ip}</div>}
      <div className="network-sub">{endpoint.region_name ?? t('network.noRegion')}</div>
    </div>
  );
}

function AccessCheckTab() {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const hosts = useAllNetworkHosts().data ?? [];
  const [form, setForm] = useState({ src: '', dst: '', port: '', protocol: 'tcp' as NetworkProtocol });
  const check = useMutation({ mutationFn: checkNetworkAccess });
  const result = check.data;

  async function copyDraft(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      addToast({ type: 'success', title: t('network.check.copied') });
    } catch {
      addToast({ type: 'error', title: t('network.check.copyFailed') });
    }
  }

  return (
    <div>
      <p className="page-subtitle">{t('network.check.hint')}</p>
      <form
        className="network-check-form"
        onSubmit={(e) => {
          e.preventDefault();
          const port = intOrUndefined(form.port);
          if (port === undefined) return;
          check.mutate({ src: form.src.trim(), dst: form.dst.trim(), port, protocol: form.protocol });
        }}
      >
        <datalist id="network-host-options">
          {hosts.flatMap((h) => [
            <option key={`n-${h.id}`} value={h.name}>{h.ip}</option>,
            <option key={`i-${h.id}`} value={h.ip}>{h.name}</option>,
          ])}
        </datalist>
        <div className="form-group">
          <label htmlFor="check-src">{t('network.check.src')}</label>
          <input
            id="check-src"
            list="network-host-options"
            value={form.src}
            onChange={(e) => setForm({ ...form, src: e.target.value })}
            placeholder={t('network.check.endpointPlaceholder')}
            required
          />
        </div>
        <div className="form-group">
          <label htmlFor="check-dst">{t('network.check.dst')}</label>
          <input
            id="check-dst"
            list="network-host-options"
            value={form.dst}
            onChange={(e) => setForm({ ...form, dst: e.target.value })}
            placeholder={t('network.check.endpointPlaceholder')}
            required
          />
        </div>
        <div className="form-group">
          <label htmlFor="check-port">{t('network.usages.port')}</label>
          <input
            id="check-port"
            type="number"
            min={1}
            max={65535}
            value={form.port}
            onChange={(e) => setForm({ ...form, port: e.target.value })}
            required
          />
        </div>
        <div className="form-group">
          <label htmlFor="check-protocol">{t('network.protocol')}</label>
          <select
            id="check-protocol"
            value={form.protocol}
            onChange={(e) => setForm({ ...form, protocol: e.target.value as NetworkProtocol })}
          >
            <option value="tcp">tcp</option>
            <option value="udp">udp</option>
          </select>
        </div>
        <div className="form-group network-check-form__submit">
          <button type="submit" className="btn btn-primary" disabled={check.isPending} aria-busy={check.isPending}>
            {t('network.check.run')}
          </button>
        </div>
      </form>

      {check.isError && (
        <div className="error-banner" role="alert">{networkErrorMessage(check.error, t('common.errorOccurred'))}</div>
      )}

      {result && (
        <section className="network-section" aria-labelledby="network-check-result">
          <h2 id="network-check-result" className="section-title">{t('network.check.result')}</h2>
          <div className="network-check-result">
            <span className={`badge network-check-status ${STATUS_BADGE[result.status] ?? 'badge-unknown'}`}>
              {t(`network.check.status.${result.status}`, { defaultValue: result.status })}
            </span>
            <span>{t(`network.check.reason.${result.reason}`, { defaultValue: result.message })}</span>
          </div>
          <div className="network-check-endpoints">
            <EndpointSummary label={t('network.check.src')} endpoint={result.src} />
            <span className="network-arrow" aria-hidden="true">→</span>
            <EndpointSummary label={t('network.check.dst')} endpoint={result.dst} />
            <span className="mono">{`${result.protocol}/${result.port}`}</span>
          </div>

          {result.request_draft && (
            <div className="network-draft">
              <div className="network-draft__header">
                <h3>{t('network.check.requestDraft')}</h3>
                <button type="button" className="btn btn-sm btn-secondary" onClick={() => copyDraft(result.request_draft!)}>
                  {t('network.check.copy')}
                </button>
              </div>
              <pre aria-label={t('network.check.requestDraft')}>{result.request_draft}</pre>
            </div>
          )}

          <h3 className="network-subtitle">{t('network.check.matchingOpenings')}</h3>
          <OpeningsTable openings={result.openings} label={t('network.check.matchingOpenings')} />
          <h3 className="network-subtitle">{t('network.check.usagesOnPort')}</h3>
          <UsagesTable usages={result.usages} label={t('network.check.usagesOnPort')} />
        </section>
      )}
    </div>
  );
}

export default AccessCheckTab;
