import { describe, expect, it, beforeEach, vi, afterEach } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';
import { ThemeProvider } from '../components/ThemeContext';
import { useTheme } from '../components/useTheme';

function readResolved() {
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <ThemeProvider>{children}</ThemeProvider>
  );
  return renderHook(() => useTheme(), { wrapper });
}

describe('ThemeContext', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
    localStorage.clear();
  });

  it('defaults to dark theme', () => {
    const { result } = readResolved();
    expect(result.current.theme).toBe('dark');
    expect(result.current.resolved).toBe('dark');
  });

  it('reads stored "light" theme from localStorage', () => {
    localStorage.setItem('theme', 'light');
    const { result } = readResolved();
    expect(result.current.theme).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('reads stored "system" theme and resolves via matchMedia', () => {
    localStorage.setItem('theme', 'system');
    const mqList = {
      matches: true,  // prefers light
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as MediaQueryList;
    const original = window.matchMedia;
    window.matchMedia = vi.fn().mockReturnValue(mqList) as typeof window.matchMedia;

    const { result } = readResolved();
    expect(result.current.theme).toBe('system');
    expect(result.current.resolved).toBe('light');
    expect(mqList.addEventListener).toHaveBeenCalledWith('change', expect.any(Function));

    window.matchMedia = original;
  });

  it('falls back to dark when matchMedia is unavailable', () => {
    localStorage.setItem('theme', 'system');
    const original = window.matchMedia;
    // @ts-expect-error - simulating unavailable API
    window.matchMedia = undefined;

    const { result } = readResolved();
    expect(result.current.resolved).toBe('dark');

    window.matchMedia = original;
  });

  it('setTheme updates state and writes localStorage', () => {
    const { result } = readResolved();
    act(() => result.current.setTheme('light'));
    expect(result.current.theme).toBe('light');
    expect(localStorage.getItem('theme')).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('setTheme back to dark removes data-theme attribute', () => {
    localStorage.setItem('theme', 'light');
    const { result } = readResolved();
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    act(() => result.current.setTheme('dark'));
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('handles localStorage throwing on read gracefully', () => {
    const originalGet = Storage.prototype.getItem;
    Storage.prototype.getItem = () => { throw new Error('blocked'); };
    try {
      render(
        <ThemeProvider>
          <div data-testid="kid">child</div>
        </ThemeProvider>,
      );
      expect(screen.getByTestId('kid')).toBeInTheDocument();
    } finally {
      Storage.prototype.getItem = originalGet;
    }
  });

  it('handles localStorage throwing on write gracefully', () => {
    const originalSet = Storage.prototype.setItem;
    Storage.prototype.setItem = () => { throw new Error('blocked'); };
    try {
      const { result } = readResolved();
      // Should not throw
      act(() => result.current.setTheme('light'));
      expect(result.current.theme).toBe('light');
    } finally {
      Storage.prototype.setItem = originalSet;
    }
  });

  it('ignores invalid stored theme values', () => {
    localStorage.setItem('theme', 'plaid');
    const { result } = readResolved();
    expect(result.current.theme).toBe('dark');
  });
});

afterEach(() => {
  document.documentElement.removeAttribute('data-theme');
  localStorage.clear();
});
