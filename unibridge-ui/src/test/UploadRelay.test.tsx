import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UploadRelay from '../pages/UploadRelay';
import { TICKET_STORAGE_KEY, reloadPage } from '../utils/uploadRelay';

const keycloakMock = vi.hoisted(() => ({
  init: vi.fn(),
  updateToken: vi.fn(),
  logout: vi.fn(),
  token: 'kc-token',
  tokenParsed: {} as Record<string, unknown>,
  onTokenExpired: undefined as (() => void) | undefined,
}));

vi.mock('../keycloak', () => ({ default: keycloakMock }));
// jsdom cannot navigate: the page's "start over" is observed, not performed.
vi.mock('../utils/uploadRelay', async (original) => ({
  ...(await original<typeof import('../utils/uploadRelay')>()),
  reloadPage: vi.fn(),
}));

const TICKET = 'v1.payload.signature';
const INFO = {
  user: 'jinyoung',
  name: 'Jin',
  expiresAt: new Date(Date.now() + 600_000).toISOString(),
  expiresInSeconds: 600,
  accept: ['.pdf', '.png'],
  maxBytes: 1024,
  maxFiles: 5,
  remainingFiles: 5,
};

type Reply = { status: number; body?: unknown } | Error;

/** fetch stub: answers each call from `replies[path]` in order (the last one repeats). */
function stubFetch(replies: Record<string, Reply[]>) {
  const calls: { path: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = String(input);
    calls.push({ path, init });
    const queue = replies[path] ?? [];
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    if (!reply) throw new Error(`unexpected fetch ${path}`);
    if (reply instanceof Error) throw reply;
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

function open(hash: string) {
  window.history.replaceState(null, '', `/upload-relay${hash}`);
}

function file(name: string, size = 10) {
  return new File([new Uint8Array(size)], name);
}

async function ready(replies: Record<string, Reply[]> = {}) {
  const calls = stubFetch({ '/_api/upload-relay/ticket': [{ status: 200, body: INFO }], ...replies });
  open(`#t=${TICKET}`);
  render(<UploadRelay />);
  await screen.findByText("These files go to Jin's Noah conversation");
  return calls;
}

function picker(): HTMLInputElement {
  return screen.getByLabelText('Choose files') as HTMLInputElement;
}

describe('UploadRelay', () => {
  let close: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    window.sessionStorage.clear();
    keycloakMock.init.mockReset().mockResolvedValue(true);
    keycloakMock.updateToken.mockReset().mockResolvedValue(true);
    keycloakMock.logout.mockReset();
    keycloakMock.token = 'kc-token';
    // An approved account: it holds an app role, as the app itself requires.
    keycloakMock.tokenParsed = { preferred_username: 'operator', roles: ['user'] };
    keycloakMock.onTokenExpired = undefined;
    vi.mocked(reloadPage).mockClear();
    // jsdom's close() tears the window down; every test only observes it.
    close = vi.spyOn(window, 'close').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    close.mockRestore();
  });

  it('moves the ticket out of the address bar before Keycloak starts', async () => {
    const seen: { hash: string; stored: string | null }[] = [];
    keycloakMock.init.mockImplementation(async () => {
      seen.push({ hash: window.location.hash, stored: window.sessionStorage.getItem(TICKET_STORAGE_KEY) });
      return true;
    });
    const calls = await ready();

    expect(seen).toEqual([{ hash: '', stored: TICKET }]);
    expect(window.location.pathname).toBe('/upload-relay');
    expect(keycloakMock.init).toHaveBeenCalledWith({
      onLoad: 'login-required', checkLoginIframe: false, pkceMethod: 'S256',
    });
    // The ticket goes to the backend in the body, the Keycloak token in the header.
    expect(calls[0].path).toBe('/_api/upload-relay/ticket');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ ticket: TICKET });
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe('Bearer kc-token');
  });

  it('uses the stored ticket when Keycloak returns with its own fragment', async () => {
    window.sessionStorage.setItem(TICKET_STORAGE_KEY, TICKET);
    const calls = stubFetch({ '/_api/upload-relay/ticket': [{ status: 200, body: INFO }] });
    open('#state=abc&session_state=def&code=ghi');
    keycloakMock.init.mockImplementation(async () => {
      // keycloak-js still needs its callback fragment: this page leaves it alone.
      expect(window.location.hash).toBe('#state=abc&session_state=def&code=ghi');
      return true;
    });
    render(<UploadRelay />);

    await screen.findByText("These files go to Jin's Noah conversation");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ ticket: TICKET });
  });

  it('says who receives the files, who is signed in and what may be uploaded', async () => {
    await ready();

    expect(screen.getByText('Noah account: jinyoung')).toBeInTheDocument();
    expect(screen.getByText(/If that is not your own Noah conversation/)).toBeInTheDocument();
    expect(screen.getByText('Signed in as operator')).toBeInTheDocument();
    expect(screen.getByText('Accepted: .pdf .png · up to 1.0 KB per file · up to 5 at a time')).toBeInTheDocument();
    expect(picker()).toHaveAttribute('accept', '.pdf,.png');
    expect(picker().multiple).toBe(true);
  });

  it('falls back to the Noah username when the ticket has no display name', async () => {
    stubFetch({ '/_api/upload-relay/ticket': [{ status: 200, body: { ...INFO, name: null } }] });
    open(`#t=${TICKET}`);
    render(<UploadRelay />);

    expect(await screen.findByText("These files go to jinyoung's Noah conversation")).toBeInTheDocument();
    expect(screen.queryByText('Noah account: jinyoung')).not.toBeInTheDocument();
  });

  it('uploads picked files one at a time and reports each', async () => {
    const calls = await ready({
      '/_api/upload-relay/files': [
        { status: 201, body: { id: 'f1', name: 'a.pdf', size: 10, sha256: 'x' } },
        { status: 413, body: { detail: 'The file is larger than this upload allows' } },
      ],
    });

    await userEvent.upload(picker(), [file('a.pdf'), file('b.png')]);

    const rows = within(screen.getByRole('list', { name: 'Uploaded files' }));
    await waitFor(() => expect(rows.getByText('Sent · check the Noah message box')).toBeInTheDocument());
    expect(await rows.findByText('Not uploaded: The file is too large.')).toBeInTheDocument();
    expect(rows.getByText('a.pdf')).toBeInTheDocument();
    expect(rows.getByText('b.png')).toBeInTheDocument();

    const uploads = calls.filter((call) => call.path === '/_api/upload-relay/files');
    expect(uploads).toHaveLength(2);
    // The ticket goes in a header (the backend verifies it before reading the
    // body), the file alone in the form.
    const form = uploads[0].init.body as FormData;
    expect(form.get('ticket')).toBeNull();
    expect((form.get('file') as File).name).toBe('a.pdf');
    const sent = uploads[0].init.headers as Record<string, string>;
    expect(sent['X-Upload-Relay-Ticket']).toBe(TICKET);
    expect(sent.Authorization).toBe('Bearer kc-token');
    // A fresh Keycloak token is asked for before every request.
    expect(keycloakMock.updateToken).toHaveBeenCalledTimes(3);
  });

  it('refuses a wrong type, an empty file and an oversized one without sending them', async () => {
    const calls = await ready();

    fireEvent.change(picker(), { target: { files: [file('notes.txt'), file('empty.pdf', 0), file('big.pdf', 2048)] } });

    const rows = within(screen.getByRole('list', { name: 'Uploaded files' }));
    expect(rows.getByText('Not uploaded: This type of file cannot be uploaded here.')).toBeInTheDocument();
    expect(rows.getByText('Not uploaded: The file is empty.')).toBeInTheDocument();
    expect(rows.getByText('Not uploaded: The file is too large.')).toBeInTheDocument();
    expect(rows.getByText('2.0 KB')).toBeInTheDocument();
    expect(calls.filter((call) => call.path === '/_api/upload-relay/files')).toHaveLength(0);
  });

  it.each([
    [400, 'The file name is not usable', 'The file name has characters that cannot be used, or it is too long.'],
    [400, 'Something new', 'The file could not be uploaded. Please try again.'],
    [409, 'full', 'That is more files than one upload takes at a time. Try again once Noah has collected the earlier ones.'],
    [429, 'quota', 'Too many files are already waiting for this Noah user. Try again once Noah has collected the earlier ones.'],
    [503, 'busy', 'Files cannot be received right now. Try again in a moment.'],
    [500, 'boom', 'The file could not be uploaded. Please try again.'],
  ])('explains a %i refusal (%s)', async (status, detail, message) => {
    await ready({ '/_api/upload-relay/files': [{ status, body: { detail } }] });

    await userEvent.upload(picker(), file('a.pdf'));

    expect(await screen.findByText(`Not uploaded: ${message}`)).toBeInTheDocument();
  });

  it('accepts dropped files and marks the drop target while dragging', async () => {
    await ready({ '/_api/upload-relay/files': [{ status: 201, body: { id: 'f1', name: 'a.pdf', size: 10, sha256: 'x' } }] });
    const drop = screen.getByText('Drop files here, or choose them with the button below.').parentElement!;

    fireEvent.dragOver(drop);
    expect(drop.className).toContain('upload-relay-drop--over');
    fireEvent.dragLeave(drop);
    expect(drop.className).not.toContain('upload-relay-drop--over');
    fireEvent.dragOver(drop);
    fireEvent.drop(drop, { dataTransfer: { files: [file('a.pdf')] } });

    expect(drop.className).not.toContain('upload-relay-drop--over');
    expect(await screen.findByText('Sending to Noah...')).toBeInTheDocument();
  });

  it('stops taking files once the upload has run out of time', async () => {
    await ready({ '/_api/upload-relay/files': [{ status: 410, body: { detail: 'This upload ticket has expired' } }] });

    await userEvent.upload(picker(), file('a.pdf'));

    const expired = 'The time for this upload has passed. Press the attach button in Noah again.';
    expect(await screen.findByRole('alert')).toHaveTextContent(expired);
    expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
    expect(screen.getByText(`Not uploaded: ${expired}`)).toBeInTheDocument();
  });

  it('counts the ticket down from the server\'s seconds, not from this PC\'s clock', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // A PC clock an hour fast: by its reading the ticket's absolute expiry is
    // long past, yet the server says five seconds are left.
    stubFetch({
      '/_api/upload-relay/ticket': [
        { status: 200, body: { ...INFO, expiresAt: new Date(Date.now() - 3_600_000).toISOString(), expiresInSeconds: 5 } },
      ],
    });
    open(`#t=${TICKET}`);
    render(<UploadRelay />);
    await screen.findByLabelText('Choose files');

    act(() => { vi.advanceTimersByTime(4_000); });
    expect(screen.getByLabelText('Choose files')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(1_100); });
    expect(screen.getByRole('alert')).toHaveTextContent('The time for this upload has passed.');
    expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
  });

  it('does not guess an expiry when the server sends no countdown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { expiresInSeconds: _unused, ...withoutCountdown } = INFO;
    void _unused;
    stubFetch({
      '/_api/upload-relay/ticket': [
        { status: 200, body: { ...withoutCountdown, expiresAt: new Date(Date.now() - 3_600_000).toISOString() } },
      ],
    });
    open(`#t=${TICKET}`);
    render(<UploadRelay />);
    await screen.findByLabelText('Choose files');

    act(() => { vi.advanceTimersByTime(60_000); });
    expect(screen.getByLabelText('Choose files')).toBeInTheDocument();
  });

  it('takes a new ticket when Noah reuses the popup, instead of uploading under the old one', async () => {
    await ready();
    expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe(TICKET);

    // A second press in Noah navigates this same window to the same page with
    // another fragment: no reload, only a hashchange.
    window.history.replaceState(null, '', '/upload-relay#t=v1.second.ticket');
    fireEvent(window, new HashChangeEvent('hashchange'));

    expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe('v1.second.ticket');
    expect(window.location.hash).toBe('');
    expect(reloadPage).toHaveBeenCalledTimes(1);
  });

  it('ignores a fragment change that carries no ticket', async () => {
    await ready();

    window.history.replaceState(null, '', '/upload-relay#somewhere');
    fireEvent(window, new HashChangeEvent('hashchange'));

    expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe(TICKET);
    expect(window.location.hash).toBe('#somewhere');
    expect(reloadPage).not.toHaveBeenCalled();
  });

  it('stops listening for a new ticket once the page is gone', async () => {
    await ready();
    cleanup();

    window.history.replaceState(null, '', '/upload-relay#t=v1.later.ticket');
    fireEvent(window, new HashChangeEvent('hashchange'));

    expect(reloadPage).not.toHaveBeenCalled();
  });

  describe('an account that is not approved yet', () => {
    const pendingNote =
      'A UniBridge administrator has to approve this account before it can send files to Noah. Once approved, press "Check again".';

    async function openAsPending(replies: Record<string, Reply[]> = {}) {
      keycloakMock.tokenParsed = { preferred_username: 'newcomer', realm_access: { roles: ['offline_access'] } };
      const calls = stubFetch(replies);
      open(`#t=${TICKET}`);
      render(<UploadRelay />);
      await screen.findByText('Awaiting approval');
      return calls;
    }

    it('shows the pending state instead of the file picker, and asks the backend nothing', async () => {
      const calls = await openAsPending();

      const status = screen.getByRole('status');
      expect(status).toHaveTextContent(pendingNote);
      expect(status).toHaveTextContent('Signed in as newcomer');
      expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
      expect(screen.queryByText(/Noah conversation/)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Close window' })).toBeInTheDocument();
      // No ticket is sent anywhere on behalf of an unapproved account.
      expect(calls).toHaveLength(0);
    });

    it('lets the user in once an admin has approved the account', async () => {
      const calls = await openAsPending({ '/_api/upload-relay/ticket': [{ status: 200, body: INFO }] });
      keycloakMock.updateToken.mockImplementation(async () => {
        // The refreshed token carries the role the admin just granted.
        keycloakMock.tokenParsed = { preferred_username: 'newcomer', roles: ['user'] };
        return true;
      });

      await userEvent.click(screen.getByRole('button', { name: 'Check again' }));

      expect(await screen.findByText("These files go to Jin's Noah conversation")).toBeInTheDocument();
      expect(screen.getByLabelText('Choose files')).toBeInTheDocument();
      // A forced refresh (-1), as the app's own pending screen does.
      expect(keycloakMock.updateToken).toHaveBeenCalledWith(-1);
      expect(calls.map((call) => call.path)).toEqual(['/_api/upload-relay/ticket']);
    });

    it('stays pending while the account is still unapproved or the refresh fails', async () => {
      const calls = await openAsPending();

      await userEvent.click(screen.getByRole('button', { name: 'Check again' }));
      await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled());
      expect(screen.getByText('Awaiting approval')).toBeInTheDocument();

      keycloakMock.updateToken.mockRejectedValueOnce(new Error('keycloak is away'));
      await userEvent.click(screen.getByRole('button', { name: 'Check again' }));
      await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled());
      expect(screen.getByText('Awaiting approval')).toBeInTheDocument();
      expect(calls).toHaveLength(0);
    });

    it('signs out back to this page, so another account can use the same ticket', async () => {
      await openAsPending();

      await userEvent.click(screen.getByRole('button', { name: 'Logout' }));

      expect(keycloakMock.logout).toHaveBeenCalledWith({ redirectUri: `${window.location.origin}/upload-relay` });
      // The ticket is still there for the next sign-in.
      expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe(TICKET);
    });

    it('treats a 401 from the backend as pending approval, not as a generic error', async () => {
      // The token looked approved to this page; the backend says otherwise.
      stubFetch({ '/_api/upload-relay/ticket': [{ status: 401, body: { detail: 'Token missing username or valid role' } }] });
      open(`#t=${TICKET}`);
      render(<UploadRelay />);

      expect(await screen.findByText('Awaiting approval')).toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent(pendingNote);
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
    });

    it('falls back to the pending state when an upload is answered 401', async () => {
      await ready({ '/_api/upload-relay/files': [{ status: 401, body: { detail: 'Token missing username or valid role' } }] });

      await userEvent.upload(picker(), file('a.pdf'));

      expect(await screen.findByText('Awaiting approval')).toBeInTheDocument();
      expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
    });
  });

  it('reports a lost sign-in instead of sending the file', async () => {
    const calls = await ready();
    keycloakMock.updateToken.mockRejectedValue(new Error('session ended'));

    await userEvent.upload(picker(), file('a.pdf'));

    expect(await screen.findByText(/Not uploaded: Your sign-in has expired/)).toBeInTheDocument();
    expect(calls.filter((call) => call.path === '/_api/upload-relay/files')).toHaveLength(0);
  });

  it('reports a network failure', async () => {
    await ready({ '/_api/upload-relay/files': [new TypeError('Failed to fetch')] });

    await userEvent.upload(picker(), file('a.pdf'));

    expect(await screen.findByText(/Not uploaded: Could not reach the server/)).toBeInTheDocument();
  });

  it.each([
    [400, 'Invalid upload ticket', 'The upload details are not valid. Press the attach button in Noah again.'],
    [410, 'This upload ticket has expired', 'The time for this upload has passed. Press the attach button in Noah again.'],
    [503, 'The upload relay is not configured', 'Files cannot be received right now. Try again in a moment.'],
  ])('shows why a ticket is not usable (%i)', async (status, detail, message) => {
    stubFetch({ '/_api/upload-relay/ticket': [{ status, body: { detail } }] });
    open(`#t=${TICKET}`);
    render(<UploadRelay />);

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.queryByLabelText('Choose files')).not.toBeInTheDocument();
    expect(screen.getByText('Press the attach button in a Noah conversation to open this page again.')).toBeInTheDocument();
  });

  it('judges an error page from a proxy by its status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>Bad Gateway</html>', { status: 502 })));
    open(`#t=${TICKET}`);
    render(<UploadRelay />);

    expect(await screen.findByRole('alert')).toHaveTextContent('The file could not be uploaded. Please try again.');
  });

  it('asks to be opened from Noah when there is no ticket, without signing in', () => {
    stubFetch({});
    open('');
    render(<UploadRelay />);

    expect(screen.getByRole('alert')).toHaveTextContent('This page has to be opened from Noah.');
    expect(keycloakMock.init).not.toHaveBeenCalled();
  });

  it('shows progress while signing in and a failure when Keycloak is unreachable', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    stubFetch({});
    let fail!: (error: Error) => void;
    keycloakMock.init.mockReturnValue(new Promise((_resolve, reject) => { fail = reject; }));
    open(`#t=${TICKET}`);
    render(<UploadRelay />);

    expect(screen.getByRole('status')).toHaveTextContent('Signing you in...');
    await act(async () => { fail(new Error('offline')); });

    expect(await screen.findByRole('alert')).toHaveTextContent('Authentication failed. Please try again.');
    consoleError.mockRestore();
  });

  it('treats an unauthenticated return from Keycloak as a failure', async () => {
    stubFetch({});
    keycloakMock.init.mockResolvedValue(false);
    open(`#t=${TICKET}`);
    render(<UploadRelay />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Authentication failed. Please try again.');
  });

  it('refreshes the Keycloak token when it expires and never touches the opener', async () => {
    const opener = { postMessage: vi.fn(), location: { href: 'https://noah.example' } };
    vi.stubGlobal('opener', opener);
    await ready();

    keycloakMock.updateToken.mockClear();
    keycloakMock.onTokenExpired?.();
    expect(keycloakMock.updateToken).toHaveBeenCalledWith(30);
    keycloakMock.updateToken.mockRejectedValueOnce(new Error('gone'));
    expect(() => keycloakMock.onTokenExpired?.()).not.toThrow();

    expect(opener.postMessage).not.toHaveBeenCalled();
    expect(opener.location.href).toBe('https://noah.example');
  });

  it('closes the window from the close button', async () => {
    await ready();

    await userEvent.click(screen.getByRole('button', { name: 'Close window' }));

    expect(close).toHaveBeenCalledTimes(1);
  });

  describe('closing by itself once Noah has the files', () => {
    const STORED = { status: 201, body: { id: 'f1', name: 'a.pdf', size: 10, sha256: 'x' } };
    const holding = (files: number): Reply => ({ status: 200, body: { ...INFO, remainingFiles: INFO.maxFiles - files } });
    const waiting = 'Sending to the Noah server...';
    const autoClose = 'This window closes by itself when that is done.';
    const ticketCalls = (calls: { path: string }[]) => calls.filter((call) => call.path === '/_api/upload-relay/ticket').length;

    /** The page with fake timers on, so the once-a-second look can be stepped. */
    async function readyWithClock(replies: Record<string, Reply[]>) {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      return ready(replies);
    }

    async function tick(times = 1) {
      for (let i = 0; i < times; i += 1) {
        await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      }
    }

    it('waits while the ticket still holds a file, and closes when it holds none', async () => {
      const calls = await readyWithClock({
        // Opening the page, then: both files there, one collected, both collected.
        '/_api/upload-relay/ticket': [holding(0), holding(2), holding(1), holding(0)],
        '/_api/upload-relay/files': [STORED],
      });

      await userEvent.upload(picker(), [file('a.pdf'), file('b.png')]);
      expect(await screen.findByText(waiting)).toBeInTheDocument();
      expect(ticketCalls(calls)).toBe(1);
      // Said once, with the promise to close, and on each file's own row.
      expect(screen.getByRole('status')).toHaveTextContent(autoClose);
      expect(screen.getAllByText('Sending to Noah...')).toHaveLength(2);
      expect(screen.queryByText('Sent · check the Noah message box')).not.toBeInTheDocument();

      await tick(2);
      expect(close).not.toHaveBeenCalled();
      await tick();
      expect(close).toHaveBeenCalledTimes(1);
      expect(ticketCalls(calls)).toBe(4);

      // A browser that kept the window open says the files arrived, and stops asking.
      expect(screen.getByRole('status')).toHaveTextContent('Noah has the files. You can close this window.');
      expect(screen.getByRole('status')).not.toHaveTextContent(autoClose);
      expect(screen.queryByText(waiting)).not.toBeInTheDocument();
      expect(screen.getAllByText('Sent · check the Noah message box')).toHaveLength(2);
      await tick(3);
      expect(ticketCalls(calls)).toBe(4);
    });

    it('does not ask before the last upload of the batch has finished', async () => {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => { release = resolve; });
      const calls = await readyWithClock({ '/_api/upload-relay/ticket': [holding(0)] });
      const fetchMock = vi.mocked(globalThis.fetch);
      const answer = fetchMock.getMockImplementation()!;
      let uploads = 0;
      fetchMock.mockImplementation(async (input, init) => {
        if (String(input) !== '/_api/upload-relay/files') return answer(input, init);
        uploads += 1;
        // The second file is still going up while Noah collects the first.
        if (uploads === 2) await gate;
        return new Response(JSON.stringify(STORED.body), { status: 201 });
      });

      await userEvent.upload(picker(), [file('a.pdf'), file('b.png')]);
      await screen.findByText('Uploading...');
      // Still uploading: the page says so, and the finished file is not called sent yet.
      expect(screen.getByRole('status')).toHaveTextContent('Uploading your files...');
      expect(screen.getByRole('status')).toHaveTextContent(autoClose);
      expect(await screen.findByText('Sending to Noah...')).toBeInTheDocument();
      await tick(3);
      expect(ticketCalls(calls)).toBe(1);
      expect(close).not.toHaveBeenCalled();

      release();
      await screen.findByText(waiting);
      await tick();
      expect(close).toHaveBeenCalledTimes(1);
    });

    it('stays open when a file of the batch was turned away', async () => {
      const calls = await readyWithClock({
        '/_api/upload-relay/files': [STORED, { status: 413, body: { detail: 'The file is larger than this upload allows' } }],
      });

      await userEvent.upload(picker(), [file('a.pdf'), file('b.png')]);
      await screen.findByText('Not uploaded: The file is too large.');
      await tick(3);

      expect(screen.queryByText(waiting)).not.toBeInTheDocument();
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
      expect(screen.queryByText(autoClose)).not.toBeInTheDocument();
      expect(screen.getByText('Sent · check the Noah message box')).toBeInTheDocument();
      expect(ticketCalls(calls)).toBe(1);
      expect(close).not.toHaveBeenCalled();
    });

    it('judges a later batch on its own, whatever an earlier one came to', async () => {
      await readyWithClock({ '/_api/upload-relay/files': [STORED] });

      fireEvent.change(picker(), { target: { files: [file('notes.txt')] } });
      await screen.findByText('Not uploaded: This type of file cannot be uploaded here.');
      await tick(2);
      expect(close).not.toHaveBeenCalled();

      fireEvent.change(picker(), { target: { files: [file('a.pdf')] } });
      await screen.findByText(waiting);
      await tick();
      expect(close).toHaveBeenCalledTimes(1);
    });

    it('gives up quietly when Noah does not come for the files', async () => {
      const calls = await readyWithClock({
        '/_api/upload-relay/ticket': [holding(0), holding(1)],
        '/_api/upload-relay/files': [STORED],
      });

      await userEvent.upload(picker(), file('a.pdf'));
      await screen.findByText(waiting);
      await tick(40);

      expect(close).not.toHaveBeenCalled();
      expect(ticketCalls(calls)).toBe(31);
      expect(screen.queryByText(waiting)).not.toBeInTheDocument();
      expect(screen.getByText('Sent · check the Noah message box')).toBeInTheDocument();
    });

    it('keeps asking through a network blip', async () => {
      await readyWithClock({
        '/_api/upload-relay/ticket': [holding(0), new TypeError('Failed to fetch'), holding(0)],
        '/_api/upload-relay/files': [STORED],
      });

      await userEvent.upload(picker(), file('a.pdf'));
      await screen.findByText(waiting);
      await tick();
      expect(close).not.toHaveBeenCalled();
      await tick();
      expect(close).toHaveBeenCalledTimes(1);
    });

    it.each([
      [410, 'This upload ticket has expired'],
      [401, 'Token missing username or valid role'],
    ])('stops asking once the ticket or the sign-in is gone (%i)', async (status, detail) => {
      const calls = await readyWithClock({
        '/_api/upload-relay/ticket': [holding(0), { status, body: { detail } }],
        '/_api/upload-relay/files': [STORED],
      });

      await userEvent.upload(picker(), file('a.pdf'));
      await screen.findByText(waiting);
      await tick(4);

      expect(ticketCalls(calls)).toBe(2);
      expect(close).not.toHaveBeenCalled();
      expect(screen.queryByText(waiting)).not.toBeInTheDocument();
    });

    it('stops asking when the page goes away', async () => {
      const calls = await readyWithClock({
        '/_api/upload-relay/ticket': [holding(0), holding(1)],
        '/_api/upload-relay/files': [STORED],
      });

      await userEvent.upload(picker(), file('a.pdf'));
      await screen.findByText(waiting);
      await tick();
      cleanup();
      await tick(3);

      expect(ticketCalls(calls)).toBe(2);
    });
  });
});
