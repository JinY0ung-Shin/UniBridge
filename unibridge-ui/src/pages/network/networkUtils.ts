import type { FirewallOpening } from '../../api/network';

/** Every network query lives under this key, so one invalidation refreshes the
 *  overview, lists and host details after any write. */
export const NETWORK_KEY = ['network'] as const;

export function portRange(from: number, to: number): string {
  return from === to ? String(from) : `${from}-${to}`;
}

export function openingPorts(opening: Pick<FirewallOpening, 'protocol' | 'port_from' | 'port_to'>): string {
  return `${opening.protocol}/${portRange(opening.port_from, opening.port_to)}`;
}

export function openingSource(opening: FirewallOpening): string {
  if (opening.src_cidr) return opening.src_cidr;
  return opening.src_host_name ?? opening.src_host_ip ?? '';
}

/** Matrix keys: a region id, or "none" for "outside every region / unassigned". */
export function regionKey(id: number | null): string {
  return id == null ? 'none' : String(id);
}

/** Comma, whitespace or newline separated list → trimmed non-empty entries. */
export function splitList(value: string): string[] {
  return value.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);
}

export function intOrUndefined(value: string): number | undefined {
  const trimmed = value.trim();
  if (trimmed === '') return undefined;
  const n = Number(trimmed);
  return Number.isInteger(n) ? n : undefined;
}
