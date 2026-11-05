import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getFirewallOpenings, getNetworkOverview, type RegionLink } from '../../api/network';
import { NETWORK_KEY, regionKey } from './networkUtils';
import { OpeningsTable } from './NetworkTables';

interface Axis {
  id: number | null;
  name: string;
}

interface SummaryCardProps {
  value: number;
  label: string;
  tone?: 'warn' | 'alert';
}

function SummaryCard({ value, label, tone }: SummaryCardProps) {
  const active = tone && value > 0;
  return (
    <div className={`network-summary-card${active ? ` network-summary-card--${tone}` : ''}`}>
      <div className="network-summary-card__value">{value}</div>
      <div className="network-summary-card__label">{label}</div>
    </div>
  );
}

function OverviewTab() {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<{ src: number | null; dst: number | null } | null>(null);

  const overviewQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'overview'],
    queryFn: getNetworkOverview,
  });
  const openingsQuery = useQuery({
    queryKey: [...NETWORK_KEY, 'openings', {}],
    queryFn: () => getFirewallOpenings(),
    enabled: selected != null,
  });

  if (overviewQuery.isLoading) return <div className="loading-message" role="status">{t('common.loading')}</div>;
  if (overviewQuery.isError || !overviewQuery.data) {
    return <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>;
  }
  const ov = overviewQuery.data;

  const links = new Map<string, RegionLink>(
    ov.links.map((l) => [`${regionKey(l.src_region_id)}:${regionKey(l.dst_region_id)}`, l]),
  );
  const outside: Axis = { id: null, name: t('network.overview.outsideRegions') };
  const regionAxes: Axis[] = ov.regions.map((r) => ({ id: r.id, name: r.name }));
  const rows = ov.links.some((l) => l.src_region_id == null) ? [...regionAxes, outside] : regionAxes;
  const cols = ov.links.some((l) => l.dst_region_id == null) ? [...regionAxes, outside] : regionAxes;

  const selectedOpenings = selected
    ? (openingsQuery.data ?? []).filter(
        (o) => o.src_region_id === selected.src && o.dst_region_id === selected.dst,
      )
    : [];
  const axisName = (id: number | null) => (id == null ? outside.name : ov.regions.find((r) => r.id === id)?.name ?? `#${id}`);

  return (
    <div>
      {ov.auto_sources_failed.length > 0 && (
        <div className="error-banner network-banner" role="alert">
          {t('network.overview.autoFailed', { sources: ov.auto_sources_failed.join(', ') })}
        </div>
      )}
      {ov.platform_host_id == null && (
        <div className="network-note">{t('network.overview.noPlatformHost')}</div>
      )}

      <div className="network-summary">
        <SummaryCard value={ov.host_count} label={t('network.overview.hosts')} />
        <SummaryCard value={ov.unassigned_hosts} label={t('network.overview.unassignedHosts')} tone="warn" />
        <SummaryCard value={ov.opening_count} label={t('network.overview.openings')} />
        <SummaryCard value={ov.unverified_count} label={t('network.overview.unverified')} tone="warn" />
        <SummaryCard value={ov.unused_openings} label={t('network.overview.unusedOpenings')} tone="warn" />
        <SummaryCard value={ov.unreachable_usages} label={t('network.overview.unreachableUsages')} tone="alert" />
      </div>

      <h2 className="section-title">{t('network.overview.matrixTitle')}</h2>
      <p className="page-subtitle">{t('network.overview.matrixHint')}</p>
      {rows.length === 0 ? (
        <div className="empty-state"><p>{t('network.overview.noRegions')}</p></div>
      ) : (
        <div className="table-container">
          <table className="data-table network-matrix" aria-label={t('network.overview.matrixTitle')}>
            <thead>
              <tr>
                <th scope="col">{t('network.overview.srcDst')}</th>
                {cols.map((c) => <th key={regionKey(c.id)} scope="col">{c.name}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={regionKey(r.id)}>
                  <th scope="row">{r.name}</th>
                  {cols.map((c) => {
                    const cellKey = `${regionKey(r.id)}:${regionKey(c.id)}`;
                    if (r.id != null && r.id === c.id) {
                      return (
                        <td key={cellKey} className="network-matrix__same">{t('network.overview.sameRegion')}</td>
                      );
                    }
                    const link = links.get(cellKey);
                    if (!link) {
                      return <td key={cellKey} className="network-matrix__none">—</td>;
                    }
                    const isSelected = selected?.src === r.id && selected?.dst === c.id;
                    return (
                      <td key={cellKey} className={isSelected ? 'network-matrix__cell--selected' : undefined}>
                        <button
                          type="button"
                          className="network-matrix__cell"
                          title={link.ports.join(', ')}
                          aria-pressed={isSelected}
                          aria-label={t('network.overview.cellLabel', {
                            src: r.name, dst: c.name, count: link.open_openings, ports: link.ports.join(', '),
                          })}
                          onClick={() => setSelected(isSelected ? null : { src: r.id, dst: c.id })}
                        >
                          <span className="network-matrix__count">{link.open_openings}</span>
                          {link.openings !== link.open_openings && (
                            <span className="network-sub">{t('network.overview.ofTotal', { count: link.openings })}</span>
                          )}
                          {link.unverified > 0 && (
                            <span className="badge badge-warn">{t('network.overview.unverifiedCount', { count: link.unverified })}</span>
                          )}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <section className="network-section" aria-labelledby="network-selected-title">
          <h2 id="network-selected-title" className="section-title">
            {t('network.overview.selectedTitle', { src: axisName(selected.src), dst: axisName(selected.dst) })}
          </h2>
          {openingsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
          {openingsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
          {openingsQuery.isSuccess && (
            <OpeningsTable openings={selectedOpenings} label={t('network.overview.selectedTitle', { src: axisName(selected.src), dst: axisName(selected.dst) })} />
          )}
        </section>
      )}
    </div>
  );
}

export default OverviewTab;
