import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import TimeRangeSelector from '../components/TimeRangeSelector';
import type { TimeSelection } from '../utils/timeRange';
import { renderWithProviders } from './helpers';

describe('TimeRangeSelector', () => {
  it('renders preset buttons and highlights the active one', () => {
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: '15m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1h' })).toHaveClass('time-range-btn--active');
  });

  it('fires onChange with a preset when a preset button is clicked', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={onChange} />,
    );
    await user.click(screen.getByRole('button', { name: '6h' }));
    expect(onChange).toHaveBeenCalledWith({ kind: 'preset', value: '6h' });
  });

  it('opens the custom popover and applies a valid range as epoch seconds', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={onChange} />,
    );
    await user.click(screen.getByTestId('custom-toggle'));

    const start = screen.getByTestId('custom-start') as HTMLInputElement;
    const end = screen.getByTestId('custom-end') as HTMLInputElement;
    await user.clear(start);
    await user.type(start, '2026-05-20T09:00');
    await user.clear(end);
    await user.type(end, '2026-05-20T10:00');
    await user.click(screen.getByTestId('custom-apply'));

    expect(onChange).toHaveBeenCalledWith({
      kind: 'custom',
      start: Date.UTC(2026, 4, 20, 0, 0, 0) / 1000,
      end: Date.UTC(2026, 4, 20, 1, 0, 0) / 1000,
    });
  });

  it('disables apply when start is not before end', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={vi.fn()} />,
    );
    await user.click(screen.getByTestId('custom-toggle'));
    const start = screen.getByTestId('custom-start') as HTMLInputElement;
    const end = screen.getByTestId('custom-end') as HTMLInputElement;
    await user.clear(start);
    await user.type(start, '2026-05-20T10:00');
    await user.clear(end);
    await user.type(end, '2026-05-20T09:00');
    expect(screen.getByTestId('custom-apply')).toBeDisabled();
  });

  it('shows a chip for an active custom selection and clears it back to 1h', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const value: TimeSelection = {
      kind: 'custom',
      start: Date.UTC(2026, 4, 20, 0, 0, 0) / 1000,
      end: Date.UTC(2026, 4, 22, 9, 0, 0) / 1000,
    };
    renderWithProviders(<TimeRangeSelector value={value} onChange={onChange} />);
    expect(screen.getByText('5/20 09:00~5/22 18:00')).toBeInTheDocument();
    await user.click(screen.getByTestId('custom-clear'));
    expect(onChange).toHaveBeenCalledWith({ kind: 'preset', value: '1h' });
  });
});
