import type { KeycloakUser } from '../api/client';

type RoleBearing = Pick<KeycloakUser, 'role'>;

/**
 * A user with no application role has signed up but not been approved yet —
 * the condition the Users page renders as a "Pending" badge.
 */
export function isPendingUser(user: RoleBearing): boolean {
  return !user.role;
}

export function countPendingUsers(users: RoleBearing[] | undefined): number {
  return (users ?? []).filter(isPendingUser).length;
}
