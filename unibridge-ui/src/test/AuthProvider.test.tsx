import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider } from '../components/AuthProvider';

const keycloakMock = vi.hoisted(() => ({
  init: vi.fn(),
  updateToken: vi.fn(),
  logout: vi.fn(),
  token: 'token-1',
  tokenParsed: {} as Record<string, unknown>,
  authServerUrl: 'https://auth.example.com',
  onAuthRefreshSuccess: undefined as (() => void) | undefined,
  onAuthSuccess: undefined as (() => void) | undefined,
  onTokenExpired: undefined as (() => void) | undefined,
}));

vi.mock('../keycloak', () => ({
  default: keycloakMock,
}));

describe('AuthProvider', () => {
  beforeEach(() => {
    keycloakMock.init.mockReset();
    keycloakMock.updateToken.mockReset();
    keycloakMock.logout.mockReset();
    keycloakMock.init.mockResolvedValue(true);
    keycloakMock.updateToken.mockResolvedValue(true);
    keycloakMock.token = 'token-1';
    keycloakMock.tokenParsed = {
      preferred_username: 'operator',
      roles: ['user'],
    };
    keycloakMock.authServerUrl = 'https://auth.example.com';
    keycloakMock.onAuthRefreshSuccess = undefined;
    keycloakMock.onAuthSuccess = undefined;
    keycloakMock.onTokenExpired = undefined;
  });

  it('announces authentication failures', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    keycloakMock.init.mockRejectedValue(new Error('offline'));

    render(
      <AuthProvider>
        <main>Loaded app</main>
      </AuthProvider>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Authentication service unavailable. Check that Keycloak is reachable at https://auth.example.com',
    );
    expect(screen.queryByText('Loaded app')).not.toBeInTheDocument();

    consoleError.mockRestore();
  });

  it('announces the approval gate as status', async () => {
    keycloakMock.tokenParsed = {
      preferred_username: 'new-user',
      roles: [],
    };

    render(
      <AuthProvider>
        <main>Loaded app</main>
      </AuthProvider>,
    );

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Your registration was received. An administrator must approve your account before you can use the service.',
    );
    expect(screen.getByText('Signed in as new-user')).toBeInTheDocument();
    expect(screen.queryByText('Loaded app')).not.toBeInTheDocument();
  });

  it('renders children after authentication and approval', async () => {
    render(
      <AuthProvider>
        <main>Loaded app</main>
      </AuthProvider>,
    );

    expect(await screen.findByText('Loaded app')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
