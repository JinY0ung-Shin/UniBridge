vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div data-testid="bar-chart">{children}</div>,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

import { screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import BucketedBreakdownView from '../components/BucketedBreakdownView';
import { renderWithProviders } from './helpers';

describe('BucketedBreakdownView', () => {
  it('shows the bucket selection hint before a bucket is selected', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Requests by route"
        bucket="auto"
        unit="requests"
      />,
    );

    expect(screen.getByText('Select Hourly, Daily or Weekly to see usage over time.')).toBeInTheDocument();
  });

  it('shows an explicit loading state after a bucket is selected', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Requests by route"
        bucket="day"
        unit="requests"
        loading
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('Loading breakdown...');
  });

  it('shows no-data copy after a bucketed query returns empty', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Requests by route"
        bucket="day"
        unit="requests"
        data={{ buckets: [], series: [], unit: 'requests' }}
      />,
    );

    expect(screen.getByText('No bucketed data available')).toBeInTheDocument();
    expect(screen.queryByText('Select Hourly, Daily or Weekly to see usage over time.')).not.toBeInTheDocument();
  });

  it('renders bucketed series data with table affordance classes', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Requests by route"
        bucket="day"
        unit="requests"
        data={{
          buckets: [1772323200, 1772409600],
          series: [{ key: 'orders-route', total: 5, points: [2, 3] }],
          unit: 'requests',
        }}
      />,
    );

    expect(screen.getByText('orders-route')).toBeInTheDocument();
    expect(screen.getByText('Total')).toHaveClass('breakdown-cell--right');
    expect(screen.getByText('5')).toHaveClass('breakdown-cell--total');
  });

  const sentinelData = {
    buckets: [1772323200],
    series: [
      { key: '__ui__', total: 4, points: [4] },
      { key: '(untracked)', total: 2, points: [2] },
      { key: 'alice', total: 1, points: [1] },
    ],
    unit: 'queries' as const,
  };

  it('translates the query-monitoring consumer sentinels when opted in', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Queries by API key"
        bucket="day"
        unit="queries"
        data={sentinelData}
        queryConsumerLabels
      />,
    );

    expect(screen.getByText('(UI / direct)')).toBeInTheDocument();
    expect(screen.getByText('(before per-key tracking)')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.queryByText('__ui__')).not.toBeInTheDocument();
  });

  it('leaves free-form keys such as DB aliases untouched by default', () => {
    renderWithProviders(
      <BucketedBreakdownView
        title="Queries by database"
        bucket="day"
        unit="queries"
        data={sentinelData}
      />,
    );

    expect(screen.getByText('__ui__')).toBeInTheDocument();
    expect(screen.getByText('(untracked)')).toBeInTheDocument();
    expect(screen.queryByText('(UI / direct)')).not.toBeInTheDocument();
  });
});
