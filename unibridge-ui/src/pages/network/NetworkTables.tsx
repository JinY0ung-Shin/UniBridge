import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { FirewallOpening, PortUsage } from '../../api/network';
import { useCanWrite } from '../../components/useCanWrite';
import { openingPorts, openingSource } from './networkUtils';
import { useNetworkRowActions } from './useNetworkActions';

/** Where a record came from, plus "unverified" while no person has confirmed it. */
export function ProvenanceBadges({ source, verified }: { source: string; verified: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="network-badges">
      {source === 'agent' && <span className="badge badge-info">{t('network.source.agent')}</span>}
      {source === 'import' && <span className="badge badge-unknown">{t('network.source.import')}</span>}
      {source === 'auto' && <span className="badge badge-unknown">{t('network.source.auto')}</span>}
      {!verified && source !== 'auto' && <span className="badge badge-warn">{t('network.unverified')}</span>}
    </span>
  );
}

export function HostLink({ id, name }: { id: number | null; name: string | null }) {
  if (id == null || !name) return <>{name ?? ''}</>;
  return <Link to={`/network/hosts/${id}`}>{name}</Link>;
}

export function RegionName({ name }: { name: string | null }) {
  const { t } = useTranslation();
  return name ? <>{name}</> : <span className="network-muted">{t('network.noRegion')}</span>;
}

interface OpeningsTableProps {
  openings: FirewallOpening[];
  label: string;
  onEdit?: (opening: FirewallOpening) => void;
}

export function OpeningsTable({ openings, label, onEdit }: OpeningsTableProps) {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const { verifyOpening, deleteOpening } = useNetworkRowActions();

  if (openings.length === 0) {
    return <div className="empty-state"><p>{t('network.openings.empty')}</p></div>;
  }

  return (
    <div className="table-container">
      <table className="data-table network-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">{t('network.openings.source')}</th>
            <th scope="col">{t('network.openings.destination')}</th>
            <th scope="col">{t('network.ports')}</th>
            <th scope="col">{t('network.openings.status')}</th>
            <th scope="col">{t('network.openings.ticket')}</th>
            <th scope="col">{t('network.openings.requester')}</th>
            <th scope="col">{t('network.openings.purpose')}</th>
            <th scope="col">{t('network.flags')}</th>
            {canWrite && <th scope="col">{t('common.actions')}</th>}
          </tr>
        </thead>
        <tbody>
          {openings.map((o) => {
            const unused = o.status === 'open' && !o.in_use;
            const name = `${openingSource(o)} → ${o.dst_host_name} ${openingPorts(o)}`;
            return (
              <tr key={o.id} className={unused ? 'network-row--unused' : undefined}>
                <td>
                  {o.src_cidr
                    ? <span className="mono">{o.src_cidr}</span>
                    : <HostLink id={o.src_host_id} name={o.src_host_name} />}
                  {o.src_host_ip && <div className="network-sub mono">{o.src_host_ip}</div>}
                  <div className="network-sub"><RegionName name={o.src_region_name} /></div>
                </td>
                <td>
                  <HostLink id={o.dst_host_id} name={o.dst_host_name} />
                  <div className="network-sub mono">{o.dst_host_ip}</div>
                  <div className="network-sub"><RegionName name={o.dst_region_name} /></div>
                </td>
                <td className="mono">{openingPorts(o)}</td>
                <td>
                  <span className={`badge ${o.status === 'open' ? 'badge-ok' : 'badge-unknown'}`}>
                    {t(`network.status.${o.status}`)}
                  </span>
                </td>
                <td>{o.ticket_no}</td>
                <td>{o.requester}</td>
                <td className="network-purpose">{o.purpose}</td>
                <td>
                  <span className="network-badges">
                    {unused && (
                      <span className="badge badge-warn" title={t('network.openings.unusedHint')}>
                        {t('network.openings.unused')}
                      </span>
                    )}
                    {o.status === 'open' && o.in_use && (
                      <span className="badge badge-ok">{t('network.openings.inUse')}</span>
                    )}
                    <ProvenanceBadges source={o.source} verified={o.verified} />
                  </span>
                </td>
                {canWrite && (
                  <td className="cell-actions">
                    {!o.verified && (
                      <button
                        type="button"
                        className="btn btn-sm btn-success"
                        aria-label={t('network.verifyItem', { name })}
                        onClick={() => verifyOpening.mutate(o.id)}
                        disabled={verifyOpening.isPending}
                      >
                        {t('network.verify')}
                      </button>
                    )}
                    {onEdit && (
                      <button
                        type="button"
                        className="btn btn-sm btn-secondary"
                        aria-label={t('network.editItem', { name })}
                        onClick={() => onEdit(o)}
                      >
                        {t('common.edit')}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      aria-label={t('network.deleteItem', { name })}
                      onClick={() => {
                        if (window.confirm(t('network.openings.deleteConfirm', { name }))) deleteOpening.mutate(o.id);
                      }}
                      disabled={deleteOpening.isPending}
                    >
                      {t('common.delete')}
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface UsagesTableProps {
  usages: PortUsage[];
  label: string;
  showHost?: boolean;
  onEdit?: (usage: PortUsage) => void;
}

export function UsagesTable({ usages, label, showHost = true, onEdit }: UsagesTableProps) {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const { verifyUsage, deleteUsage } = useNetworkRowActions();

  if (usages.length === 0) {
    return <div className="empty-state"><p>{t('network.usages.empty')}</p></div>;
  }

  return (
    <div className="table-container">
      <table className="data-table network-table" aria-label={label}>
        <thead>
          <tr>
            {showHost && <th scope="col">{t('network.usages.host')}</th>}
            <th scope="col">{t('network.usages.port')}</th>
            <th scope="col">{t('network.usages.service')}</th>
            <th scope="col">{t('network.description')}</th>
            <th scope="col">{t('network.owner')}</th>
            <th scope="col">{t('network.flags')}</th>
            {canWrite && <th scope="col">{t('common.actions')}</th>}
          </tr>
        </thead>
        <tbody>
          {usages.map((u) => {
            const isAuto = u.source === 'auto' || u.id == null;
            const name = `${u.host_name}:${u.port} ${u.service_name}`;
            const key = u.id != null ? `id-${u.id}` : `auto-${u.host_id}-${u.protocol}-${u.port}-${u.auto_kind}-${u.service_name}`;
            return (
              <tr key={key}>
                {showHost && (
                  <td>
                    <HostLink id={u.host_id} name={u.host_name} />
                    <div className="network-sub mono">{u.host_ip}</div>
                  </td>
                )}
                <td className="mono">{`${u.protocol}/${u.port}`}</td>
                <td>
                  {u.service_name}
                  {u.auto_kind && <div className="network-sub">{t(`network.autoKind.${u.auto_kind}`, { defaultValue: u.auto_kind })}</div>}
                </td>
                <td>{u.description}</td>
                <td>{u.owner}</td>
                <td>
                  <span className="network-badges">
                    <ProvenanceBadges source={u.source} verified={u.verified} />
                    {u.reachable_from_platform === false && (
                      <span className="badge badge-error" title={t('network.usages.unreachableHint')}>
                        {t('network.usages.unreachable')}
                      </span>
                    )}
                  </span>
                </td>
                {canWrite && (
                  <td className="cell-actions">
                    {isAuto ? (
                      <span className="network-muted">{t('network.usages.readOnly')}</span>
                    ) : (
                      <>
                        {!u.verified && (
                          <button
                            type="button"
                            className="btn btn-sm btn-success"
                            aria-label={t('network.verifyItem', { name })}
                            onClick={() => verifyUsage.mutate(u.id!)}
                            disabled={verifyUsage.isPending}
                          >
                            {t('network.verify')}
                          </button>
                        )}
                        {onEdit && (
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            aria-label={t('network.editItem', { name })}
                            onClick={() => onEdit(u)}
                          >
                            {t('common.edit')}
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          aria-label={t('network.deleteItem', { name })}
                          onClick={() => {
                            if (window.confirm(t('network.usages.deleteConfirm', { name }))) deleteUsage.mutate(u.id!);
                          }}
                          disabled={deleteUsage.isPending}
                        >
                          {t('common.delete')}
                        </button>
                      </>
                    )}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
