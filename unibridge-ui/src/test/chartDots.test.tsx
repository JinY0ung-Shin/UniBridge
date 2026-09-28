import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { isolatedPointDot } from '../utils/chartDots';

const rows = [
  { p50: 10 },   // 0: lone at the left edge
  { p50: null },
  { p50: 20 },   // 2: part of a segment
  { p50: 30 },   // 3: part of a segment
  { p50: null },
  { p50: 40 },   // 5: lone between gaps
  { p50: null },
  { p50: 50 },   // 7: lone at the right edge
];

function circleAt(index: number, coords: { cx?: number; cy?: number } = { cx: 5, cy: 5 }) {
  const renderDot = isolatedPointDot(rows, 'p50');
  const { container } = render(<svg>{renderDot({ ...coords, index, stroke: '#123456' })}</svg>);
  return container.querySelector('circle');
}

describe('isolatedPointDot', () => {
  it('draws a dot only for values without non-null neighbours (edges count as gaps)', () => {
    expect(circleAt(0)).not.toBeNull();
    expect(circleAt(2)).toBeNull();
    expect(circleAt(3)).toBeNull();
    expect(circleAt(5)).not.toBeNull();
    expect(circleAt(7)).not.toBeNull();
  });

  it('draws nothing for null values or missing coordinates', () => {
    expect(circleAt(1)).toBeNull();
    expect(circleAt(5, { cy: 5 })).toBeNull();
  });

  it('uses the line colour and position for the dot', () => {
    const circle = circleAt(5, { cx: 12, cy: 34 });
    expect(circle).toHaveAttribute('cx', '12');
    expect(circle).toHaveAttribute('cy', '34');
    expect(circle).toHaveAttribute('fill', '#123456');
  });
});
