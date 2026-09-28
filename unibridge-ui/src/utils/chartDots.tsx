import type { ReactElement } from 'react';

/** The subset of recharts' custom `dot` render props this renderer reads. */
interface DotRenderProps {
  cx?: number;
  cy?: number;
  index?: number;
  stroke?: string;
}

/**
 * `dot` renderer for gap-preserving line charts (no connectNulls): draws a
 * point only where both neighbours of `rows[index][key]` are null. With dots
 * off, recharts renders such a lone value as a zero-length path — invisible —
 * so a sparse series (single samples between gaps) would look like no data.
 */
export function isolatedPointDot<K extends string>(
  rows: ReadonlyArray<Partial<Record<K, number | null>>>,
  key: K,
): (props: DotRenderProps) => ReactElement {
  return ({ cx, cy, index = -1, stroke }: DotRenderProps) => {
    const isolated =
      rows[index]?.[key] != null && rows[index - 1]?.[key] == null && rows[index + 1]?.[key] == null;
    if (!isolated || cx == null || cy == null) return <g key={`${key}-${index}`} />;
    return <circle key={`${key}-${index}`} cx={cx} cy={cy} r={3} fill={stroke} />;
  };
}
