import { useContext } from 'react';
import { ThemeContext } from './ThemeContextValue';

export function useTheme() {
  return useContext(ThemeContext);
}
