import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TICKET_STORAGE_KEY,
  captureTicket,
  fileExtension,
  fragmentTicket,
  formatBytes,
  isUploadRelayPath,
  problemFor,
} from '../utils/uploadRelay';

describe('upload relay helpers', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, '', '/upload-relay');
  });

  it('recognises only the relay page path', () => {
    expect(isUploadRelayPath('/upload-relay')).toBe(true);
    expect(isUploadRelayPath('/upload-relay/')).toBe(true);
    expect(isUploadRelayPath('/upload-relay/more')).toBe(false);
    expect(isUploadRelayPath('/upload-relays')).toBe(false);
    expect(isUploadRelayPath('/')).toBe(false);
  });

  it('captures the ticket once and finds it again afterwards', () => {
    window.history.replaceState(null, '', '/upload-relay?lang=ko#t=v1.abc.def');

    expect(captureTicket()).toBe('v1.abc.def');
    expect(window.location.hash).toBe('');
    expect(window.location.search).toBe('?lang=ko');
    expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe('v1.abc.def');
    // A second call (React StrictMode) reads the stored one.
    expect(captureTicket()).toBe('v1.abc.def');
  });

  it('prefers a new ticket in the address bar over a stored one', () => {
    window.sessionStorage.setItem(TICKET_STORAGE_KEY, 'old');
    window.history.replaceState(null, '', '/upload-relay#t=new');

    expect(captureTicket()).toBe('new');
    expect(window.sessionStorage.getItem(TICKET_STORAGE_KEY)).toBe('new');
  });

  it('leaves a fragment that is not ours alone', () => {
    window.history.replaceState(null, '', '/upload-relay#state=a&code=b');

    expect(captureTicket()).toBeNull();
    expect(window.location.hash).toBe('#state=a&code=b');
  });

  it('still returns the ticket when session storage is refused', () => {
    window.history.replaceState(null, '', '/upload-relay#t=v1.abc.def');
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });

    expect(captureTicket()).toBe('v1.abc.def');
    expect(window.location.hash).toBe('');
    expect(captureTicket()).toBeNull();

    setItem.mockRestore();
    getItem.mockRestore();
  });

  it('reads a ticket from the fragment only when it is there', () => {
    expect(fragmentTicket()).toBeNull();
    window.history.replaceState(null, '', '/upload-relay#t=');
    expect(fragmentTicket()).toBeNull();
    window.history.replaceState(null, '', '/upload-relay#state=a&t=v1.x.y');
    expect(fragmentTicket()).toBe('v1.x.y');
  });

  it('maps backend answers to problems', () => {
    expect(problemFor(400, 'Invalid upload ticket')).toBe('invalidTicket');
    expect(problemFor(400, 'The file is empty')).toBe('empty');
    expect(problemFor(400, 'This type of file is not accepted here')).toBe('type');
    expect(problemFor(400, { nested: true })).toBe('unknown');
    expect(problemFor(401, null)).toBe('notApproved');
    expect(problemFor(409, null)).toBe('tooMany');
    expect(problemFor(410, null)).toBe('expired');
    expect(problemFor(413, null)).toBe('tooLarge');
    expect(problemFor(429, null)).toBe('userQuota');
    expect(problemFor(503, null)).toBe('unavailable');
    expect(problemFor(502, null)).toBe('unknown');
  });

  it('reads extensions and formats sizes', () => {
    expect(fileExtension('Report.PDF')).toBe('.pdf');
    expect(fileExtension('archive.tar.gz')).toBe('.gz');
    expect(fileExtension('noext')).toBe('');
    expect(fileExtension('.hidden')).toBe('');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
  });
});
