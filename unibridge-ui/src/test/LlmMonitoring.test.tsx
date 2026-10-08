vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Cell: () => null,
  LabelList: () => null,
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getLlmSummary: vi.fn(),
  getLlmTokens: vi.fn(),
  getLlmByModel: vi.fn(),
  getLlmTopKeys: vi.fn(),
  getLlmErrors: vi.fn(),
  getLlmStatusCodes: vi.fn(),
  getLlmRequestsTotal: vi.fn(),
  getLlmByModelSeries: vi.fn(),
  getLlmTopKeysSeries: vi.fn(),
  getApiKeys: vi.fn(),
  createBifrostSsoHandoff: vi.fn(),
}));

// Stable across vi.resetModules() so the dynamically re-imported page sees a
// loaded permission set (the real PermissionContext would be a fresh, empty
// instance after a module reset, disabling the api-keys lookup).
vi.mock('../components/usePermissions', () => ({
  usePermissions: () => ({ permissions: ['apikeys.read', 'gateway.monitoring.read'], loaded: true }),
}));

// Same stability trick for the auth role: the LiteLLM and Bifrost Admin links
// (and GrafanaLink) render for admins only, so tests flip authState.appRole.
const authState = vi.hoisted(() => ({ appRole: 'admin' as string | null }));
vi.mock('../components/useAuth', () => ({
  useAuth: () => ({
    authenticated: true,
    token: null,
    username: 'tester',
    appRole: authState.appRole,
    initialized: true,
    logout: () => {},
  }),
}));

import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getLlmByModel,
  getLlmByModelSeries,
  getLlmErrors,
  getLlmRequestsTotal,
  getLlmStatusCodes,
  getLlmSummary,
  getLlmTokens,
  getLlmTopKeys,
  getLlmTopKeysSeries,
  getApiKeys,
  createBifrostSsoHandoff,
} from '../api/client';
import { renderWithProviders } from './helpers';

const mockedGetLlmSummary = vi.mocked(getLlmSummary);
const mockedGetLlmTokens = vi.mocked(getLlmTokens);
const mockedGetLlmByModel = vi.mocked(getLlmByModel);
const mockedGetLlmTopKeys = vi.mocked(getLlmTopKeys);
const mockedGetLlmErrors = vi.mocked(getLlmErrors);
const mockedGetLlmStatusCodes = vi.mocked(getLlmStatusCodes);
const mockedGetLlmRequestsTotal = vi.mocked(getLlmRequestsTotal);
const mockedGetLlmByModelSeries = vi.mocked(getLlmByModelSeries);
const mockedGetLlmTopKeysSeries = vi.mocked(getLlmTopKeysSeries);
const mockedGetApiKeys = vi.mocked(getApiKeys);
const mockedCreateBifrostSsoHandoff = vi.mocked(createBifrostSsoHandoff);
const emptyBucketedTokens = { buckets: [], series: [], unit: 'tokens' as const };

describe('LlmMonitoring', () => {
  beforeEach(() => {
    mockedGetLlmSummary.mockResolvedValue({
      total_tokens: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      cached_tokens: 0,
      cache_hit_rate: 0,
      estimated_cost: 0,
      total_requests: 0,
      avg_latency_ms: 0,
    });
    mockedGetLlmTokens.mockResolvedValue({ prompt: [], completion: [], cached: [] });
    mockedGetLlmByModel.mockResolvedValue([]);
    mockedGetLlmTopKeys.mockResolvedValue([]);
    mockedGetLlmErrors.mockResolvedValue([]);
    mockedGetLlmStatusCodes.mockResolvedValue([]);
    mockedGetLlmRequestsTotal.mockResolvedValue([]);
    mockedGetLlmByModelSeries.mockResolvedValue(emptyBucketedTokens);
    mockedGetLlmTopKeysSeries.mockResolvedValue(emptyBucketedTokens);
    mockedGetApiKeys.mockResolvedValue([]);
    window.__RUNTIME_CONFIG__ = {
      ...window.__RUNTIME_CONFIG__,
      LITELLM_ADMIN_URL: 'https://localhost:4000/ui',
      BIFROST_ADMIN_URL: 'https://localhost:18443',
    };
    authState.appRole = 'admin';
  });

  afterEach(() => {
    delete window.__RUNTIME_CONFIG__;
    vi.resetModules();
  });

  it('renders the custom range toggle', async () => {
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');
    renderWithProviders(<LlmMonitoring />);
    expect(screen.getByTestId('custom-toggle')).toBeInTheDocument();
  });

  it('links LiteLLM Admin to the separate-origin UI path', async () => {
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'LiteLLM Admin opens in new tab' })).toBeInTheDocument();
    });

    expect(screen.getByRole('link', { name: 'LiteLLM Admin opens in new tab' })).toHaveAttribute(
      'href',
      'https://localhost:4000/ui',
    );
  });

  it('links Bifrost Admin to the bifrost-tls UI origin', async () => {
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    const link = await screen.findByRole('link', { name: 'Bifrost Admin opens in new tab' });
    expect(link).toHaveAttribute('href', 'https://localhost:18443');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('links Bifrost Admin to BIFROST_UI_HOSTNAME on the port this page was opened on', async () => {
    window.__RUNTIME_CONFIG__ = { ...window.__RUNTIME_CONFIG__, BIFROST_UI_HOSTNAME: 'llm-proxy.example.com' };
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    // jsdom serves the page from http://localhost:3000/: the name keeps that
    // port (the UI nginx answers it there) and always uses https.
    expect(window.location.port).toBe('3000');
    const link = await screen.findByRole('link', { name: 'Bifrost Admin opens in new tab' });
    expect(link).toHaveAttribute('href', 'https://llm-proxy.example.com:3000/');
  });

  describe('Bifrost sign-in through UniBridge', () => {
    const BIFROST_HOST_CONFIG = { BIFROST_UI_HOSTNAME: 'llm-proxy.example.com' };

    // A stand-in for the tab window.open returns.
    function fakeTab() {
      return { opener: window as Window | null, location: { href: 'about:blank' } };
    }

    // Clicks the link and reports whether its own navigation was cancelled,
    // then cancels it anyway (jsdom cannot navigate).
    function clickAndReportPrevented(link: HTMLElement): boolean {
      let prevented = false;
      const record = (event: Event) => {
        prevented = event.defaultPrevented;
        event.preventDefault();
      };
      window.addEventListener('click', record);
      fireEvent.click(link);
      window.removeEventListener('click', record);
      return prevented;
    }

    async function renderBifrostLink() {
      const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');
      renderWithProviders(<LlmMonitoring />);
      return screen.findByRole('link', { name: 'Bifrost Admin opens in new tab' });
    }

    beforeEach(() => {
      mockedCreateBifrostSsoHandoff.mockReset();
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it('opens the Bifrost UI signed in through a one-time code', async () => {
      window.__RUNTIME_CONFIG__ = { ...window.__RUNTIME_CONFIG__, ...BIFROST_HOST_CONFIG };
      const tab = fakeTab();
      const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
      mockedCreateBifrostSsoHandoff.mockResolvedValue({ code: 'one-time/code', expires_in: 60 });

      const link = await renderBifrostLink();

      expect(clickAndReportPrevented(link)).toBe(true);
      // The tab opens on the click itself, before the request, so pop-up
      // blockers still count it as the user's.
      expect(open).toHaveBeenCalledWith('about:blank', '_blank');
      expect(tab.opener).toBeNull();
      await waitFor(() => {
        expect(tab.location.href).toBe('https://llm-proxy.example.com:3000/_unibridge/sso?code=one-time%2Fcode');
      });
      expect(mockedCreateBifrostSsoHandoff).toHaveBeenCalledTimes(1);
    });

    it("falls back to Bifrost's login form when the sign-in fails", async () => {
      window.__RUNTIME_CONFIG__ = { ...window.__RUNTIME_CONFIG__, ...BIFROST_HOST_CONFIG };
      const tab = fakeTab();
      vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
      mockedCreateBifrostSsoHandoff.mockRejectedValue(new Error('Request failed with status code 502'));

      const link = await renderBifrostLink();
      clickAndReportPrevented(link);

      await waitFor(() => {
        expect(tab.location.href).toBe('https://llm-proxy.example.com:3000/');
      });
    });

    it('leaves the plain link to a blocked pop-up', async () => {
      window.__RUNTIME_CONFIG__ = { ...window.__RUNTIME_CONFIG__, ...BIFROST_HOST_CONFIG };
      vi.spyOn(window, 'open').mockReturnValue(null);

      const link = await renderBifrostLink();

      expect(clickAndReportPrevented(link)).toBe(false);
      expect(mockedCreateBifrostSsoHandoff).not.toHaveBeenCalled();
    });

    it('is a plain link without BIFROST_UI_HOSTNAME (bifrost-tls port)', async () => {
      const open = vi.spyOn(window, 'open');

      const link = await renderBifrostLink();

      expect(clickAndReportPrevented(link)).toBe(false);
      expect(open).not.toHaveBeenCalled();
      expect(mockedCreateBifrostSsoHandoff).not.toHaveBeenCalled();
    });
  });

  it('hides the LiteLLM and Bifrost Admin links for non-admins', async () => {
    authState.appRole = 'user';
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByTestId('custom-toggle')).toBeInTheDocument();
    });
    expect(screen.queryByRole('link', { name: 'LiteLLM Admin opens in new tab' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Bifrost Admin opens in new tab' })).not.toBeInTheDocument();
  });

  it('renders request count in model usage table', async () => {
    mockedGetLlmByModel.mockResolvedValue([
      {
        model: 'gpt-4',
        tokens: 5000,
        input_tokens: 3000,
        output_tokens: 2000,
        cached_tokens: 1500,
        requests: 25,
        cost: 12.345,
      },
    ]);
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('gpt-4')).toBeInTheDocument();
    });

    expect(screen.getAllByText('Requests').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Input Tokens').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Output Tokens').length).toBeGreaterThan(0);
    expect(screen.getByText('3.0K')).toBeInTheDocument();
    expect(screen.getByText('2.0K')).toBeInTheDocument();
    expect(screen.getByText('5.0K')).toBeInTheDocument();
    expect(screen.getByText('25')).toBeInTheDocument();
  });

  it('renders UniBridge API key usage with input and output tokens', async () => {
    mockedGetLlmTopKeys.mockResolvedValue([
      {
        api_key: 'customer-portal',
        input_tokens: 3000,
        output_tokens: 2000,
        cached_tokens: 800,
        tokens: 5000,
        requests: 25,
        cost: 4.5,
      },
    ]);
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('customer-portal')).toBeInTheDocument();
    });

    expect(screen.getAllByText('API Key').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Input Tokens').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Output Tokens').length).toBeGreaterThan(0);
    expect(screen.getAllByText('3.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('5.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('25').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Cost').length).toBeGreaterThan(0);
    expect(screen.getByText('$4.50')).toBeInTheDocument();
  });

  it('shows the API key description as a tooltip on its name', async () => {
    mockedGetLlmTopKeys.mockResolvedValue([
      { api_key: 'customer-portal', input_tokens: 0, output_tokens: 0, cached_tokens: 0, tokens: 0, requests: 0, cost: 0 },
    ]);
    mockedGetApiKeys.mockResolvedValue([
      { name: 'customer-portal', description: 'Customer support chatbot', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], rate_limit_per_minute: null, owner: null, created_at: null },
    ]);
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    // The key name now also appears as an <option> in the API-key filter, so
    // scope the assertion to the Top API Keys table cell (a <td>).
    await waitFor(() => {
      const cell = screen
        .getAllByText('customer-portal')
        .map((el) => el.closest('td'))
        .find((td): td is HTMLTableCellElement => td != null);
      expect(cell).toHaveAttribute('title', 'Customer support chatbot');
    });
  });
});
