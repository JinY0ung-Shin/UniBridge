export type SortDir = 'asc' | 'desc';

export interface SortState<C extends string> {
  column: C;
  dir: SortDir;
}

/** Standard toggle: re-clicking flips direction, a new column starts desc. */
export function toggleSortState<C extends string>(prev: SortState<C>, column: C): SortState<C> {
  return prev.column === column
    ? { column, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
    : { column, dir: 'desc' };
}

/**
 * Sort rows by the accessor's value for the active column. Strings compare
 * with localeCompare; numbers numerically; null/undefined always sort last
 * regardless of direction.
 */
export function sortRows<T, C extends string>(
  rows: T[],
  state: SortState<C>,
  accessor: (row: T, column: C) => string | number | null | undefined,
): T[] {
  const multiplier = state.dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = accessor(a, state.column);
    const bv = accessor(b, state.column);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string' || typeof bv === 'string') {
      return String(av).localeCompare(String(bv)) * multiplier;
    }
    return (av - bv) * multiplier;
  });
}
