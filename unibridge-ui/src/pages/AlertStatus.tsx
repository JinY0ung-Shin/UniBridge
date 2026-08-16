import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  deleteAlertMute,
  getAlertMutes,
  setAlertMute,
  GLOBAL_MUTE_RESOURCE_TYPE,
  type AlertStatus as AlertStatusEntry,
} from '../api/client';
import { useAlertStatusQuery, ALERT_STATUS_QUERY_KEY } from '../components/useAlertStatus';
import { useCanWrite } from '../components/useCanWrite';
import { useToast } from '../components/useToast';
import ResourceModal from '../components/ResourceModal';
import {
  alertBadgeCounts,
  isMuteActive,
  muteKeyFor,
  ownMuteFor,
  resolveRowMute,
  ruleTypeLabel,
  MAX_MUTE_DAYS,
  type MuteKey,
} from '../utils/alerts';
import { epochToKstLocal, formatKST, kstLocalToUtcIso } from '../utils/time';
import './AlertStatus.css';

const MUTE_MUTES_QUERY_KEY = ['alert-mutes'];

const GLOBAL_MUTE_KEY: MuteKey = {
  resource_type: GLOBAL_MUTE_RESOURCE_TYPE,
  resource_id: '',
};

const PRESET_HOURS = { '1h': 1, '8h': 8, '24h': 24 } as const;
type MuteDuration = keyof typeof PRESET_HOURS | 'custom';

const DURATION_OPTIONS: Array<{ value: MuteDuration; labelKey: string }> = [
  { value: '1h', labelKey: 'alerts.muteDuration1h' },
  { value: '8h', labelKey: 'alerts.muteDuration8h' },
  { value: '24h', labelKey: 'alerts.muteDuration24h' },
  { value: 'custom', labelKey: 'alerts.muteDurationCustom' },
];

function severityLabel(t: (k: string) => string, severity: string | null): string {
  if (severity === 'critical') return t('alerts.severityCritical');
  if (severity === 'warning') return t('alerts.severityWarning');
  return '';
}

function formatDuration(ts: string | null): string {
  if (!ts) return '';
  const start = new Date(ts).getTime();
  if (Number.isNaN(start)) return '';
  const diffMs = Date.now() - start;
  if (diffMs < 0) return '';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ${min % 60}m`;
  const day = Math.floor(hr / 24);
  return `${day}d ${hr % 24}h`;
}

function hoursFromNowIso(hours: number): string {
  return new Date(Date.now() + hours * 3_600_000).toISOString();
}

function muteErrorDetail(err: unknown): string | undefined {
  if (err && typeof err === 'object' && 'response' in err) {
    const axiosErr = err as { response?: { data?: { detail?: unknown } } };
    const detail = axiosErr.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return undefined;
}

interface MuteModalProps {
  title: string;
  pending: boolean;
  onClose: () => void;
  onSubmit: (mutedUntil: string) => void;
}

function MuteModal({ title, pending, onClose, onSubmit }: MuteModalProps) {
  const { t } = useTranslation();
  const [duration, setDuration] = useState<MuteDuration>('1h');
  const [customLocal, setCustomLocal] = useState(() =>
    epochToKstLocal(Math.floor(Date.now() / 1000) + 3600),
  );
  const [error, setError] = useState('');

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const mutedUntil =
      duration === 'custom' ? kstLocalToUtcIso(customLocal) : hoursFromNowIso(PRESET_HOURS[duration]);
    if (!mutedUntil || new Date(mutedUntil).getTime() <= Date.now()) {
      setError(t('alerts.muteInvalidTime'));
      return;
    }
    // Mirrors the backend cap so an over-long window fails here with a clear
    // message instead of as a 422.
    if (new Date(mutedUntil).getTime() > Date.now() + MAX_MUTE_DAYS * 86_400_000) {
      setError(t('alerts.muteTooLong', { days: MAX_MUTE_DAYS }));
      return;
    }
    setError('');
    onSubmit(mutedUntil);
  }

  return (
    <ResourceModal title={title} onClose={onClose} closeLabel={t('common.close')}>
      <form onSubmit={handleSubmit}>
        <div className="form-grid">
          <div className="form-group form-group--full">
            <label htmlFor="alert-mute-duration">{t('alerts.muteDuration')}</label>
            <select
              id="alert-mute-duration"
              value={duration}
              aria-label={t('alerts.muteDuration')}
              onChange={(event) => setDuration(event.target.value as MuteDuration)}
            >
              {DURATION_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {t(option.labelKey)}
                </option>
              ))}
            </select>
          </div>
          {duration === 'custom' && (
            <div className="form-group form-group--full">
              <label htmlFor="alert-mute-until">{t('alerts.muteCustomUntil')}</label>
              <input
                id="alert-mute-until"
                type="datetime-local"
                value={customLocal}
                aria-label={t('alerts.muteCustomUntil')}
                aria-describedby="alert-mute-until-hint"
                onChange={(event) => setCustomLocal(event.target.value)}
              />
              <span id="alert-mute-until-hint" className="form-hint">
                {t('alerts.muteCustomHint')}
              </span>
            </div>
          )}
        </div>

        {error && <div className="form-error" role="alert">{error}</div>}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn btn-primary" disabled={pending} aria-busy={pending}>
            {pending ? t('common.saving') : t('alerts.muteConfirm')}
          </button>
        </div>
      </form>
    </ResourceModal>
  );
}

interface MuteCellProps {
  entry: AlertStatusEntry;
  muted: boolean;
  mutedUntil: string | null;
  /** False for rules the backend reports as having no mutable resource. */
  mutable: boolean;
  /** True when the row has a per-target mute of its own to delete. */
  hasOwnMute: boolean;
  /** True while a global mute is in force. */
  globalMuted: boolean;
  canWrite: boolean;
  busy: boolean;
  onMute: () => void;
  onUnmute: () => void;
}

function MuteCell({
  entry, muted, mutedUntil, mutable, hasOwnMute, globalMuted, canWrite, busy, onMute, onUnmute,
}: MuteCellProps) {
  const { t } = useTranslation();
  const target = entry.target || '*';
  // Muted purely by the global mute: there is no per-target row to delete, so
  // unmuting here would silently no-op and leave the row muted. Without a
  // global mute in force a muted row must own its mute, whether or not the
  // mute list has loaded, so the action stays available.
  const globalOnly = muted && globalMuted && !hasOwnMute;
  const hintId = `mute-hint-${entry.type}-${entry.resource_id ?? entry.target}`;

  return (
    <div className="mute-cell">
      {muted && (
        <span className="mute-chip">
          {mutedUntil ? t('alerts.mutedUntil', { time: formatKST(mutedUntil) }) : t('alerts.muted')}
        </span>
      )}
      {canWrite && mutable && (
        <>
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            aria-label={t(muted ? 'alerts.unmuteTargetTitle' : 'alerts.muteTargetTitle', { target })}
            aria-describedby={globalOnly ? hintId : undefined}
            disabled={busy || globalOnly}
            aria-busy={busy}
            onClick={muted ? onUnmute : onMute}
          >
            {muted ? t('alerts.unmuteAction') : t('alerts.muteAction')}
          </button>
          {globalOnly && (
            <span id={hintId} className="mute-hint">
              {t('alerts.muteGlobalOnlyHint')}
            </span>
          )}
        </>
      )}
    </div>
  );
}

function AlertStatus() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('alerts.write');

  // The dedicated page refreshes faster than the sidebar badge, but shares the
  // badge's cache entry so both always show the same snapshot.
  const statusQuery = useAlertStatusQuery({ refetchIntervalMs: 15_000 });
  const mutesQuery = useQuery({ queryKey: MUTE_MUTES_QUERY_KEY, queryFn: getAlertMutes });

  const [muteTarget, setMuteTarget] = useState<{ key: MuteKey; title: string } | null>(null);

  const entries: AlertStatusEntry[] = statusQuery.data?.items ?? [];
  const mutes = mutesQuery.data?.mutes ?? [];
  const globalMutedUntil =
    statusQuery.data?.global_muted_until ?? mutesQuery.data?.global_muted_until ?? null;
  const globalMuted = isMuteActive(globalMutedUntil);

  const alerting = entries.filter((e) => e.status === 'alert');
  const healthy = entries.filter((e) => e.status === 'ok');
  const counts = alertBadgeCounts(entries, mutes);

  function refreshMuteState() {
    queryClient.invalidateQueries({ queryKey: ALERT_STATUS_QUERY_KEY });
    queryClient.invalidateQueries({ queryKey: MUTE_MUTES_QUERY_KEY });
  }

  const muteMutation = useMutation({
    mutationFn: ({ key, mutedUntil }: { key: MuteKey; mutedUntil: string }) =>
      setAlertMute({ ...key, muted_until: mutedUntil }),
    onSuccess: () => {
      setMuteTarget(null);
      refreshMuteState();
      addToast({ type: 'success', title: t('alerts.muteSaved') });
    },
    onError: (err) =>
      addToast({ type: 'error', title: t('alerts.muteFailed'), message: muteErrorDetail(err) }),
  });

  const unmuteMutation = useMutation({
    mutationFn: (key: MuteKey) => deleteAlertMute(key.resource_type, key.resource_id),
    onSuccess: (_result, key) => {
      refreshMuteState();
      // Dropping a target's own mute does not surface it while the global mute
      // is still running, so say so rather than imply the alert is live again.
      const globalStillApplies =
        key.resource_type !== GLOBAL_MUTE_RESOURCE_TYPE && isMuteActive(globalMutedUntil);
      addToast({
        type: 'success',
        title: t('alerts.muteRemoved'),
        message: globalStillApplies ? t('alerts.muteRemovedGlobalRemains') : undefined,
      });
    },
    onError: (err) =>
      addToast({ type: 'error', title: t('alerts.muteFailed'), message: muteErrorDetail(err) }),
  });

  function isRowBusy(key: MuteKey): boolean {
    const muting =
      muteMutation.isPending &&
      muteMutation.variables?.key.resource_type === key.resource_type &&
      muteMutation.variables?.key.resource_id === key.resource_id;
    const unmuting =
      unmuteMutation.isPending &&
      unmuteMutation.variables?.resource_type === key.resource_type &&
      unmuteMutation.variables?.resource_id === key.resource_id;
    return muting || unmuting;
  }

  function renderMuteCell(entry: AlertStatusEntry) {
    const key = muteKeyFor(entry);
    const { muted, mutedUntil } = resolveRowMute(entry, mutes);
    return (
      <MuteCell
        entry={entry}
        muted={muted}
        mutedUntil={mutedUntil}
        mutable={key !== null}
        hasOwnMute={ownMuteFor(entry, mutes) !== null}
        globalMuted={globalMuted}
        canWrite={canWrite}
        busy={key !== null && isRowBusy(key)}
        onMute={() =>
          key &&
          setMuteTarget({
            key,
            title: t('alerts.muteTargetTitle', { target: entry.target || '*' }),
          })
        }
        onUnmute={() => key && unmuteMutation.mutate(key)}
      />
    );
  }

  return (
    <div className="alert-status">
      <div className="page-header">
        <div>
          <h1>{t('alerts.statusTitle')}</h1>
          <p className="page-subtitle">{t('alerts.statusSubtitle')}</p>
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          aria-label={t('alerts.refreshStatus')}
          title={t('alerts.refreshStatus')}
          onClick={() => statusQuery.refetch()}
          disabled={statusQuery.isFetching}
        >
          {statusQuery.isFetching ? t('common.loading') : t('common.refresh')}
        </button>
      </div>

      {globalMuted ? (
        <div className="global-mute-banner" role="status">
          <span className="global-mute-banner__text">
            {t('alerts.muteGlobalBanner', { time: formatKST(globalMutedUntil) })}
          </span>
          {canWrite && (
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() => unmuteMutation.mutate(GLOBAL_MUTE_KEY)}
              disabled={isRowBusy(GLOBAL_MUTE_KEY)}
              aria-busy={isRowBusy(GLOBAL_MUTE_KEY)}
            >
              {t('alerts.muteGlobalRelease')}
            </button>
          )}
        </div>
      ) : (
        canWrite && (
          <div className="global-mute-bar">
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() =>
                setMuteTarget({ key: GLOBAL_MUTE_KEY, title: t('alerts.muteGlobalTitle') })
              }
            >
              {t('alerts.muteGlobalAction')}
            </button>
          </div>
        )
      )}

      {statusQuery.isLoading && (
        <div className="loading-message" role="status">{t('common.loading')}</div>
      )}

      {statusQuery.isError && (
        <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>
      )}

      {!statusQuery.isLoading && !statusQuery.isError && (
        <>
          <div className="status-summary">
            <div className="status-summary-card status-summary-card--alert">
              <div className="status-summary-count">{counts.firing}</div>
              <div className="status-summary-label">{t('alerts.statusAlerting')}</div>
              {counts.mutedFiring > 0 && (
                <div className="status-summary-note">
                  {t('alerts.muteExcludedCount', { count: counts.mutedFiring })}
                </div>
              )}
            </div>
            <div className="status-summary-card status-summary-card--ok">
              <div className="status-summary-count">{healthy.length}</div>
              <div className="status-summary-label">{t('alerts.statusHealthy')}</div>
            </div>
          </div>

          {/* Alerting */}
          <section className="status-section">
            <h2 className="status-section-title">
              <span className="status-dot status-dot--alert" />
              {t('alerts.statusAlerting')} ({alerting.length})
            </h2>
            {alerting.length === 0 ? (
              <div className="empty-state empty-state--small">
                <p>{t('alerts.statusNoneAlerting')}</p>
              </div>
            ) : (
              <div className="table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">{t('alerts.ruleType')}</th>
                      <th scope="col">{t('alerts.target')}</th>
                      <th scope="col">{t('alerts.statusSince')}</th>
                      <th scope="col">{t('alerts.statusDuration')}</th>
                      <th scope="col">{t('alerts.muteColumn')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {alerting.map((e, i) => (
                      <tr
                        key={`${e.type}:${e.target}:${i}`}
                        className={resolveRowMute(e, mutes).muted ? 'status-row--muted' : ''}
                      >
                        <td>
                          <span className={`rule-type-badge rule-type-badge--${e.type}`}>
                            {ruleTypeLabel(t, e.type)}
                          </span>
                          {e.severity && severityLabel(t, e.severity) && (
                            <span className={`severity-badge severity-badge--${e.severity}`}>
                              {severityLabel(t, e.severity)}
                            </span>
                          )}
                        </td>
                        <td className="cell-target">{e.target || '*'}</td>
                        <td className="cell-timestamp">{formatKST(e.since)}</td>
                        <td className="cell-duration">{formatDuration(e.since)}</td>
                        <td>{renderMuteCell(e)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Healthy */}
          <section className="status-section">
            <h2 className="status-section-title">
              <span className="status-dot status-dot--ok" />
              {t('alerts.statusHealthy')} ({healthy.length})
            </h2>
            {healthy.length === 0 ? (
              <div className="empty-state empty-state--small">
                <p>{t('alerts.statusNoneHealthy')}</p>
              </div>
            ) : (
              <div className="table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">{t('alerts.ruleType')}</th>
                      <th scope="col">{t('alerts.target')}</th>
                      <th scope="col">{t('alerts.muteColumn')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {healthy.map((e, i) => (
                      <tr
                        key={`${e.type}:${e.target}:${i}`}
                        className={resolveRowMute(e, mutes).muted ? 'status-row--muted' : ''}
                      >
                        <td>
                          <span className={`rule-type-badge rule-type-badge--${e.type}`}>
                            {ruleTypeLabel(t, e.type)}
                          </span>
                        </td>
                        <td className="cell-target">{e.target || '*'}</td>
                        <td>{renderMuteCell(e)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      {muteTarget && (
        <MuteModal
          title={muteTarget.title}
          pending={muteMutation.isPending}
          onClose={() => setMuteTarget(null)}
          onSubmit={(mutedUntil) => muteMutation.mutate({ key: muteTarget.key, mutedUntil })}
        />
      )}
    </div>
  );
}

export default AlertStatus;
