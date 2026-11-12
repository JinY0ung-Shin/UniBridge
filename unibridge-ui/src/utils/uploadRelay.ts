/**
 * Upload relay page helpers (pages/UploadRelay.tsx).
 *
 * The Noah app opens `/upload-relay#t=<ticket>` in a popup. The ticket is
 * Noah's signed permission to upload files for one of its users; this page
 * can read it but only the backend can verify it.
 */

export const UPLOAD_RELAY_PATH = '/upload-relay';
export const TICKET_STORAGE_KEY = 'uploadRelayTicket';
/** The ticket travels in this header so the backend can verify it before it reads the file. */
export const TICKET_HEADER = 'X-Upload-Relay-Ticket';

const API_BASE = '/_api/upload-relay';

export function isUploadRelayPath(pathname: string): boolean {
  return pathname === UPLOAD_RELAY_PATH || pathname === `${UPLOAD_RELAY_PATH}/`;
}

function readStoredTicket(): string | null {
  try {
    return window.sessionStorage.getItem(TICKET_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** The ticket in the address bar's fragment (`#t=…`), if there is one. */
export function fragmentTicket(): string | null {
  const hash = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '';
  return hash ? new URLSearchParams(hash).get('t') || null : null;
}

/** Start the page over (a seam for tests: jsdom cannot navigate). */
export function reloadPage(): void {
  window.location.reload();
}

/**
 * Take the ticket out of the address bar and keep it for this tab.
 *
 * Must run BEFORE Keycloak starts: signing in redirects this tab away and
 * back, the address it returns to must be the plain page (an OIDC redirect URI
 * carries no fragment of ours), and the ticket has to survive the trip, which
 * sessionStorage does. Only our own `t=` fragment is consumed; the fragment
 * Keycloak returns with (`#state=…&code=…`) is left for keycloak-js. Safe to
 * call twice (React StrictMode): the second call finds the stored ticket.
 */
export function captureTicket(): string | null {
  const fromHash = fragmentTicket();
  if (!fromHash) return readStoredTicket();
  try {
    window.sessionStorage.setItem(TICKET_STORAGE_KEY, fromHash);
  } catch {
    /* storage refused: the ticket still works until the sign-in redirect */
  }
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
  return fromHash;
}

export interface TicketInfo {
  user: string;
  name: string | null;
  expiresAt: string;
  /** Seconds left on the ticket by the SERVER's clock; the PC's may be wrong. */
  expiresInSeconds: number;
  accept: string[];
  maxBytes: number;
  maxFiles: number;
  remainingFiles: number;
}

export interface StoredFile {
  id: string;
  name: string;
  size: number;
  sha256: string;
}

/** Why the backend (or this page) turned something away, as a locale key. */
export type RelayProblem =
  | 'invalidTicket'
  | 'expired'
  | 'signedOut'
  | 'notApproved'
  | 'type'
  | 'empty'
  | 'name'
  | 'tooLarge'
  | 'tooMany'
  | 'userQuota'
  | 'unavailable'
  | 'network'
  | 'unknown';

export class RelayError extends Error {
  readonly problem: RelayProblem;
  readonly status: number;

  constructor(problem: RelayProblem, status: number) {
    super(problem);
    this.problem = problem;
    this.status = status;
  }
}

const DETAIL_PROBLEMS: Record<string, RelayProblem> = {
  'Invalid upload ticket': 'invalidTicket',
  'The file is empty': 'empty',
  'This type of file is not accepted here': 'type',
  'The file name is not usable': 'name',
};

/** The backend answers in English; the page speaks the user's language. */
export function problemFor(status: number, detail: unknown): RelayProblem {
  if (status === 400) {
    return (typeof detail === 'string' && DETAIL_PROBLEMS[detail]) || 'unknown';
  }
  // The backend's gate is "an approved account": a login it turns away is, for
  // a page that just signed in, an account without an app role.
  if (status === 401) return 'notApproved';
  if (status === 409) return 'tooMany';
  if (status === 410) return 'expired';
  if (status === 413) return 'tooLarge';
  if (status === 429) return 'userQuota';
  if (status === 503) return 'unavailable';
  return 'unknown';
}

async function relayFetch<T>(path: string, token: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { ...(init.headers as Record<string, string> | undefined), Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new RelayError('network', 0);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* an error page from a proxy: judged by its status alone */
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : null;
    throw new RelayError(problemFor(response.status, detail), response.status);
  }
  return body as T;
}

export function describeTicket(ticket: string, token: string): Promise<TicketInfo> {
  return relayFetch<TicketInfo>('/ticket', token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket }),
  });
}

export function uploadRelayFile(ticket: string, file: File, token: string): Promise<StoredFile> {
  const form = new FormData();
  form.append('file', file, file.name);
  // In a header, not a form field: the backend checks the ticket before it
  // reads a byte of the file, and reads no more than this ticket allows.
  return relayFetch<StoredFile>('/files', token, {
    method: 'POST',
    headers: { [TICKET_HEADER]: ticket },
    body: form,
  });
}

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
}

/** What this page can tell is wrong with a file before sending it. */
export function localProblem(file: File, info: TicketInfo): RelayProblem | null {
  if (!info.accept.includes(fileExtension(file.name))) return 'type';
  if (file.size === 0) return 'empty';
  if (file.size > info.maxBytes) return 'tooLarge';
  return null;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
