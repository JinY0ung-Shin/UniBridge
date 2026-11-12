import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Root from '../Root';

vi.mock('../components/AuthProvider', () => ({
  AuthProvider: () => <div>app behind the role gate</div>,
}));
vi.mock('../App', () => ({ default: () => null }));
vi.mock('../pages/UploadRelay', () => ({ default: () => <div>upload relay page</div> }));

describe('Root', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('renders the app, behind its sign-in and role gate, at every ordinary address', () => {
    window.history.replaceState(null, '', '/connections');
    render(<Root />);

    expect(screen.getByText('app behind the role gate')).toBeInTheDocument();
    expect(screen.queryByText('upload relay page')).not.toBeInTheDocument();
  });

  it('renders the upload relay outside the app gate at /upload-relay', async () => {
    window.history.replaceState(null, '', '/upload-relay#t=v1.a.b');
    render(<Root />);

    expect(await screen.findByText('upload relay page')).toBeInTheDocument();
    expect(screen.queryByText('app behind the role gate')).not.toBeInTheDocument();
  });
});
