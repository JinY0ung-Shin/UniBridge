import { useDeferredValue, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getNetworkHosts,
  importMonitoredHosts,
  updateNetworkHost,
  type NetworkHost,
  type NetworkHostKind,
} from '../../api/network';
import { useCanWrite } from '../../components/useCanWrite';
import { useToast } from '../../components/useToast';
import { NETWORK_KEY, intOrUndefined, networkErrorMessage } from './networkUtils';
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
  const [kind, setKind] = useState<NetworkHostKind | ''>('');
  const [editing, setEditing] = useState<NetworkHost | null | 'new'>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const q = useDeferredValue(search.trim());

  const filters = {
    q: q || undefined, region_id: intOrUndefined(regionId), unverified, kind: kind || undefined,
  };
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

  // Switch several hosts between managed and external at once, one request
  // each; the ones the backend refuses (e.g. linked to a monitored server) are
  // reported and stay selected.
  const bulkKind = useNetworkMutation(
    async (target: NetworkHostKind) => {
      const failed: string[] = [];
      const failedIds: number[] = [];
      let done = 0;
      for (const host of hosts.filter((h) => selected.has(h.id))) {
        if (host.kind === target) continue;
        try {
          await updateNetworkHost(host.id, { kind: target });
          done += 1;
        } catch (err) {
          failed.push(`${host.name}: ${networkErrorMessage(err)}`);
          failedIds.push(host.id);
        }
      }
      return { done, failed, failedIds };
    },
    'network.saveFailed',
    (result) => {
      setSelected(new Set(result.failedIds));
      addToast({
        type: result.failed.length ? 'error' : 'success',
        title: result.failed.length
          ? t('network.hosts.bulkFailed', { count: result.failed.length })
          : t('network.hosts.bulkDone', { count: result.done }),
        message: result.failed.join('\n') || undefined,
      });
    },
  );
  const visibleSelected = hosts.filter((h) => selected.has(h.id));
  const allSelected = hosts.length > 0 && visibleSelected.length === hosts.length;

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

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
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as NetworkHostKind | '')}
            aria-label={t('network.kind.label')}
          >
            <option value="">{t('network.kind.all')}</option>
            <option value="managed">{t('network.kind.managed')}</option>
            <option value="external">{t('network.kind.external')}</option>
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

      {canWrite && visibleSelected.length > 0 && (
        <div className="network-toolbar" role="group" aria-label={t('network.hosts.selectedCount', { count: visibleSelected.length })}>
          <span>{t('network.hosts.selectedCount', { count: visibleSelected.length })}</span>
          <div className="network-filters">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => bulkKind.mutate('external')}
              disabled={bulkKind.isPending}
              title={t('network.kind.externalHint')}
            >
              {t('network.hosts.markExternal')}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => bulkKind.mutate('managed')}
              disabled={bulkKind.isPending}
            >
              {t('network.hosts.markManaged')}
            </button>
          </div>
        </div>
      )}
      {hostsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {hostsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {hostsQuery.isSuccess && (hosts.length === 0 ? (
        <div className="empty-state"><p>{t('network.hosts.empty')}</p></div>
      ) : (
        <div className="table-container">
          <table className="data-table network-table" aria-label={t('network.tabs.hosts')}>
            <thead>
              <tr>
                {canWrite && (
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label={t('network.hosts.selectAll')}
                      checked={allSelected}
                      onChange={() => setSelected(allSelected ? new Set() : new Set(hosts.map((h) => h.id)))}
                    />
                  </th>
                )}
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
                  {canWrite && (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={t('network.hosts.select', { name: h.name })}
                        checked={selected.has(h.id)}
                        onChange={() => toggle(h.id)}
                      />
                    </td>
                  )}
                  <td>
                    <HostLink id={h.id} name={h.name} />
                    {h.description && <div className="network-sub">{h.description}</div>}
                  </td>
                  <td className="mono">{h.ip}</td>
                  <td><RegionName name={h.region_name} external={h.kind === 'external'} /></td>
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
