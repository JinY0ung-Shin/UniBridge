import type { GatewayRoute } from '../api/client';

/** The path(s) a route matches: APISIX keeps several in `uris` instead of `uri`. */
export function routeUriLabel(route: Pick<GatewayRoute, 'uri' | 'uris'>): string {
  return route.uri || (route.uris ?? []).join(', ');
}

/**
 * Whether an API-key grant on the route means anything. A system route without
 * key-auth (the /api/llm-bi not-found route) answers every request itself.
 */
export function isGrantableRoute(route: Pick<GatewayRoute, 'system' | 'require_auth'>): boolean {
  return !route.system || Boolean(route.require_auth);
}
