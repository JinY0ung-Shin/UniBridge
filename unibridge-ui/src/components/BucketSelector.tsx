import { useTranslation } from 'react-i18next';
import { BUCKETS, type Bucket } from '../utils/timeRange';
import './BucketSelector.css';

interface BucketSelectorProps {
  value: Bucket;
  onChange: (next: Bucket) => void;
}

/**
 * Granularity toggle for volume/bar charts. `auto` defers to the time range's
 * default stepping; hour/day/week snap bars to KST calendar buckets.
 */
function BucketSelector({ value, onChange }: BucketSelectorProps) {
  const { t } = useTranslation();
  return (
    <div className="bucket-toggle" role="group" aria-label={t('bucket.label')}>
      {BUCKETS.map((b) => (
        <button
          key={b}
          type="button"
          className={`bucket-btn ${value === b ? 'bucket-btn--active' : ''}`}
          onClick={() => onChange(b)}
        >
          {t(`bucket.${b}`)}
        </button>
      ))}
    </div>
  );
}

export default BucketSelector;
