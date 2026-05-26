import { fireEvent, render, screen } from '@testing-library/react';
import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { ErrorBoundary } from '../components/ErrorBoundary';

function BrokenChild(): never {
  throw new Error('Boom');
}

let originalConsoleError: typeof console.error;

beforeEach(() => {
  originalConsoleError = console.error;
  console.error = () => {};
});

afterEach(() => {
  console.error = originalConsoleError;
});

describe('ErrorBoundary details + reload', () => {
  it('toggles details with the Show/Hide details button', () => {
    render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>,
    );

    const toggle = screen.getByRole('button', { name: 'Show details' });
    fireEvent.click(toggle);
    // After click, button label flips
    expect(screen.getByRole('button', { name: 'Hide details' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }));
    expect(screen.getByRole('button', { name: 'Show details' })).toBeInTheDocument();
  });

  it('renders stack trace pre block when details are shown', () => {
    const { container } = render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>,
    );
    expect(container.querySelector('pre')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
    expect(container.querySelector('pre')).not.toBeNull();
  });

  it('renders fallback message when error has no message', () => {
    function NoMessage(): never {
      // empty string acts as no message
      throw new Error('');
    }
    render(
      <ErrorBoundary>
        <NoMessage />
      </ErrorBoundary>,
    );
    expect(screen.getByText('An unexpected error occurred.')).toBeInTheDocument();
  });

  it('renders an enabled Reload button in error state', () => {
    render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>,
    );
    const btn = screen.getByRole('button', { name: 'Reload' });
    expect(btn).toBeEnabled();
  });
});
