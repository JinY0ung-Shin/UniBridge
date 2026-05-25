import { describe, expect, it } from 'vitest';
import { renderHook } from '@testing-library/react';
import { ThemeProvider } from '../components/ThemeContext';
import { statusCodeColor, useChartTheme } from '../components/useChartTheme';

describe('statusCodeColor', () => {
  const theme = {
    grid: '#g', axis: '#a',
    tooltipBg: '#tb', tooltipBorder: '#tbd',
    textSecondary: '#ts', textTertiary: '#tt',
    blue: '#blue', green: '#green', yellow: '#yellow', red: '#red',
  };

  it('returns green for 2xx', () => {
    expect(statusCodeColor('200', theme)).toBe('#green');
    expect(statusCodeColor('204', theme)).toBe('#green');
  });

  it('returns blue for 3xx', () => {
    expect(statusCodeColor('301', theme)).toBe('#blue');
  });

  it('returns yellow for 4xx', () => {
    expect(statusCodeColor('404', theme)).toBe('#yellow');
  });

  it('returns red for 5xx', () => {
    expect(statusCodeColor('500', theme)).toBe('#red');
    expect(statusCodeColor('503', theme)).toBe('#red');
  });

  it('returns textTertiary for unknown/non-standard codes', () => {
    expect(statusCodeColor('', theme)).toBe('#tt');
    expect(statusCodeColor('abc', theme)).toBe('#tt');
    expect(statusCodeColor('600', theme)).toBe('#tt');
    expect(statusCodeColor('100', theme)).toBe('#tt');
  });
});

describe('useChartTheme', () => {
  it('returns a ChartTheme object with all expected keys', () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ThemeProvider>{children}</ThemeProvider>
    );
    const { result } = renderHook(() => useChartTheme(), { wrapper });
    expect(result.current).toEqual(
      expect.objectContaining({
        grid: expect.any(String),
        axis: expect.any(String),
        tooltipBg: expect.any(String),
        tooltipBorder: expect.any(String),
        textSecondary: expect.any(String),
        textTertiary: expect.any(String),
        blue: expect.any(String),
        green: expect.any(String),
        yellow: expect.any(String),
        red: expect.any(String),
      }),
    );
  });
});
