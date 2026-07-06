import { screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import type { ReactElement } from 'react';
import GrafanaLink from '../components/GrafanaLink';
import { AuthContext } from '../components/AuthContext';
import { renderWithProviders } from './helpers';

// GrafanaLink renders only for admins (Grafana SSO is admin-only), so each
// test supplies the auth context explicitly.
const withRole = (appRole: string | null, ui: ReactElement) => (
  <AuthContext.Provider
    value={{
      authenticated: true,
      token: null,
      username: null,
      appRole,
      initialized: true,
      logout: () => {},
    }}
  >
    {ui}
  </AuthContext.Provider>
);

describe('GrafanaLink', () => {
  it('builds the dashboard URL with carried time and drops empty vars', () => {
    renderWithProviders(
      withRole(
        'admin',
        <GrafanaLink
          dashboard="unibridge-gateway"
          time={{ kind: 'preset', value: '24h' }}
          vars={{ 'var-route': 'query-api', 'var-consumer': '' }}
        />,
      ),
    );

    const link = screen.getByRole('link', { name: /open in grafana/i });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    const href = link.getAttribute('href')!;
    expect(href).toContain('/grafana/d/unibridge-gateway');
    expect(href).toContain('from=now-24h');
    expect(href).toContain('to=now');
    expect(href).toContain('var-route=query-api');
    expect(href).not.toContain('var-consumer');
  });

  it('maps a custom range to epoch-millisecond from/to', () => {
    renderWithProviders(
      withRole(
        'admin',
        <GrafanaLink
          dashboard="unibridge-overview"
          time={{ kind: 'custom', start: 1_700_000_000, end: 1_700_003_600 }}
        />,
      ),
    );

    const href = screen.getByRole('link').getAttribute('href')!;
    expect(href).toContain('from=1700000000000');
    expect(href).toContain('to=1700003600000');
  });

  it('omits the query string entirely without time or vars', () => {
    renderWithProviders(withRole('admin', <GrafanaLink dashboard="unibridge-servers" />));

    expect(screen.getByRole('link')).toHaveAttribute('href', '/grafana/d/unibridge-servers');
  });

  it('renders nothing for non-admin users (Grafana SSO would reject them)', () => {
    renderWithProviders(withRole('user', <GrafanaLink dashboard="unibridge-servers" />));

    expect(screen.queryByRole('link')).toBeNull();
  });
});
