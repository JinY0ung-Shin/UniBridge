import { describe, expect, it, vi } from 'vitest';

type KeycloakMock = {
  authenticated: boolean;
  token: string;
  updateToken: ReturnType<typeof vi.fn>;
  login: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};

function makeKeycloakMock(): KeycloakMock {
  return {
    authenticated: true,
    token: 'token-1',
    updateToken: vi.fn().mockResolvedValue(true),
    login: vi.fn(),
    logout: vi.fn(),
  };
}

async function importClient(keycloak: KeycloakMock) {
  vi.resetModules();
  vi.doMock('../keycloak', () => ({ default: keycloak }));
  return import('../api/client');
}

function rejectedResponse(config: unknown, status: number) {
  return Promise.reject({
    config,
    response: { status, data: {}, headers: {}, config, statusText: String(status) },
    isAxiosError: true,
  });
}

describe('api client authentication interceptor', () => {
  it('does not send requests before auth is ready', async () => {
    const keycloak = makeKeycloakMock();
    const { default: client } = await importClient(keycloak);
    const adapter = vi.fn().mockResolvedValue({
      data: {},
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {},
    });
    client.defaults.adapter = adapter;

    await expect(client.get('/query/databases')).rejects.toThrow('Authentication is not ready');

    expect(adapter).not.toHaveBeenCalled();
    expect(keycloak.updateToken).not.toHaveBeenCalled();
  });

  it('retries one 401 response after refreshing the token', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.updateToken
      .mockResolvedValueOnce(true)
      .mockImplementationOnce(async () => {
        keycloak.token = 'token-2';
        return true;
      });
    const { default: client, setApiAuthReady } = await importClient(keycloak);
    setApiAuthReady(true);

    const adapter = vi
      .fn()
      .mockImplementationOnce((config) => rejectedResponse(config, 401))
      .mockResolvedValueOnce({
        data: { ok: true },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {},
      });
    client.defaults.adapter = adapter;

    const response = await client.get('/query/databases');

    expect(response.data).toEqual({ ok: true });
    expect(keycloak.updateToken).toHaveBeenNthCalledWith(1, 5);
    expect(keycloak.updateToken).toHaveBeenNthCalledWith(2, -1);
    expect(keycloak.logout).not.toHaveBeenCalled();
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(adapter.mock.calls[1][0].headers.Authorization).toBe('Bearer token-2');
  });

  it('does not logout on 403 responses', async () => {
    const keycloak = makeKeycloakMock();
    const { default: client, setApiAuthReady } = await importClient(keycloak);
    setApiAuthReady(true);
    client.defaults.adapter = vi.fn((config) => rejectedResponse(config, 403));

    await expect(client.get('/admin/users')).rejects.toMatchObject({
      response: { status: 403 },
    });

    expect(keycloak.logout).not.toHaveBeenCalled();
  });
});
