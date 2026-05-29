import { describe, it, expect } from 'vitest';
import { resolveAppRole } from '../components/appRole';

describe('resolveAppRole (approval gate)', () => {
  it('returns null for undefined claims (no token)', () => {
    expect(resolveAppRole(undefined)).toBeNull();
  });

  it('returns null when only Keycloak default roles are present (pending)', () => {
    expect(resolveAppRole({ roles: ['offline_access', 'uma_authorization'] })).toBeNull();
    expect(resolveAppRole({ realm_access: { roles: ['offline_access', 'default-roles-apihub'] } })).toBeNull();
  });

  it('detects user via the custom roles claim', () => {
    expect(resolveAppRole({ roles: ['offline_access', 'user'] })).toBe('user');
  });

  it('detects user via realm_access.roles only', () => {
    expect(resolveAppRole({ realm_access: { roles: ['uma_authorization', 'user'] } })).toBe('user');
  });

  it('detects admin', () => {
    expect(resolveAppRole({ roles: ['admin'] })).toBe('admin');
  });

  it('prefers admin over user (priority order)', () => {
    expect(resolveAppRole({ roles: ['user', 'admin'] })).toBe('admin');
    expect(resolveAppRole({ realm_access: { roles: ['user'] }, roles: ['admin'] })).toBe('admin');
  });

  it('unions both claim shapes', () => {
    expect(resolveAppRole({ roles: ['offline_access'], realm_access: { roles: ['user'] } })).toBe('user');
  });

  it('returns null for empty role arrays', () => {
    expect(resolveAppRole({ roles: [], realm_access: { roles: [] } })).toBeNull();
  });
});
