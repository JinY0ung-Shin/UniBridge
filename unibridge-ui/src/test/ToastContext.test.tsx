import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider } from '../components/ToastContext';
import { useToast } from '../components/useToast';

function ToastEmitter({
  toasts,
}: {
  toasts: Array<{ type: 'success' | 'error' | 'info'; title: string; message?: string }>;
}) {
  const { addToast } = useToast();
  return (
    <button onClick={() => toasts.forEach((t) => addToast(t))}>emit</button>
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ToastContext', () => {
  it('renders an added toast with title and message', () => {
    render(
      <ToastProvider>
        <ToastEmitter
          toasts={[{ type: 'success', title: 'Saved', message: 'Resource saved' }]}
        />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
    });
    expect(screen.getByText('Saved')).toBeInTheDocument();
    expect(screen.getByText('Resource saved')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Saved');
  });

  it('renders multiple toasts at once', () => {
    render(
      <ToastProvider>
        <ToastEmitter
          toasts={[
            { type: 'success', title: 'One' },
            { type: 'error', title: 'Two', message: 'oops' },
            { type: 'info', title: 'Three' },
          ]}
        />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
    });
    expect(screen.getByText('One')).toBeInTheDocument();
    expect(screen.getByText('Two')).toBeInTheDocument();
    expect(screen.getByText('Three')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Two');
  });

  it('deduplicates repeated messages and limits the visible stack', () => {
    render(
      <ToastProvider>
        <ToastEmitter
          toasts={[
            { type: 'error', title: 'Repeated' },
            { type: 'error', title: 'Repeated' },
            { type: 'info', title: 'Two' },
            { type: 'info', title: 'Three' },
            { type: 'info', title: 'Four' },
            { type: 'info', title: 'Five' },
          ]}
        />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText('emit'));

    expect(screen.queryByText('Repeated')).not.toBeInTheDocument();
    expect(screen.getAllByRole('status')).toHaveLength(4);
  });

  it('auto-dismisses after timeout', () => {
    render(
      <ToastProvider>
        <ToastEmitter toasts={[{ type: 'info', title: 'Bye' }]} />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
    });
    expect(screen.getByText('Bye')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(6500);
    });
    expect(screen.queryByText('Bye')).not.toBeInTheDocument();
  });

  it('keeps error details visible until the user dismisses them', () => {
    render(
      <ToastProvider>
        <ToastEmitter toasts={[{ type: 'error', title: 'Request failed', message: 'Action required' }]} />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
      vi.advanceTimersByTime(30_000);
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Action required');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Request failed')).not.toBeInTheDocument();
  });

  it('dismisses a toast when the close button is clicked', () => {
    render(
      <ToastProvider>
        <ToastEmitter toasts={[{ type: 'success', title: 'Closable' }]} />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
    });
    expect(screen.getByText('Closable')).toBeInTheDocument();

    const closeBtn = screen.getByRole('button', { name: 'Close' });
    act(() => {
      fireEvent.click(closeBtn);
    });
    expect(screen.queryByText('Closable')).not.toBeInTheDocument();
  });

  it('hides message element when message is undefined', () => {
    const { container } = render(
      <ToastProvider>
        <ToastEmitter toasts={[{ type: 'info', title: 'No detail' }]} />
      </ToastProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText('emit'));
    });
    expect(container.querySelector('.toast-message')).toBeNull();
  });
});
