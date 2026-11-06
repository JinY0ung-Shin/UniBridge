import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getNetworkProbes, type ProbeFilters, type ProbeResult } from '../../api/network';
import { NETWORK_KEY, intOrUndefined } from './networkUtils';
import { ProbesTable } from './ProbeViews';
import { useAllNetworkHosts } from './useNetworkActions';

const RESULTS: ProbeResult[] = ['open', 'refused', 'timeout', 'error'];

function ProbesTab() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [hostId, setHostId] = useState(searchParams.get('host_id') ?? '');
  const [result, setResult] = useState<'' | ProbeResult>('');
  // The overview's mismatch card links here with ?mismatch=1.
  const [mismatch, setMismatch] = useState(searchParams.get('mismatch') === '1');

  const filters: ProbeFilters = {
    host_id: intOrUndefined(hostId),
    result: result || undefined,
    mismatch,
  };
  const probesQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'probes', filters],
    queryFn: () => getNetworkProbes(filters),
  });
  const hosts = useAllNetworkHosts().data ?? [];

  return (
    <div>
      <div className="network-toolbar">
        <div className="network-filters">
          <select value={hostId} onChange={(e) => setHostId(e.target.value)} aria-label={t('network.filterHost')}>
            <option value="">{t('network.allHosts')}</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{`${h.name} (${h.ip})`}</option>)}
          </select>
          <select
            value={result}
            onChange={(e) => setResult(e.target.value as '' | ProbeResult)}
            aria-label={t('network.probes.resultColumn')}
          >
            <option value="">{t('network.probes.allResults')}</option>
            {RESULTS.map((r) => <option key={r} value={r}>{t(`network.probes.result.${r}`)}</option>)}
          </select>
          <label className="method-check">
            <input type="checkbox" checked={mismatch} onChange={(e) => setMismatch(e.target.checked)} />
            {t('network.probes.onlyMismatch')}
          </label>
        </div>
      </div>

      <details className="network-note network-help">
        <summary>{t('network.probes.helpTitle')}</summary>
        <p>{t('network.probes.helpIntro')}</p>
        <ul>
          <li>{t('network.probes.helpResults')}</li>
          <li>{t('network.probes.helpMismatch')}</li>
          <li>{t('network.probes.helpAgents')}</li>
          <li>
            {t('network.probes.helpScript')}
            <pre className="network-help__code">{'export UNIBRIDGE_URL=https://<HOST_IP>:<UNIBRIDGE_UI_PORT>\nexport UNIBRIDGE_API_KEY=...\npython3 network_probe.py            # --dry-run: measure and print only'}</pre>
          </li>
        </ul>
      </details>

      {probesQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {probesQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {probesQuery.isSuccess && <ProbesTable probes={probesQuery.data} label={t('network.tabs.probes')} />}
    </div>
  );
}

export default ProbesTab;
