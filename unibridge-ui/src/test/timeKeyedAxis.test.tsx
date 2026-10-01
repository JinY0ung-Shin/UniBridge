import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { Line, LineChart, Tooltip, XAxis } from 'recharts';
import { timeKeyedAxis } from '../utils/monitoring';
import { formatChartTimestamp } from '../utils/time';

// Real recharts (no mock): axis tooltips resolve the hovered row by axis
// value, so a chart keyed on a repeating display label shows the first
// matching row. Four 6h points of 2026-09-24 KST all tick as "9/24" at a 30d
// span, but each has its own epoch and value.
const dayStart = Date.UTC(2026, 8, 23, 15, 0, 0) / 1000; // 2026-09-24 00:00 KST
const rows = [11, 22, 33, 44].map((rps, i) => ({ ts: dayStart + i * 6 * 3600, rps }));
const axis = timeKeyedAxis((ts) => formatChartTimestamp(ts, 30 * 86400));

describe('timeKeyedAxis with real recharts', () => {
  it('shows the hovered row even when tick labels repeat', () => {
    expect(new Set(rows.map((r) => formatChartTimestamp(r.ts, 30 * 86400)))).toEqual(new Set(['9/24']));

    const { container } = render(
      <LineChart width={600} height={300} data={rows}>
        <XAxis {...axis.xAxis} />
        <Tooltip {...axis.tooltip} defaultIndex={2} active />
        <Line dataKey="rps" isAnimationActive={false} />
      </LineChart>,
    );

    // Row 2 = 12:00 KST, rps 33 — not row 0's 00:00 / 11.
    expect(container.querySelector('.recharts-tooltip-label')).toHaveTextContent('9/24 12:00');
    expect(container.querySelector('.recharts-tooltip-item-value')).toHaveTextContent('33');
  });
});
