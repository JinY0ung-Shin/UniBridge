import { useDeferredValue, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getNetworkHosts, importMonitoredHosts, type NetworkHost } from '../../api/network';
import { useCanWrite } from '../../components/useCanWrite';
import { useToast } from '../../components/useToast';
import { NETWORK_KEY, intOrUndefined } from './networkUtils';
import { HostLink, ProvenanceBadges, RegionName } from './NetworkTables';
import { HostFormModal } from './NetworkForms';
import { CsvImportButton } from './CsvImportModal';
import { useNetworkMutation, useNetworkRowActions, useRegions } from './useNetworkActions';

function HostsTab() {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const canWrite = useCanWrite('network.write');
  const [search, setSearch] = useState('');
  const [regionId, setRegionId] = useState('');
  const [unverified, setUnverified] = useState(false);
  const [editing, setEditing] = useState<NetworkHost | null | 'new'>(null);
  const q = useDeferredValue(search.trim());

  const filters = { q: q || undefined, region_id: intOrUndefined(regionId), unverified };
  const hostsQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'hosts', filters],
    queryFn: () => getNetworkHosts(filters),
  });
  const regions = useRegions().data ?? [];
  const { verifyHost, deleteHost } = useNetworkRowActions();
  const importMutation = useNetworkMutation(importMonitoredHosts, 'network.hosts.importFailed', (result) => {
    const skipped = result.skipped.map((s) => `${s.name}: ${s.reason}`).join('\n');
    addToast({
      type: 'success',
      title: t('network.hosts.importDone', {
        created: result.created.length, linked: result.linked.length, skipped: result.skipped.length,
      }),
      message: skipped || undefined,
    });
  });

  const hosts = hostsQuery.data ?? [];

  return (
    <div>
      <div className="network-toolbar">
        <div className="network-filters">
          <input
            className="connection-search-input"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('network.hosts.searchPlaceholder')}
            aria-label={t('network.hosts.searchPlaceholder')}
          />
          <select value={regionId} onChange={(e) => setRegionId(e.target.value)} aria-label={t('network.region')}>
            <option value="">{t('network.allRegions')}</option>
            {regions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <label className="method-check">
            <input type="checkbox" checked={unverified} onChange={(e) => setUnverified(e.target.checked)} />
            {t('network.onlyUnverified')}
          </label>
        </div>
        {canWrite && (
          <div className="network-filters">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => importMutation.mutate(undefined)}
              disabled={importMutation.isPending}
              aria-busy={importMutation.isPending}
              title={t('network.hosts.importHint')}
            >
              {t('network.hosts.import')}
            </button>
            <CsvImportButton kind="hosts" />
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>{t('network.hosts.add')}</button>
          </div>
        )}
      </div>

      {hostsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {hostsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {hostsQuery.isSuccess && (hosts.length === 0 ? (
        <div className="empty-state"><p>{t('network.hosts.empty')}</p></div>
      ) : (
        <div className="table-container">
          <table className="data-table network-table" aria-label={t('network.tabs.hosts')}>
            <thead>
              <tr>
                <th scope="col">{t('network.hosts.name')}</th>
                <th scope="col">{t('network.hosts.ip')}</th>
                <th scope="col">{t('network.region')}</th>
                <th scope="col">{t('network.owner')}</th>
                <th scope="col">{t('network.hosts.monitored')}</th>
                <th scope="col">{t('network.flags')}</th>
                {canWrite && <th scope="col">{t('common.actions')}</th>}
              </tr>
            </thead>
            <tbody>
              {hosts.map((h) => (
                <tr key={h.id}>
                  <td>
                    <HostLink id={h.id} name={h.name} />
                    {h.description && <div className="network-sub">{h.description}</div>}
                  </td>
                  <td className="mono">{h.ip}</td>
                  <td><RegionName name={h.region_name} /></td>
                  <td>{h.owner}</td>
                  <td>{h.monitored_host_name ?? <span className="network-muted">—</span>}</td>
                  <td><ProvenanceBadges source={h.source} verified={h.verified} /></td>
                  {canWrite && (
                    <td className="cell-actions">
                      {!h.verified && (
                        <button
                          type="button"
                          className="btn btn-sm btn-success"
                          aria-label={t('network.verifyItem', { name: h.name })}
                          onClick={() => verifyHost.mutate(h.id)}
                          disabled={verifyHost.isPending}
                        >
                          {t('network.verify')}
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-sm btn-secondary"
                        aria-label={t('network.editItem', { name: h.name })}
                        onClick={() => setEditing(h)}
                      >
                        {t('common.edit')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        aria-label={t('network.deleteItem', { name: h.name })}
                        onClick={() => {
                          if (window.confirm(t('network.hosts.deleteConfirm', { name: h.name }))) deleteHost.mutate(h.id);
                        }}
                        disabled={deleteHost.isPending}
                      >
                        {t('common.delete')}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {editing && <HostFormModal host={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export default HostsTab;
