import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import BucketSelector from '../components/BucketSelector';
import { renderWithProviders } from './helpers';

describe('BucketSelector', () => {
  it('exposes the selected bucket as a pressed segmented-control button', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    renderWithProviders(<BucketSelector value="day" onChange={onChange} />);

    expect(screen.getByRole('group', { name: 'Bucket' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Daily' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'false');

    await user.click(screen.getByRole('button', { name: 'Weekly' }));
    expect(onChange).toHaveBeenCalledWith('week');
  });
});
