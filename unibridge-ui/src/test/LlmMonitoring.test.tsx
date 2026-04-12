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
}));

import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getLlmByModel,
  getLlmErrors,
  getLlmRequestsTotal,
  getLlmSummary,
  getLlmTokens,
  getLlmTopKeys,
} from '../api/client';
import LlmMonitoring from '../pages/LlmMonitoring';
import { renderWithProviders } from './helpers';

const mockedGetLlmSummary = vi.mocked(getLlmSummary);
const mockedGetLlmTokens = vi.mocked(getLlmTokens);
const mockedGetLlmByModel = vi.mocked(getLlmByModel);
const mockedGetLlmTopKeys = vi.mocked(getLlmTopKeys);
const mockedGetLlmErrors = vi.mocked(getLlmErrors);
const mockedGetLlmRequestsTotal = vi.mocked(getLlmRequestsTotal);

describe('LlmMonitoring', () => {
  beforeEach(() => {
    mockedGetLlmSummary.mockResolvedValue({
      total_tokens: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      estimated_cost: 0,
      total_requests: 0,
      avg_latency_ms: 0,
    });
    mockedGetLlmTokens.mockResolvedValue({ prompt: [], completion: [] });
    mockedGetLlmByModel.mockResolvedValue([]);
    mockedGetLlmTopKeys.mockResolvedValue([]);
    mockedGetLlmErrors.mockResolvedValue([]);
    mockedGetLlmRequestsTotal.mockResolvedValue([]);
  });

  it('links LiteLLM Admin to the root-hosted UI path', async () => {
    renderWithProviders(<LlmMonitoring />);

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /LiteLLM Admin/i })).toBeInTheDocument();
    });

    expect(screen.getByRole('link', { name: /LiteLLM Admin/i })).toHaveAttribute(
      'href',
      '/ui',
    );
  });
});
