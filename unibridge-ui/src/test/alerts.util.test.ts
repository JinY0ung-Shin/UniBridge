import { describe, it, expect } from 'vitest';
import type { AlertMute, AlertStatus } from '../api/client';
import {
  alertBadgeCounts,
  isMuteActive,
  muteKeyFor,
  ownMuteFor,
  resolveRowMute,
  ruleTargetCounts,
  ruleTypeLabel,
  RULE_TYPES,
  EXTERNAL_SERVICE_DOWN_RULE,
  SERVER_DOWN_RULE,
} from '../utils/alerts';

const NOW = Date.parse('2026-08-15T00:00:00Z');
const FUTURE = '2026-08-15T01:00:00Z';
const PAST = '2026-08-14T23:00:00Z';

function entry(overrides: Partial<AlertStatus> = {}): AlertStatus {
  return {
    target: 'db1',
    type: 'db_health',
    status: 'alert',
    since: null,
    // the backend sends the mute key on every row; `type`/`target` never key a mute
    resource_type: 'db',
    resource_id: 'db1',
    ...overrides,
  };
}

function mute(overrides: Partial<AlertMute> = {}): AlertMute {
  return {
    resource_type: 'db',
    resource_id: 'db1',
    muted_until: FUTURE,
    created_by: 'admin',
    ...overrides,
  };
}

describe('isMuteActive', () => {
  it('is false for missing values', () => {
    expect(isMuteActive(null, NOW)).toBe(false);
    expect(isMuteActive(undefined, NOW)).toBe(false);
    expect(isMuteActive('', NOW)).toBe(false);
  });

  it('is false for unparseable and past timestamps', () => {
    expect(isMuteActive('not-a-date', NOW)).toBe(false);
    expect(isMuteActive(PAST, NOW)).toBe(false);
  });

  it('is true only while the mute is still in the future', () => {
    expect(isMuteActive(FUTURE, NOW)).toBe(true);
  });
});

describe('muteKeyFor', () => {
  it('never infers a key from the rule id or the display target', () => {
    // both would be wrong: server_down is a rule id, and target is a label
    expect(
      muteKeyFor(entry({
        type: 'server_down', target: 'node-1', resource_type: undefined, resource_id: undefined,
      })),
    ).toBeNull();
  });

  it('reads the explicit resource key, not the display target', () => {
    expect(
      muteKeyFor(entry({ resource_type: 'db', resource_id: 'orders', target: 'Orders DB' })),
    ).toEqual({ resource_type: 'db', resource_id: 'orders' });
  });

  it('returns null when the rule has no mutable resource', () => {
    expect(muteKeyFor(entry({ resource_type: null, resource_id: null }))).toBeNull();
  });

  it('keeps the mute namespace separate from the rule namespace', () => {
    // an external service's rule is external_service_down, but its mute is
    // keyed by resource_type "service" — pass through, never re-derive
    const row = entry({
      type: EXTERNAL_SERVICE_DOWN_RULE,
      target: 'Payments API',
      resource_type: 'service',
      resource_id: 'payments',
    });
    expect(muteKeyFor(row)).toEqual({ resource_type: 'service', resource_id: 'payments' });
    // the rule identifier is still what the history filter and cards match on
    expect(ruleTargetCounts([row], EXTERNAL_SERVICE_DOWN_RULE)).toEqual({ total: 1, down: 1 });
  });
});

describe('resolveRowMute', () => {
  it('trusts the row flag when the backend sends one', () => {
    expect(resolveRowMute(entry({ muted: true, muted_until: FUTURE }), [], NOW)).toEqual({
      muted: true,
      mutedUntil: FUTURE,
    });
    // an explicit false wins over a stale entry in the mute list
    expect(resolveRowMute(entry({ muted: false }), [mute()], NOW).muted).toBe(false);
  });

  it('derives state from the mute list when the row omits it', () => {
    expect(resolveRowMute(entry(), [mute()], NOW)).toEqual({ muted: true, mutedUntil: FUTURE });
  });

  it('ignores expired and non-matching mutes', () => {
    expect(resolveRowMute(entry(), [mute({ muted_until: PAST })], NOW).muted).toBe(false);
    expect(resolveRowMute(entry(), [mute({ resource_id: 'other' })], NOW).muted).toBe(false);
    expect(resolveRowMute(entry(), [], NOW)).toEqual({ muted: false, mutedUntil: null });
  });

  it('reports an un-mutable row as unmuted', () => {
    const row = entry({ resource_type: null, resource_id: null });
    expect(resolveRowMute(row, [mute()], NOW)).toEqual({ muted: false, mutedUntil: null });
  });
});

describe('ownMuteFor', () => {
  it('finds the row\'s own active mute', () => {
    expect(ownMuteFor(entry(), [mute()], NOW)?.muted_until).toBe(FUTURE);
  });

  it('ignores the global mute entry, which is not the row\'s own', () => {
    const globalMute = mute({ resource_type: 'global', resource_id: '' });
    expect(ownMuteFor(entry({ muted: true }), [globalMute], NOW)).toBeNull();
  });

  it('ignores expired and non-matching mutes, and un-mutable rows', () => {
    expect(ownMuteFor(entry(), [mute({ muted_until: PAST })], NOW)).toBeNull();
    expect(ownMuteFor(entry(), [mute({ resource_id: 'other' })], NOW)).toBeNull();
    expect(ownMuteFor(entry({ resource_type: null, resource_id: null }), [mute()], NOW)).toBeNull();
    expect(ownMuteFor(entry(), [], NOW)).toBeNull();
  });
});

describe('alertBadgeCounts', () => {
  it('counts nothing for empty input', () => {
    expect(alertBadgeCounts(undefined)).toEqual({ firing: 0, mutedFiring: 0 });
    expect(alertBadgeCounts([])).toEqual({ firing: 0, mutedFiring: 0 });
  });

  it('ignores healthy rows, including muted ones', () => {
    const items = [
      entry({ target: 'ok-1', status: 'ok' }),
      entry({ target: 'ok-2', status: 'ok', muted: true }),
    ];
    expect(alertBadgeCounts(items, [], NOW)).toEqual({ firing: 0, mutedFiring: 0 });
  });

  it('splits firing rows by mute state', () => {
    const items = [
      entry({ target: 'loud-1' }),
      entry({ target: 'loud-2', type: 'server_down' }),
      entry({ target: 'quiet', muted: true, muted_until: FUTURE }),
    ];
    expect(alertBadgeCounts(items, [], NOW)).toEqual({ firing: 2, mutedFiring: 1 });
  });

  it('honours mutes supplied only through the mute list', () => {
    const items = [
      entry({ target: 'db1', resource_id: 'db1' }),
      entry({ target: 'db2', resource_id: 'db2' }),
    ];
    expect(alertBadgeCounts(items, [mute({ resource_id: 'db1' })], NOW)).toEqual({
      firing: 1,
      mutedFiring: 1,
    });
  });
});

describe('ruleTargetCounts', () => {
  const items = [
    entry({ type: SERVER_DOWN_RULE, target: 'node-1', status: 'ok' }),
    entry({ type: SERVER_DOWN_RULE, target: 'node-2', status: 'alert' }),
    entry({ type: SERVER_DOWN_RULE, target: 'node-3', status: 'alert', muted: true }),
    entry({ type: EXTERNAL_SERVICE_DOWN_RULE, target: 'payments', status: 'ok' }),
    entry({ type: 'server_cpu', target: 'node-1', status: 'alert' }),
  ];

  it('counts tracked and firing targets for one rule', () => {
    expect(ruleTargetCounts(items, SERVER_DOWN_RULE)).toEqual({ total: 3, down: 2 });
  });

  it('counts muted-but-firing targets as down', () => {
    // node-3 is muted; the card reports reality, not notification state
    expect(ruleTargetCounts(items, SERVER_DOWN_RULE).down).toBe(2);
  });

  it('handles rules with no firing targets and unknown rules', () => {
    expect(ruleTargetCounts(items, EXTERNAL_SERVICE_DOWN_RULE)).toEqual({ total: 1, down: 0 });
    expect(ruleTargetCounts(items, 'nope')).toEqual({ total: 0, down: 0 });
    expect(ruleTargetCounts(undefined, SERVER_DOWN_RULE)).toEqual({ total: 0, down: 0 });
  });
});

describe('ruleTypeLabel', () => {
  const t = (key: string) => key;

  it('maps every filterable rule to an i18n key', () => {
    for (const rule of RULE_TYPES) {
      expect(ruleTypeLabel(t, rule)).toMatch(/^alerts\.type/);
    }
  });

  it('falls back to the raw identifier for unknown rules', () => {
    expect(ruleTypeLabel(t, 'brand_new_rule')).toBe('brand_new_rule');
  });
});
