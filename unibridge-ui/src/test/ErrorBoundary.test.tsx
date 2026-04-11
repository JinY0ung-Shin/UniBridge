import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ErrorBoundary } from '../components/ErrorBoundary';

function BrokenChild(): never {
  throw new Error('Test render error');
}

describe('ErrorBoundary', () => {
  it('renders children normally when no error', () => {
    render(
      <ErrorBoundary>
        <div>Hello world</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText('Hello world')).toBeInTheDocument();
  });

  it('shows error UI when a child throws', () => {
    // Suppress console.error for expected error output
    const originalError = console.error;
    console.error = () => {};

    render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>,
    );

    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    expect(screen.getByText('Test render error')).toBeInTheDocument();

    console.error = originalError;
  });

  it('shows Reload button in error state', () => {
    const originalError = console.error;
    console.error = () => {};

    render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>,
    );

    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();

    console.error = originalError;
  });
});
