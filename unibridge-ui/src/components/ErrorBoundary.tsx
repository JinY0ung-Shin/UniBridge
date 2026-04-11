import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  showDetails: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, showDetails: false };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }

    const { error, showDetails } = this.state;

    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100vh', background: 'var(--bg-root)', color: 'var(--text-primary)',
        fontFamily: 'var(--font-sans)', padding: '2rem',
      }}>
        <div style={{ textAlign: 'center', maxWidth: 560 }}>
          <h2 style={{ marginBottom: '0.75rem' }}>Something went wrong</h2>
          <p style={{ color: 'var(--text-tertiary)', lineHeight: 1.6, marginBottom: '1.5rem' }}>
            {error?.message || 'An unexpected error occurred.'}
          </p>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: '8px 24px',
              background: 'var(--bg-tertiary)', color: 'var(--text-primary)',
              border: '1px solid var(--border-hover)',
              borderRadius: 6, cursor: 'pointer', fontSize: 14,
              marginRight: '0.75rem',
            }}
          >
            Reload
          </button>
          <button
            onClick={() => this.setState(s => ({ showDetails: !s.showDetails }))}
            style={{
              padding: '8px 16px',
              background: 'transparent', color: 'var(--text-tertiary)',
              border: '1px solid var(--border-hover)',
              borderRadius: 6, cursor: 'pointer', fontSize: 14,
            }}
          >
            {showDetails ? 'Hide details' : 'Show details'}
          </button>
          {showDetails && error?.stack && (
            <pre style={{
              marginTop: '1.5rem', padding: '1rem',
              background: 'var(--bg-tertiary)', border: '1px solid var(--border-hover)',
              borderRadius: 6, textAlign: 'left', fontSize: 12,
              fontFamily: 'var(--font-mono)', overflowX: 'auto',
              color: 'var(--text-tertiary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              {error.stack}
            </pre>
          )}
        </div>
      </div>
    );
  }
}
