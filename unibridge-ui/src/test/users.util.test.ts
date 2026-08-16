import { describe, it, expect } from 'vitest';
import { countPendingUsers, isPendingUser } from '../utils/users';

describe('isPendingUser', () => {
  it('treats a user with no application role as pending', () => {
    expect(isPendingUser({ role: null })).toBe(true);
    expect(isPendingUser({ role: '' })).toBe(true);
  });

  it('treats any assigned role as approved', () => {
    expect(isPendingUser({ role: 'user' })).toBe(false);
    expect(isPendingUser({ role: 'admin' })).toBe(false);
  });
});

describe('countPendingUsers', () => {
  it('counts only role-less users', () => {
    const users = [
      { role: 'admin' },
      { role: null },
      { role: 'viewer' },
      { role: null },
      { role: '' },
    ];
    expect(countPendingUsers(users)).toBe(3);
  });

  it('handles missing and empty lists', () => {
    expect(countPendingUsers(undefined)).toBe(0);
    expect(countPendingUsers([])).toBe(0);
    expect(countPendingUsers([{ role: 'user' }])).toBe(0);
  });
});
