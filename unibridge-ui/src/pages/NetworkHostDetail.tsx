import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getNetworkHost, type FirewallOpening, type PortUsage } from '../api/network';
import { useCanWrite } from '../components/useCanWrite';
import { NETWORK_KEY } from './network/networkUtils';
import { OpeningsTable, ProvenanceBadges, RegionName, UsagesTable } from './network/NetworkTables';
import { HostFormModal, OpeningFormModal, UsageFormModal } from './network/NetworkForms';
import { useNetworkRowActions } from './network/useNetworkActions';
import './Connections.css';
import './Network.css';

type Editing =
  | { kind: 'host' }
  | { kind: 'opening'; opening: FirewallOpening | null; defaults?: { src_host_id?: number; dst_host_id?: number } }
  | { kind: 'usage'; usage: PortUsage | null };

function NetworkHostDetail() {
  const { t } = useTranslation();
  const rawId = useParams().id ?? '';
  const id = Number(rawId);
  const canWrite = useCanWrite('network.write');
  const [editing, setEditing] = useState<Editing | null>(null);
  const { verifyHost } = useNetworkRowActions();

  const hostQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'host', id],
    queryFn: () => getNetworkHost(id),
    enabled: Number.isInteger(id),
  });
  const host = hostQuery.data;

  return (
    <div className="connections network-page">
      <div className="page-header">
        <div>
          <Link className="link-button" to="/network?tab=hosts">&larr; {t('network.title')}</Link>
          <h1>{host?.name ?? `#${rawId}`}</h1>
          {host && (
            <p className="page-subtitle">
              <span className="mono">{host.ip}</span> · <RegionName name={host.region_name} />
            </p>
          )}
        </div>
        {host && canWrite && (
          <div className="page-header__actions">
            {!host.verified && (
              <button
                type="button"
                className="btn btn-success"
                aria-label={t('network.verifyItem', { name: host.name })}
                onClick={() => verifyHost.mutate(host.id)}
                disabled={verifyHost.isPending}
              >
                {t('network.verify')}
              </button>
            )}
            <button type="button" className="btn btn-secondary" onClick={() => setEditing({ kind: 'host' })}>
              {t('network.hosts.edit')}
            </button>
          </div>
        )}
      </div>

      {hostQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {hostQuery.isError && <div className="error-banner" role="alert">{t('network.hosts.notFound')}</div>}

      {host && (
        <>
          <dl className="network-host-info">
            <div><dt>{t('network.owner')}</dt><dd>{host.owner || '—'}</dd></div>
            <div><dt>{t('network.description')}</dt><dd>{host.description || '—'}</dd></div>
            <div>
              <dt>{t('network.hosts.monitored')}</dt>
              <dd>
                {host.monitored_host_id != null
                  ? <Link to={`/servers/${host.monitored_host_id}`}>{host.monitored_host_name ?? `#${host.monitored_host_id}`}</Link>
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>{t('network.hosts.recordedBy')}</dt>
              <dd>
                {host.created_by ?? '—'} <ProvenanceBadges source={host.source} verified={host.verified} />
                {host.verified_by && <div className="network-sub">{t('network.hosts.verifiedBy', { name: host.verified_by })}</div>}
              </dd>
            </div>
          </dl>

          <section className="network-section" aria-labelledby="host-inbound-title">
            <div className="network-toolbar">
              <h2 id="host-inbound-title" className="section-title">{t('network.hosts.inbound')}</h2>
              {canWrite && (
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => setEditing({ kind: 'opening', opening: null, defaults: { dst_host_id: host.id } })}
                >
                  {t('network.openings.add')}
                </button>
              )}
            </div>
            <OpeningsTable
              openings={host.inbound}
              label={t('network.hosts.inbound')}
              onEdit={(o) => setEditing({ kind: 'opening', opening: o })}
            />
          </section>

          <section className="network-section" aria-labelledby="host-outbound-title">
            <div className="network-toolbar">
              <h2 id="host-outbound-title" className="section-title">{t('network.hosts.outbound')}</h2>
              {canWrite && (
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => setEditing({ kind: 'opening', opening: null, defaults: { src_host_id: host.id } })}
                >
                  {t('network.openings.add')}
                </button>
              )}
            </div>
            <OpeningsTable
              openings={host.outbound}
              label={t('network.hosts.outbound')}
              onEdit={(o) => setEditing({ kind: 'opening', opening: o })}
            />
          </section>

          <section className="network-section" aria-labelledby="host-usages-title">
            <div className="network-toolbar">
              <h2 id="host-usages-title" className="section-title">{t('network.tabs.usages')}</h2>
              {canWrite && (
                <button type="button" className="btn btn-sm btn-primary" onClick={() => setEditing({ kind: 'usage', usage: null })}>
                  {t('network.usages.add')}
                </button>
              )}
            </div>
            <UsagesTable
              usages={host.usages}
              label={t('network.tabs.usages')}
              showHost={false}
              onEdit={(u) => setEditing({ kind: 'usage', usage: u })}
            />
          </section>

          {editing?.kind === 'host' && <HostFormModal host={host} onClose={() => setEditing(null)} />}
          {editing?.kind === 'opening' && (
            <OpeningFormModal opening={editing.opening} defaults={editing.defaults} onClose={() => setEditing(null)} />
          )}
          {editing?.kind === 'usage' && (
            <UsageFormModal usage={editing.usage} defaults={{ host_id: host.id }} onClose={() => setEditing(null)} />
          )}
        </>
      )}
    </div>
  );
}

export default NetworkHostDetail;
