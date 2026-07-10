import type { SortDir } from '../utils/tableSort';

interface SortableHeaderProps<C extends string> {
  column: C;
  label: string;
  align?: 'left' | 'right';
  activeColumn: C;
  dir: SortDir;
  onToggle: (column: C) => void;
}

/** Semantic column header containing a native sort button and aria-sort state. */
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
  return (
    <th className={classes} scope="col" aria-sort={ariaSort}>
      <button
        type="button"
        className="sortable-header__button"
        onClick={() => onToggle(column)}
      >
        <span>{label}</span>
        {active && <span className="sort-indicator" aria-hidden="true">{dir === 'asc' ? '▲' : '▼'}</span>}
      </button>
    </th>
  );
}

export default SortableHeader;
