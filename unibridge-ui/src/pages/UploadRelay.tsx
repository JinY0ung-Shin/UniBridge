import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import keycloak from '../keycloak';
import { resolveAppRole, type TokenClaims } from '../components/appRole';
import {
  RelayError,
  UPLOAD_RELAY_PATH,
  captureTicket,
  describeTicket,
  formatBytes,
  fragmentTicket,
  localProblem,
  reloadPage,
  uploadRelayFile,
  type RelayProblem,
  type TicketInfo,
} from '../utils/uploadRelay';
import './UploadRelay.css';

/**
 * Upload relay: the page the Noah app opens in a popup so that a file is
 * uploaded to UniBridge's address (which the workstation file-security agent
 * decrypts uploads for) and collected by the Noah server from there.
 *
 * It renders outside the app's AuthProvider and Layout (see Root.tsx) because
 * it is a popup with its own sign-in, not because it is open to more people:
 * like the app, it is for APPROVED UniBridge accounts only. An account without
 * an app role sees the pending-approval state here and gets a 401 from the
 * backend. The login names the uploader; Noah's signed ticket says for whom.
 * It never touches window.opener.
 */

type Phase =
  | { kind: 'signingIn' }
  | { kind: 'authError' }
  | { kind: 'pending' }
  | { kind: 'noTicket' }
  | { kind: 'ticketError'; problem: RelayProblem }
  | { kind: 'ready'; info: TicketInfo };

type RowState = 'queued' | 'uploading' | 'done' | 'refused';

interface FileRow {
  key: number;
  name: string;
  size: number;
  state: RowState;
  problem?: RelayProblem;
}

async function freshToken(): Promise<string> {
  try {
    await keycloak.updateToken(30);
  } catch {
    throw new RelayError('signedOut', 0);
  }
  return keycloak.token ?? '';
}

function problemOf(error: unknown): RelayProblem {
  return error instanceof RelayError ? error.problem : 'unknown';
}

/**
 * Where a signed-in visitor stands: waiting for approval, or looking at what
 * the ticket allows. The role is read from the token exactly as the app's
 * AuthProvider reads it; the backend has the last word (its 401 means the
 * same thing), so a token this page misjudged still ends in the pending state.
 */
async function enterRelay(ticket: string): Promise<Phase> {
  if (!resolveAppRole(keycloak.tokenParsed as TokenClaims)) return { kind: 'pending' };
  try {
    return { kind: 'ready', info: await describeTicket(ticket, await freshToken()) };
  } catch (error) {
    const problem = problemOf(error);
    return problem === 'notApproved' ? { kind: 'pending' } : { kind: 'ticketError', problem };
  }
}

export default function UploadRelay() {
  const { t } = useTranslation();
  // Before anything else, and before Keycloak: see captureTicket.
  const [ticket] = useState(() => captureTicket());
  const [phase, setPhase] = useState<Phase>(() => (ticket ? { kind: 'signingIn' } : { kind: 'noTicket' }));
  const [rows, setRows] = useState<FileRow[]>([]);
  const [expired, setExpired] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const initCalled = useRef(false);
  const nextKey = useRef(1);
  // Files go up one at a time: the backend holds each in memory as it arrives.
  const queue = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    if (!ticket || initCalled.current) return;
    initCalled.current = true;
    keycloak.onTokenExpired = () => {
      keycloak.updateToken(30).catch(() => { /* the next request reports it */ });
    };
    keycloak
      .init({ onLoad: 'login-required', checkLoginIframe: false, pkceMethod: 'S256' })
      .then(async (authenticated) => {
        setPhase(authenticated ? await enterRelay(ticket) : { kind: 'authError' });
      })
      .catch((error) => {
        console.error('Keycloak init failed:', error);
        setPhase({ kind: 'authError' });
      });
  }, [ticket]);

  // Noah reuses the popup: a second press of its attach button navigates this
  // window to the same page with a NEW ticket in the fragment, which is a
  // fragment-only change and does not reload the page. Take the new ticket and
  // start over, or the files would go up under the old one.
  useEffect(() => {
    function onHashChange() {
      if (!fragmentTicket()) return;
      captureTicket();
      reloadPage();
    }
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // Counted down from what the server said is left, never by comparing the
  // ticket's absolute expiry with this PC's clock (which may be minutes off).
  const expiresInSeconds = phase.kind === 'ready' ? phase.info.expiresInSeconds : null;
  useEffect(() => {
    if (typeof expiresInSeconds !== 'number') return;
    const remaining = Math.max(0, Math.min(expiresInSeconds * 1000, 2 ** 31 - 1));
    const timer = window.setTimeout(() => setExpired(true), remaining);
    return () => window.clearTimeout(timer);
  }, [expiresInSeconds]);

  function patchRow(key: number, patch: Partial<FileRow>) {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }

  function addFiles(files: File[]) {
    if (phase.kind !== 'ready' || expired || !ticket || !files.length) return;
    const { info } = phase;
    const added: FileRow[] = [];
    for (const file of files) {
      const key = nextKey.current++;
      const problem = localProblem(file, info);
      added.push({ key, name: file.name, size: file.size, state: problem ? 'refused' : 'queued', problem: problem ?? undefined });
      if (problem) continue;
      queue.current = queue.current.then(async () => {
        patchRow(key, { state: 'uploading' });
        try {
          await uploadRelayFile(ticket, file, await freshToken());
          patchRow(key, { state: 'done' });
        } catch (error) {
          const failure = problemOf(error);
          patchRow(key, { state: 'refused', problem: failure });
          if (failure === 'expired') setExpired(true);
          // The role was taken away (or never was there) since the page opened.
          if (failure === 'notApproved') setPhase({ kind: 'pending' });
        }
      });
    }
    setRows((current) => [...current, ...added]);
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    addFiles(Array.from(event.currentTarget.files ?? []));
    event.currentTarget.value = ''; // the same file can be picked again
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    addFiles(Array.from(event.dataTransfer.files ?? []));
  }

  function onDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(true);
  }

  // Pick up a role an admin has just granted: a forced token refresh, as the
  // app's pending screen does, then look again.
  function recheckApproval() {
    if (!ticket || rechecking) return;
    setRechecking(true);
    keycloak
      .updateToken(-1)
      .then(() => enterRelay(ticket))
      .then(setPhase)
      .catch(() => { /* transient: stay on the pending state */ })
      .finally(() => setRechecking(false));
  }

  // Back to this page, so another account can sign in under the same ticket.
  function logout() {
    keycloak.logout({ redirectUri: window.location.origin + UPLOAD_RELAY_PATH });
  }

  const username = (keycloak.tokenParsed?.preferred_username as string | undefined) || '';
  const closeButton = (
    <button type="button" className="btn btn-secondary" onClick={() => window.close()}>
      {t('uploadRelay.close')}
    </button>
  );

  if (phase.kind === 'signingIn') {
    return (
      <main className="upload-relay">
        <div className="loading-message" role="status" aria-live="polite">
          {t('auth.signingIn')}
        </div>
      </main>
    );
  }

  if (phase.kind === 'pending') {
    return (
      <main className="upload-relay">
        <section className="upload-relay-card">
          <h1 className="upload-relay-title">{t('uploadRelay.title')}</h1>
          <div className="upload-relay-pending" role="status" aria-live="polite">
            <strong>{t('pending.title')}</strong>
            <span>{t('uploadRelay.pendingMessage')}</span>
            {username && <span className="upload-relay-account">{t('pending.account', { username })}</span>}
          </div>
          <div className="upload-relay-actions">
            <button type="button" className="btn btn-primary" onClick={recheckApproval} disabled={rechecking}>
              {t('pending.recheck')}
            </button>
            <button type="button" className="btn btn-secondary" onClick={logout}>
              {t('common.logout')}
            </button>
            {closeButton}
          </div>
        </section>
      </main>
    );
  }

  if (phase.kind !== 'ready') {
    const message =
      phase.kind === 'noTicket'
        ? t('uploadRelay.noTicket')
        : phase.kind === 'authError'
          ? t('auth.failed')
          : t(`uploadRelay.problem.${phase.problem}`);
    return (
      <main className="upload-relay">
        <section className="upload-relay-card">
          <h1 className="upload-relay-title">{t('uploadRelay.title')}</h1>
          <p className="error-banner" role="alert">{message}</p>
          <p className="upload-relay-hint">{t('uploadRelay.reopenHint')}</p>
          <div className="upload-relay-actions">{closeButton}</div>
        </section>
      </main>
    );
  }

  const { info } = phase;
  const recipient = info.name || info.user;
  return (
    <main className="upload-relay">
      <section className="upload-relay-card">
        <h1 className="upload-relay-title">{t('uploadRelay.title')}</h1>

        <div className="upload-relay-recipient">
          <strong>{t('uploadRelay.recipient', { recipient })}</strong>
          {info.name && info.name !== info.user && (
            <span className="upload-relay-account">{t('uploadRelay.recipientAccount', { user: info.user })}</span>
          )}
          <span>{t('uploadRelay.recipientHint')}</span>
        </div>

        {expired ? (
          <p className="error-banner" role="alert">{t('uploadRelay.problem.expired')}</p>
        ) : (
          <div
            className={`upload-relay-drop${dragging ? ' upload-relay-drop--over' : ''}`}
            onDragOver={onDragOver}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <p>{t('uploadRelay.dropHint')}</p>
            <label className="btn btn-primary upload-relay-pick">
              {t('uploadRelay.choose')}
              <input
                className="visually-hidden"
                type="file"
                multiple
                accept={info.accept.join(',')}
                onChange={onPick}
              />
            </label>
            <p className="upload-relay-limits">
              {t('uploadRelay.limits', {
                types: info.accept.join(' '),
                size: formatBytes(info.maxBytes),
                count: info.maxFiles,
              })}
            </p>
          </div>
        )}

        {rows.length > 0 && (
          <ul className="upload-relay-files" aria-live="polite" aria-label={t('uploadRelay.filesLabel')}>
            {rows.map((row) => (
              <li key={row.key} className={`upload-relay-file upload-relay-file--${row.state}`}>
                <span className="upload-relay-file-name">{row.name}</span>
                <span className="upload-relay-file-size">{formatBytes(row.size)}</span>
                <span className="upload-relay-file-status">
                  {row.state === 'refused'
                    ? t('uploadRelay.status.refused', { reason: t(`uploadRelay.problem.${row.problem ?? 'unknown'}`) })
                    : t(`uploadRelay.status.${row.state}`)}
                </span>
              </li>
            ))}
          </ul>
        )}

        <p className="upload-relay-hint">{t('uploadRelay.auditNote')}</p>
        <div className="upload-relay-actions">
          {username && <span className="upload-relay-account">{t('pending.account', { username })}</span>}
          {closeButton}
        </div>
      </section>
    </main>
  );
}
