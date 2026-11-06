import { useTranslation } from 'react-i18next';
import type { FinderBlockedPort, FinderUsedPort, PortSpan } from '../../api/network';
import { portRange } from './networkUtils';

/* One reachable port span drawn as a strip: a cell per port when the span is
 * short, otherwise a proportional bar with ticks where ports are taken. Colour
 * is never the only cue: every cell has a tooltip and the counts are in text. */

const CELL_MAX = 64; // spans up to this many ports get one cell per port

type PortState = 'free' | 'used' | 'blocked';

interface PortStripProps {
  span: PortSpan;
  used: FinderUsedPort[];
  blocked: FinderBlockedPort[];
  /** Suggested ports, outlined so they are easy to find in the strip. */
  suggested: Set<number>;
}

function PortStrip({ span, used, blocked, suggested }: PortStripProps) {
  const { t } = useTranslation();
  const size = span.port_to - span.port_from + 1;
  const usedBy = new Map(
    used.filter((u) => u.port >= span.port_from && u.port <= span.port_to).map((u) => [u.port, u]),
  );
  const blockedBy = new Map<number, string[]>();
  for (const b of blocked) {
    if (b.port < span.port_from || b.port > span.port_to) continue;
    blockedBy.set(b.port, [...(blockedBy.get(b.port) ?? []), b.src_host_name]);
  }
  const taken = new Set([...usedBy.keys(), ...blockedBy.keys()]);
  const freeCount = size - taken.size;

  function state(port: number): PortState {
    if (blockedBy.has(port)) return 'blocked';
    if (usedBy.has(port)) return 'used';
    return 'free';
  }

  function tip(port: number): string {
    const s = state(port);
    if (s === 'blocked') return t('network.finder.cellBlocked', { port, sources: blockedBy.get(port)!.join(', ') });
    if (s === 'used') {
      const u = usedBy.get(port)!;
      return u.kind === 'listening'
        ? t('network.finder.cellListening', { port })
        : t('network.finder.cellUsed', { port, service: u.service_name });
    }
    return t('network.finder.cellFree', { port });
  }

  const label = t('network.finder.stripLabel', {
    range: portRange(span.port_from, span.port_to), free: freeCount, total: size,
  });

  return (
    <div className="port-strip">
      <div className="port-strip__head">
        <span className="mono">{portRange(span.port_from, span.port_to)}</span>
        <span className="network-sub">{t('network.finder.freeOf', { free: freeCount.toLocaleString(), total: size.toLocaleString() })}</span>
      </div>
      {size <= CELL_MAX ? (
        <ul className="port-strip__cells" aria-label={label}>
          {Array.from({ length: size }, (_, i) => span.port_from + i).map((port) => (
            <li
              key={port}
              className={`port-strip__cell port-strip__cell--${state(port)}${suggested.has(port) ? ' port-strip__cell--suggested' : ''}`}
              title={tip(port)}
              aria-label={tip(port)}
            />
          ))}
        </ul>
      ) : (
        <div className="port-strip__bar" role="img" aria-label={label}>
          {[...taken].sort((a, b) => a - b).map((port) => (
            <span
              key={port}
              className={`port-strip__tick port-strip__tick--${state(port)}`}
              style={{ left: `${((port - span.port_from) / Math.max(1, size - 1)) * 100}%` }}
              title={tip(port)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default PortStrip;
