import type { FirewallOpening } from '../../api/network';

/** Every network query lives under this key, so one invalidation refreshes the
 *  overview, lists and host details after any write. */
export const NETWORK_KEY = ['network'] as const;

/**
 * The backend's reason for a failure. FastAPI sends `detail` as a string
 * (HTTPException), an array of `{msg}` (body validation) or, for a duplicate
 * opening, an object `{message, id}`.
 */
export function networkErrorMessage(err: unknown, fallback = ''): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
        .filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const { message, id } = detail as { message: unknown; id?: unknown };
      return id != null ? `${String(message)} (#${String(id)})` : String(message);
    }
  }
  return fallback;
}

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

export function parseRegionKey(value: string | null): number | null | undefined {
  if (value == null || value === '') return undefined;
  if (value === 'none') return null;
  const n = Number(value);
  return Number.isInteger(n) ? n : undefined;
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
