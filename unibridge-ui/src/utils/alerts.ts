import type { AlertMute, AlertStatus } from '../api/client';

/*
 * Two separate namespaces live in this file — don't cross them:
 *   rule identifiers   (`AlertStatus.type`, history `rule_type`) e.g. "external_service_down"
 *   mute resource types (`AlertStatus.resource_type`)            e.g. "service"
 * The mapping between them belongs to the backend, which sends the mute key on
 * every status row; nothing here re-derives it.
 */

/** Every alert rule the backend can emit, in the order shown in filters. */
export const RULE_TYPES = [
  'db_health',
  's3_health',
  'nas_health',
  'upstream_health',
  'route_error_rate',
  'server_down',
  'server_disk',
  'server_disk_forecast',
  'server_cpu',
  'server_mem',
  'server_gpu_down',
  'server_gpu_util',
  'server_gpu_mem',
  'server_gpu_underutil',
  'external_service_down',
] as const;

export const SERVER_DOWN_RULE = 'server_down';
export const EXTERNAL_SERVICE_DOWN_RULE = 'external_service_down';

const RULE_LABEL_KEYS: Record<string, string> = {
  db_health: 'alerts.typeDbHealth',
  s3_health: 'alerts.typeS3Health',
  nas_health: 'alerts.typeNasHealth',
  upstream_health: 'alerts.typeUpstreamHealth',
  error_rate: 'alerts.typeErrorRate',
  route_error_rate: 'alerts.typeRouteErrorRate',
  server_down: 'alerts.typeServerDown',
  server_disk: 'alerts.typeServerDisk',
  server_disk_forecast: 'alerts.typeServerDiskForecast',
  server_cpu: 'alerts.typeServerCpu',
  server_mem: 'alerts.typeServerMem',
  server_gpu_down: 'alerts.typeServerGpuDown',
  server_gpu_util: 'alerts.typeServerGpuUtil',
  server_gpu_mem: 'alerts.typeServerGpuMem',
  server_gpu_underutil: 'alerts.typeServerGpuUnderutil',
  external_service_down: 'alerts.typeExternalServiceDown',
};

/** Human label for a rule identifier; unknown rules render as-is. */
export function ruleTypeLabel(t: (key: string) => string, type: string): string {
  const key = RULE_LABEL_KEYS[type];
  return key ? t(key) : type;
}

export interface MuteKey {
  resource_type: string;
  resource_id: string;
}

/** Longest mute the backend accepts (`alert_mutes.MAX_MUTE_DAYS`). */
export const MAX_MUTE_DAYS = 30;

/**
 * Mute key for a status row, or null when the row cannot be muted.
 *
 * Read strictly from the explicit pair the backend sends — never inferred.
 * `type` is a rule id from the other namespace and `target` is a display label
 * (e.g. "checkout (r-1)"), so a key built from those would address the wrong
 * resource or be rejected outright. The pair is null together for a rule with
 * no mutable resource, and absent together on a backend predating mutes; both
 * mean the same thing here — no mute button.
 */
export function muteKeyFor(item: AlertStatus): MuteKey | null {
  const { resource_type: resourceType, resource_id: resourceId } = item;
  if (!resourceType || !resourceId) return null;
  return { resource_type: resourceType, resource_id: resourceId };
}

/** True when `value` is a timestamp still in the future. */
export function isMuteActive(value: string | null | undefined, now: number = Date.now()): boolean {
  if (!value) return false;
  const until = new Date(value).getTime();
  return !Number.isNaN(until) && until > now;
}

export interface RowMuteState {
  muted: boolean;
  mutedUntil: string | null;
}

/**
 * The row's own per-target mute, or null when it has none.
 *
 * A row reads as muted while a global mute is in force even though nothing
 * per-target exists, so this is what separates "has a mute to delete" from
 * "only the global mute is silencing it" — deleting the latter is a no-op the
 * backend answers 204 to, leaving the row muted.
 */
export function ownMuteFor(
  item: AlertStatus,
  mutes: AlertMute[] = [],
  now: number = Date.now(),
): AlertMute | null {
  const key = muteKeyFor(item);
  if (!key) return null;
  return (
    mutes.find(
      (m) =>
        m.resource_type === key.resource_type &&
        m.resource_id === key.resource_id &&
        isMuteActive(m.muted_until, now),
    ) ?? null
  );
}

/**
 * Mute state for one status row. The row's own `muted` flag wins when the
 * backend sends one; otherwise it is derived from the mute list so the page
 * still works against a backend that only exposes `/admin/alerts/mutes`.
 */
export function resolveRowMute(
  item: AlertStatus,
  mutes: AlertMute[] = [],
  now: number = Date.now(),
): RowMuteState {
  if (item.muted !== undefined) {
    return { muted: item.muted, mutedUntil: item.muted_until ?? null };
  }
  const own = ownMuteFor(item, mutes, now);
  return { muted: Boolean(own), mutedUntil: own?.muted_until ?? null };
}

export interface AlertBadgeCounts {
  /** Firing alerts that would still notify. */
  firing: number;
  /** Firing alerts suppressed by a mute. */
  mutedFiring: number;
}

/**
 * Counts behind the sidebar badge and the dashboard card: firing alerts split
 * by whether a mute is suppressing them.
 */
export function alertBadgeCounts(
  items: AlertStatus[] | undefined,
  mutes: AlertMute[] = [],
  now: number = Date.now(),
): AlertBadgeCounts {
  let firing = 0;
  let mutedFiring = 0;
  for (const item of items ?? []) {
    if (item.status !== 'alert') continue;
    if (resolveRowMute(item, mutes, now).muted) mutedFiring += 1;
    else firing += 1;
  }
  return { firing, mutedFiring };
}

export interface RuleTargetCounts {
  /** Targets tracked by the rule. */
  total: number;
  /** Targets the rule is currently firing on. */
  down: number;
}

/**
 * Tracked-vs-firing counts for a single rule. Mutes are ignored on purpose:
 * these cards report reality, not whether anyone is being paged about it.
 */
export function ruleTargetCounts(
  items: AlertStatus[] | undefined,
  ruleType: string,
): RuleTargetCounts {
  let total = 0;
  let down = 0;
  for (const item of items ?? []) {
    if (item.type !== ruleType) continue;
    total += 1;
    if (item.status === 'alert') down += 1;
  }
  return { total, down };
}
