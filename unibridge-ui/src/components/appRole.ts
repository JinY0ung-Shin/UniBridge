// Application roles that grant access. A user without any of these has registered
// but not yet been approved by an admin (approval-gated registration).
// Keep in sync with backend ROLE_PRIORITY in unibridge-service/app/auth.py.
export const APP_ROLES = ['admin', 'user'];

export type TokenClaims = { roles?: string[]; realm_access?: { roles?: string[] } } | undefined;

/**
 * Resolve the highest-priority application role from Keycloak token claims, or
 * null if the user has none (pending approval). Reads both the custom `roles`
 * claim (realm-export protocol mapper) and standard `realm_access.roles`.
 */
export function resolveAppRole(parsed: TokenClaims): string | null {
  const roles = new Set<string>([
    ...(parsed?.roles ?? []),
    ...(parsed?.realm_access?.roles ?? []),
  ]);
  return APP_ROLES.find((r) => roles.has(r)) ?? null;
}
