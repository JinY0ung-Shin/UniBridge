import type { KeyboardEvent } from 'react';
import type { SortDir } from '../utils/tableSort';

interface SortableHeaderProps<C extends string> {
  column: C;
  label: string;
  align?: 'left' | 'right';
  activeColumn: C;
  dir: SortDir;
  onToggle: (column: C) => void;
}

/** Clickable/keyboard-operable `<th>` with an aria-sort state and ▲▼ marker. */
function SortableHeader<C extends string>({
  column,
  label,
  align = 'left',
  activeColumn,
  dir,
  onToggle,
}: SortableHeaderProps<C>) {
  const active = activeColumn === column;
  const ariaSort: 'none' | 'ascending' | 'descending' =
    active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const classes = `sortable-header${align === 'right' ? ' sortable-header--right' : ''}`;
  const handleKey = (e: KeyboardEvent<HTMLTableCellElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle(column);
    }
  };
  return (
    <th
      className={classes}
      onClick={() => onToggle(column)}
      onKeyDown={handleKey}
      tabIndex={0}
      role="button"
      aria-sort={ariaSort}
    >
      {label}
      {active && <span className="sort-indicator">{dir === 'asc' ? '▲' : '▼'}</span>}
    </th>
  );
}

export default SortableHeader;
