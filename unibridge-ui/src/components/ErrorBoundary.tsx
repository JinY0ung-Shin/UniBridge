import { Component, type ReactNode } from 'react';
import i18n from '../i18n';

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
    const t = i18n.t.bind(i18n);

    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        minHeight: '100dvh', background: 'var(--bg-root)', color: 'var(--text-primary)',
        fontFamily: 'var(--font-sans)', padding: '2rem',
      }}>
        <div style={{ textAlign: 'center', maxWidth: 560 }}>
          <h2 style={{ marginBottom: '0.75rem' }}>{t('errorBoundary.title')}</h2>
          <p
            role="alert"
            style={{ color: 'var(--text-tertiary)', lineHeight: 1.6, marginBottom: '1.5rem', overflowWrap: 'anywhere' }}
          >
            {error?.message || t('errorBoundary.fallback')}
          </p>
          <div style={{ display: 'flex', justifyContent: 'center', gap: 12, flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={() => window.location.reload()}
              style={{
                minHeight: 44, padding: '8px 24px',
                background: 'var(--bg-tertiary)', color: 'var(--text-primary)',
                border: '1px solid var(--border-hover)',
                borderRadius: 6, cursor: 'pointer', fontSize: 14,
              }}
            >
              {t('errorBoundary.reload')}
            </button>
            <button
              type="button"
              aria-expanded={showDetails}
              aria-controls={error?.stack ? 'error-boundary-details' : undefined}
              onClick={() => this.setState(s => ({ showDetails: !s.showDetails }))}
              style={{
                minHeight: 44, padding: '8px 16px',
                background: 'transparent', color: 'var(--text-tertiary)',
                border: '1px solid var(--border-hover)',
                borderRadius: 6, cursor: 'pointer', fontSize: 14,
              }}
            >
              {showDetails ? t('errorBoundary.hideDetails') : t('errorBoundary.showDetails')}
            </button>
          </div>
          {showDetails && error?.stack && (
            <pre id="error-boundary-details" style={{
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
