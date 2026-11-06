import { useDeferredValue, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getPortUsages, type PortUsage, type UsageFilters } from '../../api/network';
import { useCanWrite } from '../../components/useCanWrite';
import { NETWORK_KEY, intOrUndefined } from './networkUtils';
import { UsagesTable } from './NetworkTables';
import { UsageFormModal } from './NetworkForms';
import { CsvImportButton } from './CsvImportModal';
import { useAllNetworkHosts } from './useNetworkActions';

type SourceFilter = '' | NonNullable<UsageFilters['source']>;

function UsagesTab() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const [hostId, setHostId] = useState('');
  const [port, setPort] = useState('');
  const [source, setSource] = useState<SourceFilter>('');
  const [unverified, setUnverified] = useState(false);
  const [editing, setEditing] = useState<PortUsage | null | 'new'>(null);
  const deferredPort = useDeferredValue(port);

  const filters: UsageFilters = {
    host_id: intOrUndefined(hostId),
    port: intOrUndefined(deferredPort),
    source: source || undefined,
    unverified,
  };
  const usagesQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'port-usages', filters],
    queryFn: () => getPortUsages(filters),
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
          <input
            className="network-port-input"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(e) => setPort(e.target.value)}
            placeholder={t('network.filterPort')}
            aria-label={t('network.filterPort')}
          />
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as SourceFilter)}
            aria-label={t('network.usages.sourceFilter')}
          >
            <option value="">{t('network.usages.allSources')}</option>
            <option value="manual">{t('network.source.manual')}</option>
            <option value="agent">{t('network.source.agent')}</option>
            <option value="auto">{t('network.source.auto')}</option>
          </select>
          <label className="method-check">
            <input type="checkbox" checked={unverified} onChange={(e) => setUnverified(e.target.checked)} />
            {t('network.onlyUnverified')}
          </label>
        </div>
        {canWrite && (
          <div className="network-filters">
            <CsvImportButton kind="port_usages" />
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>{t('network.usages.add')}</button>
          </div>
        )}
      </div>
      <p className="page-subtitle">{t('network.usages.hint')}</p>

      {usagesQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {usagesQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {usagesQuery.isSuccess && (
        <UsagesTable usages={usagesQuery.data} label={t('network.tabs.usages')} onEdit={(u) => setEditing(u)} />
      )}

      {editing && <UsageFormModal usage={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export default UsagesTab;
