vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getLlmSummary: vi.fn(),
  getLlmTokens: vi.fn(),
  getLlmByModel: vi.fn(),
  getLlmTopKeys: vi.fn(),
  getLlmErrors: vi.fn(),
  getLlmRequestsTotal: vi.fn(),
  getApiKeys: vi.fn(),
}));

// Stable across vi.resetModules() so the dynamically re-imported page sees a
// loaded permission set (the real PermissionContext would be a fresh, empty
// instance after a module reset, disabling the api-keys lookup).
vi.mock('../components/usePermissions', () => ({
  usePermissions: () => ({ permissions: ['apikeys.read', 'gateway.monitoring.read'], loaded: true }),
}));

import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getLlmByModel,
  getLlmErrors,
  getLlmRequestsTotal,
  getLlmSummary,
  getLlmTokens,
  getLlmTopKeys,
  getApiKeys,
} from '../api/client';
import { renderWithProviders } from './helpers';

const mockedGetLlmSummary = vi.mocked(getLlmSummary);
const mockedGetLlmTokens = vi.mocked(getLlmTokens);
const mockedGetLlmByModel = vi.mocked(getLlmByModel);
const mockedGetLlmTopKeys = vi.mocked(getLlmTopKeys);
const mockedGetLlmErrors = vi.mocked(getLlmErrors);
const mockedGetLlmRequestsTotal = vi.mocked(getLlmRequestsTotal);
const mockedGetApiKeys = vi.mocked(getApiKeys);

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
    mockedGetLlmRequestsTotal.mockResolvedValue([]);
    mockedGetApiKeys.mockResolvedValue([]);
    window.__RUNTIME_CONFIG__ = {
      ...window.__RUNTIME_CONFIG__,
      LITELLM_ADMIN_URL: 'https://localhost:4000/ui',
    };
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
      },
    ]);
    const { default: LlmMonitoring } = await import('../pages/LlmMonitoring');

    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('customer-portal')).toBeInTheDocument();
    });

    expect(screen.getByText('API Key Name')).toBeInTheDocument();
    expect(screen.getAllByText('Input Tokens').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Output Tokens').length).toBeGreaterThan(0);
    expect(screen.getAllByText('3.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('5.0K').length).toBeGreaterThan(0);
    expect(screen.getAllByText('25').length).toBeGreaterThan(0);
  });

  it('shows the API key description as a tooltip on its name', async () => {
    mockedGetLlmTopKeys.mockResolvedValue([
      { api_key: 'customer-portal', input_tokens: 0, output_tokens: 0, cached_tokens: 0, tokens: 0, requests: 0 },
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
