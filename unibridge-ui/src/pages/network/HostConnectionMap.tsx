import { useId, useRef, useState, type FocusEvent, type KeyboardEvent, type MouseEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getNetworkHost,
  type FirewallOpening,
  type NetworkHost,
  type NetworkHostDetail,
  type PortUsage,
} from '../../api/network';
import { NETWORK_KEY, openingPorts } from './networkUtils';
import { OpeningsTable } from './NetworkTables';

/* One host in the middle, what reaches it on the left (inbound openings: hosts
 * or CIDR ranges), what it reaches on the right (outbound openings, its own and
 * those of a range it is in), and its own region below (every port, both ways).
 * Clicking a peer host re-centres the map on it. */

const NODE_W = 190;
const NODE_H = 52;
const GAP_Y = 18;
const COL_GAP = 210; // room between columns for the edges and their port labels
const PAD = 24;
const PEER_MAX = 30; // more peers on one side than this are listed in the table only
const CENTER_W = 230;

type Side = 'in' | 'out';

interface Peer {
  key: string;
  side: Side;
  hostId: number | null;
  name: string;
  sub: string;
  region: string | null;
  openings: FirewallOpening[];
  /** Outbound through a CIDR the centre host is in, rather than by name. */
  viaCidr: boolean;
}

interface Selection {
  kind: 'peer' | 'region';
  key: string;
}

function peersOf(detail: NetworkHostDetail): { inbound: Peer[]; outbound: Peer[] } {
  const inbound = new Map<string, Peer>();
  for (const o of detail.inbound) {
    const key = o.src_host_id != null ? `in:h${o.src_host_id}` : `in:c${o.src_cidr}`;
    const peer = inbound.get(key) ?? {
      key,
      side: 'in' as const,
      hostId: o.src_host_id,
      name: o.src_host_name ?? o.src_cidr ?? '?',
      sub: o.src_host_id != null ? o.src_host_ip ?? '' : 'CIDR',
      region: o.src_region_name,
      openings: [],
      viaCidr: false,
    };
    peer.openings.push(o);
    inbound.set(key, peer);
  }
  const outbound = new Map<string, Peer>();
  for (const [o, viaCidr] of [
    ...detail.outbound.map((o) => [o, false] as const),
    ...detail.outbound_via_cidr.map((o) => [o, true] as const),
  ]) {
    const key = `out:h${o.dst_host_id}`;
    const peer = outbound.get(key) ?? {
      key,
      side: 'out' as const,
      hostId: o.dst_host_id,
      name: o.dst_host_name,
      sub: o.dst_host_ip,
      region: o.dst_region_name,
      openings: [],
      viaCidr,
    };
    peer.openings.push(o);
    peer.viaCidr = peer.viaCidr && viaCidr;
    outbound.set(key, peer);
  }
  const order = (a: Peer, b: Peer) =>
    (a.region ?? '￿').localeCompare(b.region ?? '￿') || a.name.localeCompare(b.name);
  return { inbound: [...inbound.values()].sort(order), outbound: [...outbound.values()].sort(order) };
}

function peerTone(peer: Peer): 'mismatch' | 'closed' | 'warn' | 'open' {
  if (peer.openings.some((o) => o.last_probe?.mismatch)) return 'mismatch';
  if (peer.openings.every((o) => o.status === 'closed')) return 'closed';
  if (peer.openings.some((o) => !o.verified)) return 'warn';
  return 'open';
}

/** Port list for an edge label, cut to fit (the full list is in the tooltip). */
function portsText(openings: FirewallOpening[], max = 20): string {
  const open = openings.filter((o) => o.status === 'open');
  const labels = [...new Set((open.length ? open : openings).map((o) => openingPorts(o).replace(/^tcp\//, '')))];
  let text = '';
  for (let i = 0; i < labels.length; i++) {
    const next = text ? `${text}, ${labels[i]}` : labels[i];
    if (next.length > max) return `${text || labels[i].slice(0, max)} +${labels.length - i - (text ? 0 : 1)}`.replace(/ \+0$/, '');
    text = next;
  }
  return text;
}

/** What listens on one opening's ports at its destination. */
interface OpeningPortInfo {
  opening: FirewallOpening;
  /** Recorded or discovered services inside the opening's ports. */
  services: PortUsage[];
  /** Ports of the opening with no service on record. */
  freeCount: number;
}

/** Services on the destination of each opening (one entry per opening, by port). */
function portInfo(openings: FirewallOpening[], usages: PortUsage[]): OpeningPortInfo[] {
  return [...openings]
    .sort((a, b) => a.port_from - b.port_from || a.port_to - b.port_to || a.id - b.id)
    .map((opening) => {
      const services = usages
        .filter((u) => u.host_id === opening.dst_host_id && u.protocol === opening.protocol
          && u.port >= opening.port_from && u.port <= opening.port_to)
        .sort((a, b) => a.port - b.port || a.service_name.localeCompare(b.service_name));
      const used = new Set(services.map((u) => u.port)).size;
      return { opening, services, freeCount: opening.port_to - opening.port_from + 1 - used };
    });
}

const TIP_MAX_OPENINGS = 8;
const TIP_MAX_SERVICES = 6;
const TIP_W = 320;

interface EdgeTip {
  key: string;
  x: number;
  y: number;
}

function textWidth(text: string): number {
  let w = 0;
  for (const ch of text) w += ch.charCodeAt(0) >= 0x1100 ? 12 : 6.6;
  return w;
}

function fit(text: string, width: number): string {
  if (textWidth(text) <= width) return text;
  let cut = text;
  while (cut.length > 1 && textWidth(`${cut}…`) > width) cut = cut.slice(0, -1);
  return `${cut}…`;
}

interface HostConnectionMapProps {
  hostId: number;
  /** Called with a peer host's id when it is clicked (re-centre). */
  onRecenter: (hostId: number) => void;
  /** Links to the host page and the port finder (off on the host page itself). */
  showActions?: boolean;
}

function HostConnectionMap({ hostId, onRecenter, showActions = true }: HostConnectionMapProps) {
  const { t } = useTranslation();
  const markerId = `host-map-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const tipId = `host-map-tip-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const boxRef = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [tip, setTip] = useState<EdgeTip | null>(null);
  const [selectedFor, setSelectedFor] = useState(hostId);
  // A new centre starts with nothing selected.
  if (selectedFor !== hostId) {
    setSelectedFor(hostId);
    setSelected(null);
    setTip(null);
  }

  const query = useQuery({
    queryKey: [...NETWORK_KEY, 'host', hostId],
    queryFn: () => getNetworkHost(hostId),
  });

  if (query.isLoading) return <div className="loading-message" role="status">{t('common.loading')}</div>;
  if (query.isError || !query.data) {
    return <div className="error-banner" role="alert">{t('network.hosts.notFound')}</div>;
  }
  const detail = query.data;
  const { inbound, outbound } = peersOf(detail);
  const shownIn = inbound.slice(0, PEER_MAX);
  const shownOut = outbound.slice(0, PEER_MAX);
  const sameRegion = detail.same_region_hosts;

  const rows = Math.max(shownIn.length, shownOut.length, 1);
  const colH = rows * NODE_H + (rows - 1) * GAP_Y;
  const usedPorts = detail.usages.slice(0, 6);
  const centerH = Math.max(NODE_H + 18, 64 + usedPorts.length * 18 + (detail.usages.length > 6 ? 18 : 0));
  const regionNodeH = 46;
  const bodyH = Math.max(colH, centerH);
  const leftX = PAD;
  const centerX = PAD + NODE_W + COL_GAP;
  const rightX = centerX + CENTER_W + COL_GAP;
  const width = rightX + NODE_W + PAD;
  const centerY = PAD + (bodyH - centerH) / 2;
  const regionY = PAD + bodyH + 46;
  const height = (detail.region_id != null ? regionY + regionNodeH : PAD + bodyH) + PAD;

  function columnY(count: number, i: number): number {
    const h = count * NODE_H + (count - 1) * GAP_Y;
    return PAD + (bodyH - h) / 2 + i * (NODE_H + GAP_Y);
  }

  /** Edge endpoints spread along the centre box's side, in peer order. */
  function anchorY(count: number, i: number): number {
    const top = centerY + 14;
    const span = centerH - 28;
    return count <= 1 ? centerY + centerH / 2 : top + (span * i) / (count - 1);
  }

  function toggle(sel: Selection) {
    setSelected((cur) => (cur && cur.kind === sel.kind && cur.key === sel.key ? null : sel));
  }

  function onKey(event: KeyboardEvent<SVGGElement>, action: () => void) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      action();
    }
  }

  const selectedPeer = selected?.kind === 'peer'
    ? [...inbound, ...outbound].find((p) => p.key === selected.key) ?? null
    : null;

  /** Inbound edges end at this host; outbound ones at the peer. */
  function servicesFor(peer: Peer): PortUsage[] {
    return peer.side === 'in' ? detail.usages : detail.outbound_usages ?? [];
  }

  /** The tooltip sits at the pointer (or under the edge's label on focus),
   *  inside the map box and flipped to stay in it. */
  function placeTip(key: string, clientX: number, clientY: number) {
    const box = boxRef.current?.getBoundingClientRect();
    if (!box) return;
    let x = clientX - box.left + 14;
    if (x + TIP_W > box.width) x = Math.max(0, clientX - box.left - TIP_W - 14);
    setTip({ key, x, y: clientY - box.top + 14 });
  }

  function onEdgeMouse(event: MouseEvent<SVGGElement>, key: string) {
    placeTip(key, event.clientX, event.clientY);
  }

  function onEdgeFocus(event: FocusEvent<SVGGElement>, key: string) {
    const label = event.currentTarget.querySelector('rect.host-map__label-bg') ?? event.currentTarget;
    const r = label.getBoundingClientRect();
    placeTip(key, r.left, r.bottom - 8);
  }

  const tipPeer = tip ? [...shownIn, ...shownOut].find((p) => p.key === tip.key) ?? null : null;

  function probeNote(o: FirewallOpening): string | null {
    const probe = o.last_probe;
    if (!probe || o.port_from !== o.port_to) return null;
    return t(`network.hostMap.tipProbe.${probe.result}`, { defaultValue: '' }) || null;
  }

  function renderTip(peer: Peer) {
    const dstName = peer.side === 'in' ? detail.name : peer.name;
    const infos = portInfo(peer.openings, servicesFor(peer));
    return (
      <div
        id={tipId}
        role="tooltip"
        className="host-map__tip"
        style={{ left: tip!.x, top: tip!.y, width: TIP_W }}
      >
        <div className="host-map__tip-title">
          {peer.side === 'in' ? `${peer.name} → ${detail.name}` : `${detail.name} → ${peer.name}`}
        </div>
        <div className="host-map__tip-sub">{t('network.hostMap.tipServicesOn', { host: dstName })}</div>
        <ul className="host-map__tip-list">
          {infos.slice(0, TIP_MAX_OPENINGS).map(({ opening: o, services, freeCount }) => {
            const single = o.port_from === o.port_to;
            const note = probeNote(o);
            return (
              <li key={o.id} className={o.status === 'closed' ? 'host-map__tip-item--closed' : undefined}>
                <div className="host-map__tip-head">
                  <span className="mono">{openingPorts(o)}</span>
                  {o.status === 'closed' && <span className="badge badge-unknown">{t('network.status.closed')}</span>}
                  {!o.verified && <span className="badge badge-warn">{t('network.unverified')}</span>}
                </div>
                {services.slice(0, TIP_MAX_SERVICES).map((u, i) => (
                  <div key={`${u.port}-${u.service_name}-${i}`} className="host-map__tip-svc">
                    <span className="mono host-map__tip-port">{u.port}</span>
                    <span>{u.service_name || t('network.hostMap.tipUnnamed')}</span>
                    {u.source === 'auto' && <span className="network-sub">{t('network.hostMap.tipDiscovered')}</span>}
                  </div>
                ))}
                {services.length > TIP_MAX_SERVICES && (
                  <div className="network-sub">{t('network.hostMap.tipMoreServices', { count: services.length - TIP_MAX_SERVICES })}</div>
                )}
                {services.length === 0 ? (
                  <div className="host-map__tip-free">
                    {single ? t('network.hostMap.tipFree') : t('network.hostMap.tipAllFree', { count: freeCount })}
                  </div>
                ) : freeCount > 0 && (
                  <div className="host-map__tip-free">{t('network.hostMap.tipRestFree', { count: freeCount })}</div>
                )}
                {note && <div className="network-sub">{note}</div>}
              </li>
            );
          })}
        </ul>
        {infos.length > TIP_MAX_OPENINGS && (
          <div className="network-sub">{t('network.hostMap.tipMoreOpenings', { count: infos.length - TIP_MAX_OPENINGS })}</div>
        )}
      </div>
    );
  }

  function renderPeer(peer: Peer, i: number, count: number) {
    const x = peer.side === 'in' ? leftX : rightX;
    const y = columnY(count, i);
    const tone = peerTone(peer);
    const isSelected = selected?.kind === 'peer' && selected.key === peer.key;
    // Edge from the peer's inner side to the centre box (direction = traffic).
    const peerEdgeX = peer.side === 'in' ? x + NODE_W : x;
    const centerEdgeX = peer.side === 'in' ? centerX : centerX + CENTER_W;
    const py = y + NODE_H / 2;
    const cy = anchorY(count, i);
    const [x1, y1, x2, y2] = peer.side === 'in'
      ? [peerEdgeX, py, centerEdgeX - 3, cy]
      : [centerEdgeX, cy, peerEdgeX - 3, py];
    const mid = (x1 + x2) / 2;
    const d = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
    // Label next to the peer, where the edges are still apart; it grows away
    // from the peer box so a long port list never covers it.
    const ly = py;
    const ports = `${portsText(peer.openings)}${peer.viaCidr ? ` · ${t('network.hostMap.viaCidr')}` : ''}`;
    const fullPorts = peer.openings.map((o) => `${openingPorts(o)}${o.status === 'closed' ? ` (${t('network.status.closed')})` : ''}`).join(', ');
    const label = t(peer.side === 'in' ? 'network.hostMap.inEdge' : 'network.hostMap.outEdge', {
      peer: peer.name, host: detail.name, ports: fullPorts,
    });
    const boxW = textWidth(ports) + 16;
    const boxX = peer.side === 'in' ? peerEdgeX + 10 : peerEdgeX - 13 - boxW;
    return (
      <g key={peer.key}>
        <g
          className={`host-map__edge host-map__edge--${tone}${isSelected ? ' host-map__edge--selected' : ''}`}
          role="button"
          tabIndex={0}
          aria-pressed={isSelected}
          aria-label={label}
          aria-describedby={tip?.key === peer.key ? tipId : undefined}
          onClick={() => toggle({ kind: 'peer', key: peer.key })}
          onKeyDown={(e) => onKey(e, () => toggle({ kind: 'peer', key: peer.key }))}
          onMouseEnter={(e) => onEdgeMouse(e, peer.key)}
          onMouseMove={(e) => onEdgeMouse(e, peer.key)}
          onMouseLeave={() => setTip((cur) => (cur?.key === peer.key ? null : cur))}
          onFocus={(e) => onEdgeFocus(e, peer.key)}
          onBlur={() => setTip((cur) => (cur?.key === peer.key ? null : cur))}
        >
          <path className="host-map__hit" d={d} />
          <path className="host-map__line" d={d} markerEnd={`url(#${markerId}-${isSelected ? 'selected' : tone})`} />
          <rect className="host-map__label-bg" x={boxX} y={ly - 10} width={boxW} height={20} rx={10} />
          <text className="host-map__label" x={boxX + boxW / 2} y={ly} textAnchor="middle" dominantBaseline="central">{ports}</text>
        </g>
        <g
          className={`host-map__node${peer.hostId == null ? ' host-map__node--cidr' : ' host-map__node--link'}${isSelected ? ' host-map__node--selected' : ''}`}
          role="button"
          tabIndex={0}
          aria-label={peer.hostId != null
            ? t('network.hostMap.recenter', { name: peer.name })
            : t('network.hostMap.showOpenings', { name: peer.name })}
          onClick={() => (peer.hostId != null ? onRecenter(peer.hostId) : toggle({ kind: 'peer', key: peer.key }))}
          onKeyDown={(e) => onKey(e, () => (peer.hostId != null ? onRecenter(peer.hostId) : toggle({ kind: 'peer', key: peer.key })))}
        >
          <title>{`${peer.name}${peer.sub ? ` (${peer.sub})` : ''}\n${peer.region ?? t('network.noRegion')}`}</title>
          <rect x={x} y={y} width={NODE_W} height={NODE_H} rx={8} />
          <text className="host-map__name" x={x + 12} y={y + 18} dominantBaseline="central">{fit(peer.name, NODE_W - 24)}</text>
          <text className="host-map__sub" x={x + 12} y={y + 36} dominantBaseline="central">
            {fit(
              [peer.region ?? t('network.noRegion'), peer.sub].filter(Boolean).join(' · '),
              NODE_W - 24,
            )}
          </text>
        </g>
      </g>
    );
  }

  const regionSelected = selected?.kind === 'region';

  return (
    <div className="host-map" ref={boxRef}>
      <div className="host-map__legend" aria-hidden="true">
        <span><i className="host-map__swatch host-map__swatch--open" />{t('network.hostMap.legendOpen')}</span>
        <span><i className="host-map__swatch host-map__swatch--warn" />{t('network.hostMap.legendUnverified')}</span>
        <span><i className="host-map__swatch host-map__swatch--mismatch" />{t('network.hostMap.legendMismatch')}</span>
        <span><i className="host-map__swatch host-map__swatch--closed" />{t('network.hostMap.legendClosed')}</span>
      </div>
      <div className="network-graph-scroll">
        <svg
          className="host-map__svg"
          viewBox={`0 0 ${Math.round(width)} ${Math.round(height)}`}
          style={{ maxWidth: Math.round(width), minWidth: Math.round(Math.min(width, 640)) }}
          role="group"
          aria-label={t('network.hostMap.title', { name: detail.name })}
        >
          <defs>
            {(['open', 'warn', 'mismatch', 'closed', 'selected'] as const).map((tone) => (
              <marker
                key={tone}
                id={`${markerId}-${tone}`}
                className={`host-map__arrow host-map__arrow--${tone}`}
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="8"
                markerHeight="8"
                markerUnits="userSpaceOnUse"
                orient="auto"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
            ))}
          </defs>

          <text className="host-map__column" x={leftX + NODE_W / 2} y={12} textAnchor="middle">
            {t('network.hostMap.inbound', { count: inbound.length })}
          </text>
          <text className="host-map__column" x={rightX + NODE_W / 2} y={12} textAnchor="middle">
            {t('network.hostMap.outbound', { count: outbound.length })}
          </text>

          {shownIn.map((p, i) => renderPeer(p, i, shownIn.length))}
          {shownOut.map((p, i) => renderPeer(p, i, shownOut.length))}
          {shownIn.length === 0 && (
            <text className="host-map__empty" x={leftX + NODE_W / 2} y={PAD + bodyH / 2} textAnchor="middle">
              {t('network.hostMap.noInbound')}
            </text>
          )}
          {shownOut.length === 0 && (
            <text className="host-map__empty" x={rightX + NODE_W / 2} y={PAD + bodyH / 2} textAnchor="middle">
              {t('network.hostMap.noOutbound')}
            </text>
          )}

          {detail.region_id != null && (
            <g
              className={`host-map__region${regionSelected ? ' host-map__region--selected' : ''}`}
              role="button"
              tabIndex={0}
              aria-pressed={regionSelected}
              aria-label={t('network.hostMap.sameRegion', { region: detail.region_name, count: sameRegion.length })}
              onClick={() => toggle({ kind: 'region', key: 'region' })}
              onKeyDown={(e) => onKey(e, () => toggle({ kind: 'region', key: 'region' }))}
            >
              <line className="host-map__region-line" x1={centerX + CENTER_W / 2} y1={centerY + centerH} x2={centerX + CENTER_W / 2} y2={regionY} />
              <rect x={centerX} y={regionY} width={CENTER_W} height={regionNodeH} rx={8} />
              <text className="host-map__name" x={centerX + CENTER_W / 2} y={regionY + 16} textAnchor="middle" dominantBaseline="central">
                {fit(t('network.hostMap.sameRegionTitle', { region: detail.region_name, count: sameRegion.length }), CENTER_W - 20)}
              </text>
              <text className="host-map__sub" x={centerX + CENTER_W / 2} y={regionY + 33} textAnchor="middle" dominantBaseline="central">
                {t('network.hostMap.allPorts')}
              </text>
            </g>
          )}

          <g className="host-map__center">
            <title>{`${detail.name} (${detail.ip})`}</title>
            <rect x={centerX} y={centerY} width={CENTER_W} height={centerH} rx={10} />
            <text className="host-map__center-name" x={centerX + 14} y={centerY + 20} dominantBaseline="central">
              {fit(detail.name, CENTER_W - 28)}
            </text>
            <text className="host-map__sub" x={centerX + 14} y={centerY + 40} dominantBaseline="central">
              {fit(`${detail.ip} · ${detail.region_name ?? t('network.noRegion')}`, CENTER_W - 28)}
            </text>
            {usedPorts.map((u, i) => (
              <text key={`${u.port}-${u.service_name}-${i}`} className="host-map__port" x={centerX + 14} y={centerY + 62 + i * 18} dominantBaseline="central">
                <tspan className="host-map__port-num">{`${u.port}`}</tspan>
                <tspan dx={8}>{fit(u.service_name, CENTER_W - 80)}</tspan>
              </text>
            ))}
            {detail.usages.length > 6 && (
              <text className="host-map__sub" x={centerX + 14} y={centerY + 62 + 6 * 18} dominantBaseline="central">
                {t('network.hostMap.morePorts', { count: detail.usages.length - 6 })}
              </text>
            )}
          </g>
        </svg>
      </div>
      {tipPeer && renderTip(tipPeer)}

      {(inbound.length > PEER_MAX || outbound.length > PEER_MAX) && (
        <div className="network-note">{t('network.hostMap.tooMany', { max: PEER_MAX })}</div>
      )}

      {selectedPeer && (
        <section className="network-section" aria-labelledby="host-map-selected">
          <div className="network-toolbar">
            <h3 id="host-map-selected" className="section-title">
              {selectedPeer.side === 'in'
                ? t('network.hostMap.selectedIn', { peer: selectedPeer.name, host: detail.name })
                : t('network.hostMap.selectedOut', { peer: selectedPeer.name, host: detail.name })}
            </h3>
            {selectedPeer.hostId != null && (
              <button type="button" className="btn btn-sm btn-secondary" onClick={() => onRecenter(selectedPeer.hostId!)}>
                {t('network.hostMap.recenterButton', { name: selectedPeer.name })}
              </button>
            )}
          </div>
          <OpeningsTable openings={selectedPeer.openings} label={t('network.hostMap.openingsLabel')} />
        </section>
      )}

      {regionSelected && (
        <section className="network-section" aria-labelledby="host-map-region">
          <h3 id="host-map-region" className="section-title">
            {t('network.hostMap.sameRegionTitle', { region: detail.region_name, count: sameRegion.length })}
          </h3>
          <p className="page-subtitle">{t('network.hostMap.sameRegionHint')}</p>
          {sameRegion.length === 0 ? (
            <div className="network-muted">{t('network.hostMap.sameRegionEmpty')}</div>
          ) : (
            <ul className="host-map__peer-list">
              {sameRegion.map((h: NetworkHost) => (
                <li key={h.id}>
                  <button type="button" className="link-button" onClick={() => onRecenter(h.id)}>{h.name}</button>
                  <span className="network-sub mono">{h.ip}</span>
                  {h.owner && <span className="network-sub">{h.owner}</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {showActions && (
        <div className="host-map__actions">
          <Link className="link-button" to={`/network/hosts/${detail.id}`}>{t('network.hostMap.openDetail')}</Link>
          <Link className="link-button" to={`/network?tab=finder&dst=${encodeURIComponent(detail.name)}`}>
            {t('network.hostMap.findPorts')}
          </Link>
        </div>
      )}
    </div>
  );
}

export default HostConnectionMap;
