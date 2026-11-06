import { useId, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { Region, RegionLink } from '../../api/network';
import { regionKey } from './networkUtils';

/* Inline-SVG map of regions (nodes) and the openings between them (directed
 * edges). Same data and click behaviour as the matrix; no chart dependency. */

const NODE_W = 170;
const NODE_H = 50;
const PAD = 70; // room for curved edges and self-loops outside the nodes
const CIRCLE_MAX = 10; // more nodes than this are laid out on a staggered grid

export interface GraphSelection {
  src: number | null;
  dst: number | null;
}

interface GraphNode {
  id: number | null;
  name: string;
  hostCount: number | null;
  x: number;
  y: number;
}

interface Point {
  x: number;
  y: number;
}

function layout(items: Array<Omit<GraphNode, 'x' | 'y'>>): { nodes: GraphNode[]; width: number; height: number } {
  const n = items.length;
  let raw: Point[];
  if (n <= 1) {
    raw = items.map(() => ({ x: 0, y: 0 }));
  } else if (n <= CIRCLE_MAX) {
    // An ellipse: boxes are wider than tall. Two nodes sit side by side.
    const r = Math.max(170, (n * (NODE_W + 60)) / (2 * Math.PI));
    const ry = n === 2 ? r : Math.max(r * 0.72, 120);
    const start = n === 2 ? Math.PI : -Math.PI / 2;
    raw = items.map((_, i) => {
      const a = start + (2 * Math.PI * i) / n;
      return { x: r * Math.cos(a), y: ry * Math.sin(a) };
    });
  } else {
    // Staggered grid so straight runs between rows don't cross nodes head-on.
    const cols = Math.ceil(Math.sqrt(n));
    const gapX = NODE_W + 70;
    const gapY = NODE_H + 100;
    raw = items.map((_, i) => {
      const row = Math.floor(i / cols);
      const col = i % cols;
      return { x: col * gapX + (row % 2 ? gapX / 2 : 0), y: row * gapY };
    });
  }
  const minX = Math.min(...raw.map((p) => p.x));
  const minY = Math.min(...raw.map((p) => p.y));
  const maxX = Math.max(...raw.map((p) => p.x));
  const maxY = Math.max(...raw.map((p) => p.y));
  const offX = PAD + NODE_W / 2 - minX;
  const offY = PAD + NODE_H / 2 - minY;
  return {
    nodes: items.map((item, i) => ({ ...item, x: raw[i].x + offX, y: raw[i].y + offY })),
    width: maxX - minX + NODE_W + PAD * 2,
    height: maxY - minY + NODE_H + PAD * 2,
  };
}

/** Where the ray from a node's centre toward `toward` leaves its box (plus a gap). */
function boxExit(c: Point, toward: Point, gap: number): Point {
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  if (dx === 0 && dy === 0) return c;
  const sx = dx === 0 ? Infinity : (NODE_W / 2 + gap) / Math.abs(dx);
  const sy = dy === 0 ? Infinity : (NODE_H / 2 + gap) / Math.abs(dy);
  const s = Math.min(sx, sy);
  return { x: c.x + dx * s, y: c.y + dy * s };
}

interface EdgeGeometry {
  d: string;
  label: Point;
}

function edgeGeometry(s: GraphNode, t: GraphNode): EdgeGeometry {
  if (s === t) {
    // Outside → outside (a CIDR source to an unassigned host): a loop above the node.
    const top = s.y - NODE_H / 2;
    return {
      d: `M ${s.x - 24} ${top - 2} C ${s.x - 50} ${top - 58}, ${s.x + 50} ${top - 58}, ${s.x + 24} ${top - 4}`,
      label: { x: s.x, y: top - 44 },
    };
  }
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const dist = Math.hypot(dx, dy);
  // Always bend to the left of the direction of travel: A→B and B→A bow apart.
  const bend = Math.min(60, Math.max(24, dist * 0.15));
  const ctrl = { x: (s.x + t.x) / 2 + (-dy / dist) * bend, y: (s.y + t.y) / 2 + (dx / dist) * bend };
  const a = boxExit(s, ctrl, 2);
  const b = boxExit(t, ctrl, 4);
  const r = (v: number) => Math.round(v * 10) / 10;
  return {
    d: `M ${r(a.x)} ${r(a.y)} Q ${r(ctrl.x)} ${r(ctrl.y)} ${r(b.x)} ${r(b.y)}`,
    label: { x: 0.25 * a.x + 0.5 * ctrl.x + 0.25 * b.x, y: 0.25 * a.y + 0.5 * ctrl.y + 0.25 * b.y },
  };
}

/** Rough text width: CJK glyphs are about twice as wide as Latin ones. */
function textWidth(text: string, latin = 7, wide = 12): number {
  let w = 0;
  for (const ch of text) w += ch.charCodeAt(0) >= 0x1100 ? wide : latin;
  return w;
}

/** A node name cut to fit its box (the full name is in the node's tooltip). */
function truncate(name: string): string {
  const fits = (s: string) => textWidth(s, 6.8, 13) <= NODE_W - 14;
  if (fits(name)) return name;
  let cut = name;
  while (cut.length > 1 && !fits(`${cut}…`)) cut = cut.slice(0, -1);
  return `${cut}…`;
}

interface RegionGraphProps {
  regions: Region[];
  links: RegionLink[];
  outsideName: string;
  selected: GraphSelection | null;
  onSelect: (selection: GraphSelection | null) => void;
}

function RegionGraph({ regions, links, outsideName, selected, onSelect }: RegionGraphProps) {
  const { t } = useTranslation();
  const markerBase = `network-graph-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;

  const hasOutside = links.some((l) => l.src_region_id == null || l.dst_region_id == null);
  const items = regions.map((r) => ({ id: r.id as number | null, name: r.name, hostCount: r.host_count as number | null }));
  if (hasOutside) items.push({ id: null, name: outsideName, hostCount: null });
  const { nodes, width, height } = layout(items);
  const byKey = new Map(nodes.map((n) => [regionKey(n.id), n]));

  const edges = links.flatMap((link) => {
    const s = byKey.get(regionKey(link.src_region_id));
    const d = byKey.get(regionKey(link.dst_region_id));
    return s && d ? [{ link, s, d, geo: edgeGeometry(s, d) }] : [];
  });

  function toggle(link: RegionLink) {
    const isSelected = selected?.src === link.src_region_id && selected?.dst === link.dst_region_id;
    onSelect(isSelected ? null : { src: link.src_region_id, dst: link.dst_region_id });
  }

  function onKey(event: KeyboardEvent<SVGGElement>, link: RegionLink) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle(link);
    }
  }

  return (
    <div className="network-graph-scroll">
      <svg
        className="network-graph"
        viewBox={`0 0 ${Math.round(width)} ${Math.round(height)}`}
        // Shrink to fit down to 3/4 of natural size (and never below 560px);
        // past that the box scrolls sideways so labels stay readable.
        style={{ maxWidth: Math.round(width), minWidth: Math.round(Math.min(width, Math.max(560, width * 0.75))) }}
        role="group"
        aria-label={t('network.overview.graphTitle')}
      >
        <defs>
          {(['default', 'warn', 'selected'] as const).map((tone) => (
            <marker
              key={tone}
              id={`${markerBase}-${tone}`}
              className={`network-graph__arrow network-graph__arrow--${tone}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="9"
              markerHeight="9"
              markerUnits="userSpaceOnUse"
              orient="auto"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          ))}
        </defs>

        {/* Nodes first, so edge labels near a box stay on top of it. */}
        {nodes.map((n) => (
          <g key={regionKey(n.id)} className={`network-graph__node${n.id == null ? ' network-graph__node--outside' : ''}`}>
            <title>{n.name}</title>
            <rect x={n.x - NODE_W / 2} y={n.y - NODE_H / 2} width={NODE_W} height={NODE_H} rx={10} />
            <text className="network-graph__name" x={n.x} y={n.y - 7} textAnchor="middle" dominantBaseline="central">
              {truncate(n.name)}
            </text>
            <text className="network-graph__sub" x={n.x} y={n.y + 12} textAnchor="middle" dominantBaseline="central">
              {n.hostCount == null
                ? t('network.overview.graphOutsideSub')
                : t('network.overview.graphHosts', { count: n.hostCount })}
            </text>
          </g>
        ))}

        {edges.map(({ link, s, d, geo }) => {
          const isSelected = selected?.src === link.src_region_id && selected?.dst === link.dst_region_id;
          const closedOnly = link.open_openings === 0;
          const tone = isSelected ? 'selected' : link.unverified > 0 ? 'warn' : 'default';
          const ports = link.ports.join(', ');
          const label = t('network.overview.edgeLabel', {
            src: s.name, dst: d.name, count: link.open_openings, unverified: link.unverified, ports,
          });
          const countText = String(link.open_openings);
          const warnText = link.unverified > 0 ? ` · ${t('network.overview.unverifiedCount', { count: link.unverified })}` : '';
          const boxW = textWidth(countText) + textWidth(warnText) + 14;
          return (
            <g
              key={`${regionKey(link.src_region_id)}:${regionKey(link.dst_region_id)}`}
              className={[
                'network-graph__edge',
                isSelected ? 'network-graph__edge--selected' : '',
                closedOnly ? 'network-graph__edge--closed' : '',
                link.unverified > 0 ? 'network-graph__edge--warn' : '',
              ].filter(Boolean).join(' ')}
              role="button"
              tabIndex={0}
              aria-pressed={isSelected}
              aria-label={label}
              onClick={() => toggle(link)}
              onKeyDown={(e) => onKey(e, link)}
            >
              <title>{`${s.name} → ${d.name}\n${ports}`}</title>
              <path className="network-graph__hit" d={geo.d} />
              <path
                className="network-graph__line"
                d={geo.d}
                style={{ strokeWidth: 1.5 + Math.min(3, Math.log2(1 + link.open_openings)) }}
                markerEnd={`url(#${markerBase}-${tone})`}
              />
              <rect
                className="network-graph__label-bg"
                x={geo.label.x - boxW / 2}
                y={geo.label.y - 10}
                width={boxW}
                height={20}
                rx={10}
              />
              <text className="network-graph__label" x={geo.label.x} y={geo.label.y} textAnchor="middle" dominantBaseline="central">
                <tspan className="network-graph__count">{countText}</tspan>
                {warnText && <tspan className="network-graph__warn">{warnText}</tspan>}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default RegionGraph;
