import { useQuery } from '@tanstack/react-query';
import { getAlertStatus } from '../api/client';

/** Shared across the sidebar badge, the dashboard cards and the status page so
 *  they read one cache entry instead of each fetching their own copy. */
export const ALERT_STATUS_QUERY_KEY = ['alert-status'];

interface UseAlertStatusOptions {
  /** Skip the request entirely (e.g. the user lacks `alerts.read`). */
  enabled?: boolean;
  refetchIntervalMs?: number;
}

export function useAlertStatusQuery({
  enabled = true,
  refetchIntervalMs = 30_000,
}: UseAlertStatusOptions = {}) {
  return useQuery({
    queryKey: ALERT_STATUS_QUERY_KEY,
    queryFn: getAlertStatus,
    refetchInterval: refetchIntervalMs,
    enabled,
  });
}
