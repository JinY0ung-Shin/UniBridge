import { useTheme } from './useTheme';

export interface ChartTheme {
  grid: string;
  axis: string;
  tooltipBg: string;
  tooltipBorder: string;
  textSecondary: string;
  textTertiary: string;
  blue: string;
  green: string;
  yellow: string;
  red: string;
}

function readCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function useChartTheme(): ChartTheme {
  // Subscribe to theme changes so chart variables are reread when the theme flips.
  useTheme();
  return {
    grid: readCssVar('--chart-grid'),
    axis: readCssVar('--chart-axis'),
    tooltipBg: readCssVar('--chart-tooltip-bg'),
    tooltipBorder: readCssVar('--chart-tooltip-border'),
    textSecondary: readCssVar('--text-secondary'),
    textTertiary: readCssVar('--text-tertiary'),
    blue: readCssVar('--accent-blue'),
    green: readCssVar('--accent-green'),
    yellow: readCssVar('--accent-yellow'),
    red: readCssVar('--accent-red'),
  };
}

export function statusCodeColor(code: string, theme: ChartTheme): string {
  if (code.startsWith('2')) return theme.green;
  if (code.startsWith('3')) return theme.blue;
  if (code.startsWith('4')) return theme.yellow;
  if (code.startsWith('5')) return theme.red;
  return theme.textTertiary;
}
