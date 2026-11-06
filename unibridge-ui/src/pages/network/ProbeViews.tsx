import { useTranslation } from 'react-i18next';
import type { Probe } from '../../api/network';
import { HostLink, RegionName } from './NetworkTables';
import { MismatchBadge, ProbeResultBadge, ProbeTime } from './ProbeBadges';

const EXPECTED_BADGE: Record<string, string> = {
  open: 'badge-ok',
  blocked: 'badge-error',
  unknown: 'badge-warn',
};

interface ProbesTableProps {
  probes: Probe[];
  label: string;
}

export function ProbesTable({ probes, label }: ProbesTableProps) {
  const { t } = useTranslation();
  if (probes.length === 0) {
    return <div className="empty-state"><p>{t('network.probes.empty')}</p></div>;
  }
  return (
    <div className="table-container">
      <table className="data-table network-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">{t('network.probes.path')}</th>
            <th scope="col">{t('network.probes.resultColumn')}</th>
            <th scope="col">{t('network.probes.expected')}</th>
            <th scope="col">{t('network.probes.mismatchColumn')}</th>
            <th scope="col">{t('network.probes.latency')}</th>
            <th scope="col">{t('network.probes.observedAt')}</th>
            <th scope="col">{t('network.probes.reportedBy')}</th>
          </tr>
        </thead>
        <tbody>
          {probes.map((p) => (
            <tr key={p.id} className={p.mismatch ? 'network-row--mismatch' : undefined}>
              <td>
                <span className="network-probe-path">
                  <HostLink id={p.src_host_id} name={p.src_host_name} />
                  <span className="network-arrow" aria-hidden="true">→</span>
                  <HostLink id={p.dst_host_id} name={p.dst_host_name} />
                  <span className="mono">{`:${p.port}`}</span>
                </span>
                <div className="network-sub">
                  <span className="mono">{`${p.src_host_ip} → ${p.dst_host_ip} ${p.protocol}/${p.port}`}</span>
                </div>
                <div className="network-sub">
                  <RegionName name={p.src_region_name} /> → <RegionName name={p.dst_region_name} />
                </div>
              </td>
              <td>
                <ProbeResultBadge result={p.result} error={p.error} />
                {p.result === 'refused' && <div className="network-sub">{t('network.probes.resultHint.refused')}</div>}
                {p.result === 'error' && p.error && <div className="network-sub">{p.error}</div>}
              </td>
              <td>
                <span
                  className={`badge ${EXPECTED_BADGE[p.expected] ?? 'badge-unknown'}`}
                  title={t(`network.check.reason.${p.expected_reason}`, { defaultValue: p.expected_reason })}
                >
                  {t(`network.check.status.${p.expected}`, { defaultValue: p.expected })}
                </span>
              </td>
              <td>
                {p.mismatch
                  ? <MismatchBadge mismatch={p.mismatch} />
                  : <span className="network-muted">—</span>}
              </td>
              <td className="mono">{p.latency_ms != null ? `${p.latency_ms.toFixed(1)} ms` : '—'}</td>
              <td><ProbeTime value={p.observed_at} /></td>
              <td>{p.reported_by ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
