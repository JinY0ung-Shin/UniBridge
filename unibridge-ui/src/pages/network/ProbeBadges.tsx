import { useTranslation } from 'react-i18next';
import type { ProbeSummary } from '../../api/network';
import { formatKST, formatRelativeTime } from '../../utils/time';

const RESULT_BADGE: Record<string, string> = {
  open: 'badge-ok',
  refused: 'badge-info',
  timeout: 'badge-error',
  error: 'badge-unknown',
};

/** open (green) · refused (blue: the path passed, nothing listens) · timeout (red) · error (grey). */
export function ProbeResultBadge({ result, error }: { result: string; error?: string }) {
  const { t } = useTranslation();
  const hint = result === 'error' && error ? error : t(`network.probes.resultHint.${result}`, { defaultValue: '' });
  return (
    <span className={`badge ${RESULT_BADGE[result] ?? 'badge-unknown'}`} title={hint || undefined}>
      {t(`network.probes.result.${result}`, { defaultValue: result })}
    </span>
  );
}

export function MismatchBadge({ mismatch }: { mismatch: string }) {
  const { t } = useTranslation();
  const tone = mismatch === 'recorded_open_but_blocked' ? 'badge-error' : 'badge-warn';
  return (
    <span className={`badge ${tone}`} title={t(`network.probes.mismatchHint.${mismatch}`, { defaultValue: '' }) || undefined}>
      {t(`network.probes.mismatch.${mismatch}`, { defaultValue: mismatch })}
    </span>
  );
}

/** Relative time with the absolute KST time beside it (and in the tooltip). */
export function ProbeTime({ value, inline = false }: { value: string; inline?: boolean }) {
  const { i18n } = useTranslation();
  const absolute = formatKST(value);
  const relative = formatRelativeTime(value, i18n.resolvedLanguage ?? i18n.language ?? 'ko');
  if (inline) return <time dateTime={value} title={absolute}>{relative}</time>;
  return (
    <time dateTime={value} title={absolute}>
      {relative}
      <div className="network-sub">{absolute}</div>
    </time>
  );
}

/** An opening's latest probe: result and when; red when it contradicts the records. */
export function LastProbeBadge({ probe }: { probe: ProbeSummary }) {
  const { t, i18n } = useTranslation();
  const when = formatRelativeTime(probe.observed_at, i18n.resolvedLanguage ?? i18n.language ?? 'ko');
  const resultLabel = t(`network.probes.result.${probe.result}`, { defaultValue: probe.result });
  const tone = probe.mismatch ? 'badge-error' : RESULT_BADGE[probe.result] ?? 'badge-unknown';
  const title = [
    t('network.probes.lastProbeTitle', { src: probe.src_host_name, port: probe.port, time: formatKST(probe.observed_at) }),
    probe.mismatch ? t(`network.probes.mismatchHint.${probe.mismatch}`, { defaultValue: probe.mismatch }) : '',
  ].filter(Boolean).join('\n');
  return (
    <span className={`badge ${tone} network-probe-badge`} title={title}>
      {t('network.probes.lastProbe', { result: resultLabel, time: when })}
    </span>
  );
}
