import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getFirewallOpenings, getNetworkOverview, type RegionLink } from '../../api/network';
import { NETWORK_KEY, regionKey } from './networkUtils';
import { OpeningsTable } from './NetworkTables';
import RegionGraph from './RegionGraph';

interface Axis {
  id: number | null;
  name: string;
}

interface SummaryCardProps {
  value: number;
  label: string;
  tone?: 'warn' | 'alert';
  /** Makes the card a button (e.g. to open the matching list). */
  onClick?: () => void;
}

function SummaryCard({ value, label, tone, onClick }: SummaryCardProps) {
  const active = tone && value > 0;
  const className = `network-summary-card${active ? ` network-summary-card--${tone}` : ''}`;
  const body = (
    <>
      <div className="network-summary-card__value">{value}</div>
      <div className="network-summary-card__label">{label}</div>
    </>
  );
  if (onClick) {
    return (
      <button type="button" className={`${className} network-summary-card--link`} onClick={onClick}>
        {body}
      </button>
    );
  }
  return <div className={className}>{body}</div>;
}

type MapView = 'matrix' | 'graph';

function OverviewTab() {
  const { t } = useTranslation();
  const [, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<{ src: number | null; dst: number | null } | null>(null);
  const [view, setView] = useState<MapView>('matrix');

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
        <SummaryCard
          value={ov.probe_mismatches}
          label={t('network.overview.probeMismatches')}
          tone="warn"
          onClick={() => setSearchParams({ tab: 'probes', mismatch: '1' }, { replace: true })}
        />
      </div>

      <div className="network-toolbar">
        <h2 className="section-title">
          {view === 'matrix' ? t('network.overview.matrixTitle') : t('network.overview.graphTitle')}
        </h2>
        <div className="network-segmented" role="group" aria-label={t('network.overview.viewLabel')}>
          {(['matrix', 'graph'] as const).map((v) => (
            <button
              key={v}
              type="button"
              className={`network-segmented__btn${view === v ? ' network-segmented__btn--active' : ''}`}
              aria-pressed={view === v}
              onClick={() => setView(v)}
            >
              {t(v === 'matrix' ? 'network.overview.viewMatrix' : 'network.overview.viewGraph')}
            </button>
          ))}
        </div>
      </div>
      <p className="page-subtitle">
        {view === 'matrix' ? t('network.overview.matrixHint') : t('network.overview.graphHint')}
      </p>
      {rows.length === 0 ? (
        <div className="empty-state"><p>{t('network.overview.noRegions')}</p></div>
      ) : view === 'graph' ? (
        <RegionGraph
          regions={ov.regions}
          links={ov.links}
          outsideName={t('network.overview.graphOutside')}
          selected={selected}
          onSelect={setSelected}
        />
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
