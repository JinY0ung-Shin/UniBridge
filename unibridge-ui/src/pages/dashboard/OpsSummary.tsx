import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { getUsers } from '../../api/client';
import { usePermissions } from '../../components/usePermissions';
import { useAlertStatusQuery } from '../../components/useAlertStatus';
import {
  alertBadgeCounts,
  isMuteActive,
  ruleTargetCounts,
  EXTERNAL_SERVICE_DOWN_RULE,
  SERVER_DOWN_RULE,
} from '../../utils/alerts';
import { countPendingUsers } from '../../utils/users';
import './OpsSummary.css';

type CardTone = 'neutral' | 'good' | 'bad';

const TONE_COLOR: Record<CardTone, string | undefined> = {
  neutral: undefined,
  good: 'var(--accent-green)',
  bad: 'var(--accent-red)',
};

interface OpsCardProps {
  to: string;
  label: string;
  value: number;
  sub?: ReactNode;
  tone?: CardTone;
  isLoading: boolean;
  isError: boolean;
}

function OpsCard({ to, label, value, sub, tone = 'neutral', isLoading, isError }: OpsCardProps) {
  const { t } = useTranslation();

  return (
    <Link to={to} className="summary-card ops-card">
      <div className="summary-card__value" style={{ color: isError ? undefined : TONE_COLOR[tone] }}>
        {isLoading ? '…' : isError ? '—' : value}
      </div>
      <div className="summary-card__label">{label}</div>
      {isError ? (
        <div className="ops-card__sub ops-card__sub--error" role="alert">
          {t('dashboard.opsLoadFailed')}
        </div>
      ) : (
        !isLoading && sub && <div className="ops-card__sub">{sub}</div>
      )}
    </Link>
  );
}

function OpsSummary() {
  const { t } = useTranslation();
  const { permissions } = usePermissions();
  const canReadAlerts = permissions.includes('alerts.read');
  const canReadUsers = permissions.includes('admin.users.read');

  const statusQuery = useAlertStatusQuery({ enabled: canReadAlerts });
  const usersQuery = useQuery({
    queryKey: ['users', ''],
    queryFn: () => getUsers(),
    // Approvals move on a human timescale, so this polls far slower than the
    // alert cards; mount and window-focus refetches keep it current enough.
    refetchInterval: 300_000,
    enabled: canReadUsers,
  });

  if (!canReadAlerts && !canReadUsers) return null;

  const entries = statusQuery.data?.items ?? [];
  const counts = alertBadgeCounts(entries);
  const globalMuted = isMuteActive(statusQuery.data?.global_muted_until);
  const servers = ruleTargetCounts(entries, SERVER_DOWN_RULE);
  const externals = ruleTargetCounts(entries, EXTERNAL_SERVICE_DOWN_RULE);
  const pendingUsers = countPendingUsers(usersQuery.data?.users);
  const totalUsers = usersQuery.data?.total ?? usersQuery.data?.users?.length ?? 0;

  const alertState = { isLoading: statusQuery.isLoading, isError: statusQuery.isError };
  // The checker only populates state after its first cycle (and emits nothing
  // when Prometheus is unreachable), so an empty list means "no data yet",
  // not "nothing is being watched".
  const awaitingFirstCheck = statusQuery.isSuccess && entries.length === 0;

  function trackedSub(total: number) {
    if (awaitingFirstCheck) return t('dashboard.opsAwaitingData');
    return total === 0 ? t('dashboard.opsNoTargets') : t('dashboard.opsTrackedTotal', { count: total });
  }

  return (
    <>
      <h2 className="section-title">{t('dashboard.opsSummary')}</h2>
      <div className="summary-cards ops-summary">
        {canReadAlerts && (
          <>
            <OpsCard
              to="/alerts/status"
              label={t('dashboard.opsFiringAlerts')}
              value={counts.firing}
              tone={counts.firing > 0 ? 'bad' : 'good'}
              sub={
                globalMuted
                  ? t('dashboard.opsGlobalMuted')
                  : awaitingFirstCheck
                    ? t('dashboard.opsAwaitingData')
                    : counts.mutedFiring > 0
                      ? t('alerts.muteExcludedCount', { count: counts.mutedFiring })
                      : counts.firing === 0
                        ? t('dashboard.opsAllHealthy')
                        : undefined
              }
              {...alertState}
            />
            <OpsCard
              to="/servers"
              label={t('dashboard.opsServers')}
              value={servers.down}
              tone={servers.down > 0 ? 'bad' : 'good'}
              sub={trackedSub(servers.total)}
              {...alertState}
            />
            <OpsCard
              to="/external/monitoring"
              label={t('dashboard.opsExternalServices')}
              value={externals.down}
              tone={externals.down > 0 ? 'bad' : 'good'}
              sub={trackedSub(externals.total)}
              {...alertState}
            />
          </>
        )}
        {canReadUsers && (
          <OpsCard
            to="/users"
            label={t('dashboard.opsPendingUsers')}
            value={pendingUsers}
            tone={pendingUsers > 0 ? 'bad' : 'good'}
            sub={t('dashboard.opsUsersTotal', { count: totalUsers })}
            isLoading={usersQuery.isLoading}
            isError={usersQuery.isError}
          />
        )}
      </div>
    </>
  );
}

export default OpsSummary;
