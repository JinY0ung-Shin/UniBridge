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
});
