import type { ReactNode } from 'react';

interface DataTablePageHeaderProps {
  title: string;
  subtitle?: string;
  canAdd?: boolean;
  addLabel?: string;
  onAdd?: () => void;
  extra?: ReactNode;
}

function DataTablePageHeader({
  title,
  subtitle,
  canAdd,
  addLabel,
  onAdd,
  extra,
}: DataTablePageHeaderProps) {
  const showAdd = Boolean(canAdd && onAdd && addLabel);
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {extra}
      {showAdd && (
        <button className="btn btn-primary" onClick={onAdd}>
          {addLabel}
        </button>
      )}
    </div>
  );
}

export default DataTablePageHeader;
