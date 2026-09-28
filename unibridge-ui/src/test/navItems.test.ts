import { describe, it, expect } from 'vitest';
import { navItems, isNavItemVisible, firstAccessiblePath } from '../components/navItems';

describe('navItems', () => {
  const queryMonitoring = navItems.find((i) => i.to === '/query-monitoring');

  it('registers Query Monitoring in the data section right after Audit Logs', () => {
    expect(queryMonitoring).toMatchObject({
      labelKey: 'nav.queryMonitoring',
      section: 'data',
      permission: ['gateway.monitoring.read', 'gateway.monitoring.self'],
      excludeFromLanding: true,
    });
    const auditIndex = navItems.findIndex((i) => i.to === '/audit-logs');
    expect(navItems[auditIndex + 1]).toBe(queryMonitoring);
  });

  it('shows Query Monitoring to full and self-scoped monitoring viewers only', () => {
    expect(isNavItemVisible(queryMonitoring!, ['gateway.monitoring.read'])).toBe(true);
    expect(isNavItemVisible(queryMonitoring!, ['gateway.monitoring.self'])).toBe(true);
    expect(isNavItemVisible(queryMonitoring!, ['query.execute'])).toBe(false);
  });

  it('keeps self-scoped users landing on Gateway Monitoring', () => {
    // Seeded `user` role: the new data-section item comes first in the array
    // but is excluded from landing selection.
    expect(firstAccessiblePath(['gateway.monitoring.self', 'apikeys.self'])).toBe('/gateway/monitoring');
    expect(firstAccessiblePath(['gateway.monitoring.read'])).toBe('/gateway/monitoring');
  });

  it('still lands on the first visible regular item', () => {
    expect(firstAccessiblePath(['query.audit.read', 'gateway.monitoring.read'])).toBe('/audit-logs');
    expect(firstAccessiblePath([])).toBe('/external/guide');
  });
});
