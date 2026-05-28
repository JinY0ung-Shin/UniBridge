import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { TIME_RANGES, type TimeSelection } from '../utils/timeRange';
import { kstLocalToEpoch, epochToKstLocal, formatKstChip } from '../utils/time';
import './TimeRangeSelector.css';

const MIN_CUSTOM_SPAN_SECONDS = 60;

interface TimeRangeSelectorProps {
  value: TimeSelection;
  onChange: (next: TimeSelection) => void;
}

function currentEpochSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function defaultLocalRange(nowSec: number): { start: string; end: string } {
  return {
    start: epochToKstLocal(nowSec - 3600),
    end: epochToKstLocal(nowSec),
  };
}

function TimeRangeSelector({ value, onChange }: TimeRangeSelectorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [validationNowSec, setValidationNowSec] = useState(currentEpochSeconds);
  const initial = defaultLocalRange(validationNowSec);
  const [startLocal, setStartLocal] = useState(
    value.kind === 'custom' ? epochToKstLocal(value.start) : initial.start,
  );
  const [endLocal, setEndLocal] = useState(
    value.kind === 'custom' ? epochToKstLocal(value.end) : initial.end,
  );

  const startEpoch = startLocal ? kstLocalToEpoch(startLocal) : NaN;
  const endEpoch = endLocal ? kstLocalToEpoch(endLocal) : NaN;
  const valid =
    Number.isFinite(startEpoch) &&
    Number.isFinite(endEpoch) &&
    startEpoch < endEpoch &&
    endEpoch - startEpoch >= MIN_CUSTOM_SPAN_SECONDS &&
    endEpoch <= validationNowSec + 60;

  const apply = () => {
    if (!Number.isFinite(startEpoch) || !Number.isFinite(endEpoch)) return;
    if (startEpoch >= endEpoch) return;
    if (endEpoch - startEpoch < MIN_CUSTOM_SPAN_SECONDS) return;
    const now = currentEpochSeconds();
    if (endEpoch > now + 60) return;
    onChange({ kind: 'custom', start: startEpoch, end: endEpoch });
    setOpen(false);
  };

  const clearCustom = () => onChange({ kind: 'preset', value: '1h' });

  const toggleCustom = () => {
    if (open) {
      setOpen(false);
      return;
    }
    const now = currentEpochSeconds();
    const next = defaultLocalRange(now);
    setValidationNowSec(now);
    setStartLocal(next.start);
    setEndLocal(next.end);
    setOpen(true);
  };

  const updateStartLocal = (next: string) => {
    setValidationNowSec(currentEpochSeconds());
    setStartLocal(next);
  };

  const updateEndLocal = (next: string) => {
    setValidationNowSec(currentEpochSeconds());
    setEndLocal(next);
  };

  return (
    <div className="time-range-selector">
      <div className="time-range-toggle">
        {TIME_RANGES.map((r) => (
          <button
            key={r}
            className={`time-range-btn ${value.kind === 'preset' && value.value === r ? 'time-range-btn--active' : ''}`}
            onClick={() => onChange({ kind: 'preset', value: r })}
          >
            {r}
          </button>
        ))}
        {value.kind === 'custom' ? (
          <span className="time-range-chip">
            {formatKstChip(value.start, value.end)}
            <button
              type="button"
              className="time-range-chip__clear"
              data-testid="custom-clear"
              aria-label={t('timeRange.clear')}
              onClick={clearCustom}
            >
              ✕
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="time-range-btn time-range-btn--custom"
            data-testid="custom-toggle"
            onClick={toggleCustom}
          >
            {t('timeRange.custom')} ▾
          </button>
        )}
      </div>

      {open && (
        <div className="time-range-popover">
          <label className="time-range-field">
            <span>{t('timeRange.start')}</span>
            <input
              type="datetime-local"
              data-testid="custom-start"
              value={startLocal}
              onChange={(e) => updateStartLocal(e.target.value)}
            />
          </label>
          <label className="time-range-field">
            <span>{t('timeRange.end')}</span>
            <input
              type="datetime-local"
              data-testid="custom-end"
              value={endLocal}
              onChange={(e) => updateEndLocal(e.target.value)}
            />
          </label>
          {!valid && <div className="time-range-error">{t('timeRange.invalid')}</div>}
          <div className="time-range-actions">
            <button type="button" onClick={() => setOpen(false)}>
              {t('timeRange.cancel')}
            </button>
            <button type="button" data-testid="custom-apply" disabled={!valid} onClick={apply}>
              {t('timeRange.apply')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default TimeRangeSelector;
