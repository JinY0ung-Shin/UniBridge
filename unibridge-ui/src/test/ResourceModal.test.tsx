import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import ResourceModal from '../components/ResourceModal';

describe('ResourceModal', () => {
  it('renders as an accessible dialog, closes on Escape, and restores trigger focus', async () => {
    const user = userEvent.setup();

    function ModalHost() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open modal</button>
          {open && (
            <ResourceModal title="Create Resource" closeLabel="Close" onClose={() => setOpen(false)}>
              <input aria-label="Resource name" />
            </ResourceModal>
          )}
        </>
      );
    }

    render(<ModalHost />);

    const opener = screen.getByRole('button', { name: 'Open modal' });
    await user.click(opener);

    const dialog = screen.getByRole('dialog', { name: 'Create Resource' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('textbox', { name: 'Resource name' })).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Create Resource' })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('does not override child autofocus', () => {
    render(
      <ResourceModal title="Create Resource" closeLabel="Close" onClose={vi.fn()}>
        <input aria-label="Focused by child" autoFocus />
        <input aria-label="Second field" />
      </ResourceModal>,
    );

    expect(screen.getByRole('textbox', { name: 'Focused by child' })).toHaveFocus();
  });

  it('skips visually hidden controls when choosing initial focus', () => {
    render(
      <ResourceModal title="Create Resource" closeLabel="Close" onClose={vi.fn()}>
        <button type="button" style={{ display: 'none' }}>Hidden Action</button>
        <button type="button">Save</button>
      </ResourceModal>,
    );

    expect(screen.getByRole('button', { name: 'Save' })).toHaveFocus();
  });

  it('ignores Escape when closeOnEscape is false', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(
      <ResourceModal title="Locked" closeLabel="Close" onClose={onClose} closeOnEscape={false}>
        <input aria-label="Field" />
      </ResourceModal>,
    );

    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps Tab focus inside the dialog', async () => {
    const user = userEvent.setup();

    render(
      <ResourceModal title="Edit Resource" closeLabel="Close" onClose={vi.fn()}>
        <input aria-label="Resource name" />
        <button type="button">Save</button>
      </ResourceModal>,
    );

    const closeButton = screen.getByRole('button', { name: 'Close' });
    const input = screen.getByRole('textbox', { name: 'Resource name' });
    const saveButton = screen.getByRole('button', { name: 'Save' });

    expect(input).toHaveFocus();

    await user.tab();
    expect(saveButton).toHaveFocus();

    await user.tab();
    expect(closeButton).toHaveFocus();

    await user.tab();
    expect(input).toHaveFocus();

    await user.tab({ shift: true });
    expect(closeButton).toHaveFocus();

    await user.tab({ shift: true });
    expect(saveButton).toHaveFocus();
  });
});
