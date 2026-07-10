import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SortableHeader from '../components/SortableHeader';

describe('SortableHeader', () => {
  it('preserves column-header semantics while using a native sort button', () => {
    const onToggle = vi.fn();
    render(
      <table>
        <thead>
          <tr>
            <SortableHeader
              column="requests"
              label="Requests"
              activeColumn="requests"
              dir="asc"
              onToggle={onToggle}
            />
          </tr>
        </thead>
      </table>,
    );

    expect(screen.getByRole('columnheader', { name: 'Requests' })).toHaveAttribute('aria-sort', 'ascending');
    fireEvent.click(screen.getByRole('button', { name: 'Requests' }));
    expect(onToggle).toHaveBeenCalledWith('requests');
  });
});
