// Shared navigation model + permission helpers, used by both the sidebar
// (Layout) and the landing-page redirect (App). `permission` may be:
//   null        → always visible
//   string      → visible if the user has that permission
//   string[]    → visible if the user has ANY of them (any-of)

export type NavPermission = string | string[] | null;

export interface NavItem {
  to: string;
  labelKey: string;
  icon: string;
  section: string;
  permission: NavPermission;
  /** Hide the item when the user has ANY of these permissions, even if
   *  `permission` matches. The route itself stays reachable by URL. */
  hiddenForPermissions?: string[];
}

export const navItems: NavItem[] = [
  { to: '/', labelKey: 'nav.dashboard', icon: 'Dashboard', section: 'data', permission: 'dashboard.read' },
  { to: '/connections', labelKey: 'nav.connections', icon: 'Connections', section: 'data', permission: 'query.databases.read' },
  { to: '/permissions', labelKey: 'nav.permissions', icon: 'Permissions', section: 'data', permission: 'query.permissions.read' },
  { to: '/audit-logs', labelKey: 'nav.auditLogs', icon: 'Audit Logs', section: 'data', permission: 'query.audit.read' },
  { to: '/query', labelKey: 'nav.queryPlayground', icon: 'Query Playground', section: 'data', permission: 'query.execute' },
  { to: '/query-templates', labelKey: 'nav.queryTemplates', icon: 'Query Templates', section: 'data', permission: 'query.settings.read' },
  { to: '/query-settings', labelKey: 'nav.querySettings', icon: 'Query Settings', section: 'data', permission: 'query.settings.read' },
  { to: '/s3', labelKey: 'nav.s3Connections', icon: 'S3', section: 's3', permission: 's3.connections.read' },
  { to: '/nas', labelKey: 'nav.nasConnections', icon: 'S3', section: 'nas', permission: 'nas.connections.read' },
  { to: '/gateway/routes', labelKey: 'nav.gatewayRoutes', icon: 'Gateway Routes', section: 'gateway', permission: 'gateway.routes.read' },
  { to: '/gateway/upstreams', labelKey: 'nav.gatewayUpstreams', icon: 'Gateway Upstreams', section: 'gateway', permission: 'gateway.upstreams.read' },
  { to: '/gateway/monitoring', labelKey: 'nav.gatewayMonitoring', icon: 'Gateway Monitoring', section: 'gateway', permission: ['gateway.monitoring.read', 'gateway.monitoring.self'] },
  { to: '/llm/monitoring', labelKey: 'nav.llmMonitoring', icon: 'LLM Monitoring', section: 'llm', permission: 'gateway.monitoring.read' },
  { to: '/servers', labelKey: 'nav.servers', icon: 'Gateway Monitoring', section: 'servers', permission: 'servers.read' },
  { to: '/external/monitoring', labelKey: 'nav.externalMonitoring', icon: 'Gateway Monitoring', section: 'servers', permission: 'gateway.monitoring.read' },
  { to: '/external/guide', labelKey: 'nav.metricsGuide', icon: 'Audit Logs', section: 'servers', permission: null },
  { to: '/api-keys', labelKey: 'nav.apiKeys', icon: 'API Keys', section: 'access', permission: 'apikeys.read' },
  // Key administrators manage every key (incl. their own) on /api-keys, so the
  // self-service page only clutters their sidebar.
  { to: '/my-api-key', labelKey: 'nav.myApiKey', icon: 'API Keys', section: 'access', permission: 'apikeys.self', hiddenForPermissions: ['apikeys.read'] },
  { to: '/roles', labelKey: 'nav.roles', icon: 'Roles', section: 'admin', permission: 'admin.roles.read' },
  { to: '/users', labelKey: 'nav.users', icon: 'Users', section: 'admin', permission: 'admin.users.read' },
  { to: '/admin-audit-logs', labelKey: 'nav.adminAuditLogs', icon: 'Audit Logs', section: 'admin', permission: 'admin.audit.read' },
  { to: '/alerts/status', labelKey: 'nav.alertStatus', icon: 'Alert Status', section: 'alerts', permission: 'alerts.read' },
  { to: '/alerts/settings', labelKey: 'nav.alertSettings', icon: 'Alert Settings', section: 'alerts', permission: 'alerts.read' },
  { to: '/alerts/history', labelKey: 'nav.alertHistory', icon: 'Alert History', section: 'alerts', permission: 'alerts.read' },
];

/** Any-of permission check. `null` permission is always visible. */
export function hasNavPermission(permission: NavPermission, perms: string[]): boolean {
  if (permission === null) return true;
  if (Array.isArray(permission)) return permission.some((p) => perms.includes(p));
  return perms.includes(permission);
}

/** Full sidebar visibility: permission match minus hiddenForPermissions. */
export function isNavItemVisible(item: NavItem, perms: string[]): boolean {
  if (!hasNavPermission(item.permission, perms)) return false;
  return !item.hiddenForPermissions?.some((p) => perms.includes(p));
}

/** First non-root nav path the user can access, or null if none. Used as the
 *  landing page for users who cannot see the dashboard. Mirrors sidebar
 *  visibility so users never land on a page their menu doesn't show. */
export function firstAccessiblePath(perms: string[]): string | null {
  const item = navItems.find((i) => i.to !== '/' && isNavItemVisible(i, perms));
  return item ? item.to : null;
}
