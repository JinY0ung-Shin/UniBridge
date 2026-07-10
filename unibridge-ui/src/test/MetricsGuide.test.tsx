import { screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import MetricsGuide from '../pages/MetricsGuide';
import bundledGuide from '../content/api-metrics-convention.md?raw';
// The canonical guide at the repo root — resolvable here because vitest runs
// from the repo checkout; the docker image build never compiles test files.
import canonicalGuide from '../../../docs/api-metrics-convention.md?raw';
import { renderWithProviders } from './helpers';

describe('MetricsGuide', () => {
  it('renders the convention document', () => {
    renderWithProviders(<MetricsGuide />);

    expect(screen.getByText('API Metrics Guide')).toBeInTheDocument();
    // Headings from the markdown itself
    expect(
      screen.getByRole('heading', { name: /API 메트릭 컨벤션/ }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/http_requests_total/).length).toBeGreaterThan(0);
  });

  it('stays in sync with the canonical docs/ copy', () => {
    // The canonical guide lives outside the UI docker build context, so the
    // page bundles a committed copy. This guard fails the suite whenever the
    // two files diverge — update both together:
    //   cp docs/api-metrics-convention.md unibridge-ui/src/content/
    expect(bundledGuide).toBe(canonicalGuide);
  });

  it('wraps wide markdown tables in a keyboard-scrollable labelled region', () => {
    renderWithProviders(<MetricsGuide />);

    const tableRegions = screen.getAllByRole('region', {
      name: 'Scrollable metrics reference table',
    });
    expect(tableRegions.length).toBeGreaterThan(0);
    tableRegions.forEach((region) => {
      expect(region).toHaveAttribute('tabindex', '0');
      expect(region.querySelector('table')).toBeInTheDocument();
    });
  });
});
