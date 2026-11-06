import { useDeferredValue, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getFirewallOpenings, type FirewallOpening, type OpeningStatus } from '../../api/network';
import { useCanWrite } from '../../components/useCanWrite';
import { NETWORK_KEY, intOrUndefined } from './networkUtils';
import { OpeningsTable } from './NetworkTables';
import { OpeningFormModal } from './NetworkForms';
import { CsvImportButton } from './CsvImportModal';
import { useAllNetworkHosts, useRegions } from './useNetworkActions';

function OpeningsTab() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const [hostId, setHostId] = useState('');
  const [regionId, setRegionId] = useState('');
  const [status, setStatus] = useState<'' | OpeningStatus>('');
  const [port, setPort] = useState('');
  const [unverified, setUnverified] = useState(false);
  const [editing, setEditing] = useState<FirewallOpening | null | 'new'>(null);
  const deferredPort = useDeferredValue(port);

  const filters = {
    host_id: intOrUndefined(hostId),
    region_id: intOrUndefined(regionId),
    status: status || undefined,
    port: intOrUndefined(deferredPort),
    unverified,
  };
  const openingsQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'openings', filters],
    queryFn: () => getFirewallOpenings(filters),
  });
  const hosts = useAllNetworkHosts().data ?? [];
  const regions = useRegions().data ?? [];

  return (
    <div>
      <div className="network-toolbar">
        <div className="network-filters">
          <select value={hostId} onChange={(e) => setHostId(e.target.value)} aria-label={t('network.filterHost')}>
            <option value="">{t('network.allHosts')}</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{`${h.name} (${h.ip})`}</option>)}
          </select>
          <select value={regionId} onChange={(e) => setRegionId(e.target.value)} aria-label={t('network.region')}>
            <option value="">{t('network.allRegions')}</option>
            {regions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as '' | OpeningStatus)}
            aria-label={t('network.openings.status')}
          >
            <option value="">{t('network.allStatuses')}</option>
            <option value="open">{t('network.status.open')}</option>
            <option value="closed">{t('network.status.closed')}</option>
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
          <label className="method-check">
            <input type="checkbox" checked={unverified} onChange={(e) => setUnverified(e.target.checked)} />
            {t('network.onlyUnverified')}
          </label>
        </div>
        {canWrite && (
          <div className="network-filters">
            <CsvImportButton kind="openings" />
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>{t('network.openings.add')}</button>
          </div>
        )}
      </div>
      <p className="page-subtitle">{t('network.openings.hint')}</p>

      {openingsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {openingsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {openingsQuery.isSuccess && (
        <OpeningsTable
          openings={openingsQuery.data}
          label={t('network.tabs.openings')}
          onEdit={(o) => setEditing(o)}
        />
      )}

      {editing && <OpeningFormModal opening={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export default OpeningsTab;
