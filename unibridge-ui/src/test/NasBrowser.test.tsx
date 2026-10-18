vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getNasEntries: vi.fn(),
  getNasEntryMetadata: vi.fn(),
  downloadNasEntry: vi.fn(),
  downloadNasZip: vi.fn(),
  readBlobErrorDetail: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ alias: 'nas-main' }),
    useNavigate: () => vi.fn(),
  };
});

import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getNasEntries,
  getNasEntryMetadata,
  downloadNasEntry,
  downloadNasZip,
  readBlobErrorDetail,
} from '../api/client';
import i18n from '../i18n';
import NasBrowser from '../pages/NasBrowser';
import { renderWithProviders } from './helpers';

const mockEntries = vi.mocked(getNasEntries);
const mockMeta = vi.mocked(getNasEntryMetadata);
const mockDownload = vi.mocked(downloadNasEntry);
const mockZip = vi.mocked(downloadNasZip);
const mockBlobDetail = vi.mocked(readBlobErrorDetail);
const clipboardWriteText = vi.fn();

/** A NasEntry exactly per contract §6/§10 — name/path/is_dir/size/modified_time, nothing else. */
function makeEntry(overrides: Partial<NasEntryShape> = {}): NasEntryShape {
  return {
    name: 'file.csv',
    path: 'file.csv',
    is_dir: false,
    size: 1234,
    modified_time: '2026-06-01T12:00:00+00:00',
    ...overrides,
  };
}

interface NasEntryShape {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  modified_time: string | null;
}

/** A NasListResponse exactly per contract §6/§10. */
function makeListResponse(overrides: Partial<NasListShape> = {}): NasListShape {
  return {
    path: '',
    folders: [],
    files: [],
    total_count: 0,
    has_more: false,
    next_cursor: null,
    ...overrides,
  };
}

interface NasListShape {
  path: string;
  folders: NasEntryShape[];
  files: NasEntryShape[];
  total_count: number;
  has_more: boolean;
  next_cursor: string | null;
  truncated?: boolean;
}

function escapeRegExp(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function labelRe(...keys: string[]) {
  return new RegExp(`^(${keys.map((k) => escapeRegExp(i18n.t(k))).join('|')})$`, 'i');
}

const NAS_PERMISSIONS = ['nas.connections.read', 'nas.browse'];

/** Stub the objectURL + anchor plumbing a blob save goes through; returns the click spy. */
function stubBlobSaving() {
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:mock') });
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
  return vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
}

/** Object URLs named after what they hold: `blob:svg`, `blob:png`, else `blob:other`. */
function stubObjectUrls() {
  const names: Record<string, string> = { 'image/svg+xml': 'blob:svg', 'image/png': 'blob:png' };
  const createObjectURL = vi.fn((blob: Blob) => names[blob.type] ?? 'blob:other');
  const revokeObjectURL = vi.fn();
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  return { createObjectURL, revokeObjectURL };
}

/**
 * jsdom neither decodes images nor draws on a canvas: stand in for both, for
 * a 64×32 SVG. `decodes: false` fails the decode; `exportable: false` makes
 * the canvas refuse to export, as a tainted one does.
 */
function stubSvgDrawing({ decodes = true, exportable = true } = {}) {
  const setSrc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src')!.set!;
  vi.spyOn(HTMLImageElement.prototype, 'src', 'set').mockImplementation(function (
    this: HTMLImageElement,
    value: string,
  ) {
    setSrc.call(this, value);
    setTimeout(() => this.dispatchEvent(new Event(decodes ? 'load' : 'error')), 0);
  });
  vi.spyOn(HTMLImageElement.prototype, 'naturalWidth', 'get').mockReturnValue(64);
  vi.spyOn(HTMLImageElement.prototype, 'naturalHeight', 'get').mockReturnValue(32);
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    { drawImage } as unknown as CanvasRenderingContext2D,
  );
  const toBlob = vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
    if (!exportable) throw new DOMException('The canvas has been tainted', 'SecurityError');
    callback(new Blob(['png'], { type: 'image/png' }));
  });
  return { drawImage, toBlob };
}

/** Record every src/srcset that shows up in the document; the returned function stops and reports. */
function recordImageSources(): () => string[] {
  const seen: string[] = [];
  const record = (node: Node) => {
    if (!(node instanceof Element)) return;
    for (const el of [node, ...node.querySelectorAll('[src], [srcset]')]) {
      for (const attr of ['src', 'srcset']) {
        const value = el.getAttribute(attr);
        if (value) seen.push(value);
      }
    }
  };
  const recordAll = (mutations: MutationRecord[]) =>
    mutations.forEach((m) => {
      record(m.target);
      m.addedNodes.forEach(record);
    });
  const observer = new MutationObserver(recordAll);
  observer.observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['src', 'srcset'],
  });
  return () => {
    recordAll(observer.takeRecords());
    observer.disconnect();
    return seen;
  };
}

function selectBox(name: string) {
  return screen.getByRole('checkbox', { name: i18n.t('nas.selectFile', { name }) });
}

describe('NasBrowser page', () => {
  beforeEach(() => {
    mockEntries.mockReset();
    mockMeta.mockReset();
    mockDownload.mockReset();
    mockZip.mockReset();
    mockBlobDetail.mockReset();
    mockBlobDetail.mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockReset();
    clipboardWriteText.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists entries at the base_path root (no bucket selector)', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
        files: [makeEntry({ name: 'README.md', path: 'README.md', size: 2048 })],
        total_count: 2,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });

    await waitFor(() => expect(screen.getByText('logs')).toBeInTheDocument());
    expect(screen.getByText('README.md')).toBeInTheDocument();

    // Browse must start at root with an empty relative path (contract §7/§10).
    expect(mockEntries).toHaveBeenCalledWith('nas-main', expect.objectContaining({ path: '' }));

    // There is no filesystem analog to S3 buckets: NO bucket selector.
    expect(screen.queryByText(i18n.t('s3.selectBucket'))).not.toBeInTheDocument();
    expect(document.querySelector('select')).toBeNull();
  });

  it('descends into a folder using entry.path', async () => {
    mockEntries
      .mockResolvedValueOnce(
        makeListResponse({
          folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
          total_count: 1,
        }),
      )
      .mockResolvedValueOnce(
        makeListResponse({
          path: 'logs',
          files: [makeEntry({ name: '2026.txt', path: 'logs/2026.txt', size: 10 })],
          total_count: 1,
        }),
      );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });

    await waitFor(() => expect(screen.getByText('logs')).toBeInTheDocument());
    fireEvent.click(screen.getByText('logs'));

    await waitFor(() => expect(screen.getByText('2026.txt')).toBeInTheDocument());
    expect(screen.getByRole('navigation', { name: 'NAS path' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'logs' })).toHaveAttribute('aria-current', 'page');
    // The descent must use the folder entry's own `path`, not a reconstructed key.
    expect(mockEntries).toHaveBeenLastCalledWith(
      'nas-main',
      expect.objectContaining({ path: 'logs' }),
    );
  });

  it('descends into a folder from the keyboard row target', async () => {
    mockEntries
      .mockResolvedValueOnce(
        makeListResponse({
          folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
          total_count: 1,
        }),
      )
      .mockResolvedValueOnce(
        makeListResponse({
          path: 'logs',
          files: [makeEntry({ name: '2026.txt', path: 'logs/2026.txt', size: 10 })],
          total_count: 1,
        }),
      );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });

    const row = await screen.findByRole('button', { name: `${i18n.t('nas.folder')}: logs` });
    fireEvent.keyDown(row, { key: 'Enter' });

    await waitFor(() => expect(screen.getByText('2026.txt')).toBeInTheDocument());
    expect(mockEntries).toHaveBeenLastCalledWith(
      'nas-main',
      expect.objectContaining({ path: 'logs' }),
    );
  });

  it('shows the empty state when a directory has no entries', async () => {
    mockEntries.mockResolvedValue(makeListResponse());
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() =>
      expect(screen.getByText(i18n.t('nas.noEntries'))).toBeInTheDocument(),
    );
  });

  it('shows API request examples for the current NAS alias and file path', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 1,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: labelRe('nas.apiExamples') }));
    const dialog = await screen.findByRole('dialog');

    expect(dialog).toHaveTextContent('/api/nas/nas-main/entries?path=&limit=100');
    expect(dialog).toHaveTextContent('/api/nas/nas-main/entries?path=&q=report&limit=100');
    expect(dialog).toHaveTextContent('/api/nas/nas-main/metadata?path=file.txt');
    expect(dialog).toHaveTextContent('/api/nas/nas-main/download?path=file.txt');
    expect(dialog).toHaveTextContent("-H 'apikey: <YOUR_API_KEY>'");

    fireEvent.click(within(dialog).getByRole('button', { name: labelRe('nas.apiExamplesCopyLabel') }));
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining('/api/nas/nas-main/download?path=file.txt'));
    });
    expect(within(dialog).getByRole('status')).toHaveTextContent(i18n.t('nas.apiExamplesCopied'));
  });

  it('shows an error banner when the listing fails', async () => {
    mockEntries.mockRejectedValue(new Error('mount gone'));
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    // The failure surfaces in both the error banner and a toast — match either/both.
    await waitFor(() =>
      expect(screen.getAllByText(i18n.t('nas.loadFailed')).length).toBeGreaterThan(0),
    );
  });

  it('renders a Download control and downloads via the blob helper', async () => {
    const blob = new Blob(['hi']);
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob, filename: 'file.txt' });

    const createObjectURL = vi.fn(() => 'blob:mock');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    const downloadBtn = screen.getByRole('button', { name: 'Download file.txt' });
    expect(downloadBtn).toBeInTheDocument();
    fireEvent.click(downloadBtn);

    await waitFor(() => expect(mockDownload).toHaveBeenCalled());
    // Download must address the entry by its alias-relative path (contract §10: downloadNasEntry(alias, path)).
    expect(mockDownload).toHaveBeenCalledWith('nas-main', 'file.txt');
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('surfaces a download failure as a toast', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 1,
      }),
    );
    mockDownload.mockRejectedValue(new Error('nope'));
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Download file.txt' }));
    await waitFor(() => expect(mockDownload).toHaveBeenCalled());
  });

  it('paginates with Load More only when has_more is set', async () => {
    mockEntries
      .mockResolvedValueOnce(
        makeListResponse({
          files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 1 })],
          total_count: 1,
          has_more: true,
          next_cursor: '500',
        }),
      )
      .mockResolvedValueOnce(
        makeListResponse({
          files: [makeEntry({ name: 'b.txt', path: 'b.txt', size: 1 })],
          total_count: 1,
          has_more: false,
          next_cursor: null,
        }),
      );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });

    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());
    const loadMore = screen.getByRole('button', { name: labelRe('nas.loadMore', 's3.loadMore', 'common.loading') });
    fireEvent.click(loadMore);
    await waitFor(() => expect(screen.getByText('b.txt')).toBeInTheDocument());
    // The follow-up call must carry the cursor offset from next_cursor.
    expect(mockEntries).toHaveBeenLastCalledWith(
      'nas-main',
      expect.objectContaining({ offset: 500 }),
    );
  });

  it('opens the metadata modal on metadata click', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 1,
      }),
    );
    mockMeta.mockResolvedValue({
      name: 'file.txt',
      path: 'file.txt',
      is_dir: false,
      size: 5,
      modified_time: '2026-06-01T12:00:00+00:00',
      content_type: 'text/plain',
    });
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Show metadata for file.txt' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText('text/plain')).toBeInTheDocument();
    // Browse responses never leak absolute paths (contract §6).
    expect(screen.queryByText('/mnt/share')).not.toBeInTheDocument();
  });

  it('exposes NO write controls anywhere — strictly read-only', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 2,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    // No mutating affordances of any kind.
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete|삭제/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rename|이름 변경|이름변경/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /new folder|create folder|폴더 만들기|폴더 생성/i })).not.toBeInTheDocument();
    // No file input that would back an upload control.
    expect(document.querySelector('input[type="file"]')).toBeNull();
    // The only mutating verb in S3-land — presigned URL — must not exist here.
    expect(screen.queryByRole('button', { name: /presigned/i })).not.toBeInTheDocument();
  });

  it('downloads a non-previewable file when its name is clicked', async () => {
    const blob = new Blob(['hi']);
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'data.bin', path: 'data.bin', size: 9 })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob, filename: 'data.bin' });
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:mock') });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('data.bin')).toBeInTheDocument());

    // Clicking the file name itself triggers the download — no separate button needed.
    fireEvent.click(screen.getByText('data.bin'));
    await waitFor(() => expect(mockDownload).toHaveBeenCalledWith('nas-main', 'data.bin'));
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('opens an inline image preview when an image name is clicked', async () => {
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'photo.png', path: 'photo.png', size: 3 })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob, filename: 'photo.png' });
    const createObjectURL = vi.fn(() => 'blob:preview');
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext');

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('photo.png')).toBeInTheDocument());

    fireEvent.click(screen.getByText('photo.png'));

    // The image is fetched and shown inline in a dialog — NOT pushed to disk.
    const img = await screen.findByAltText('photo.png');
    expect(img).toHaveAttribute('src', 'blob:preview');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(clickSpy).not.toHaveBeenCalled();
    // A raster image is shown as downloaded: only an SVG is redrawn.
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(img).not.toHaveAttribute('srcset');
    expect(getContext).not.toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('shows an SVG preview as a PNG drawn from it, never through an SVG blob URL', async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"><script>alert(1)</script></svg>';
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'logo.svg', path: 'logo.svg', size: svg.length })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob: new Blob([svg], { type: 'image/svg+xml' }), filename: 'logo.svg' });
    const { createObjectURL, revokeObjectURL } = stubObjectUrls();
    const { drawImage } = stubSvgDrawing();
    const stopRecording = recordImageSources();

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    fireEvent.click(await screen.findByText('logo.svg'));

    const img = await screen.findByAltText('logo.svg');
    expect(img).toHaveAttribute('src', 'blob:png');
    expect(img).toHaveAttribute('srcset', 'blob:png 1x');
    expect(drawImage).toHaveBeenCalledTimes(1);
    // The SVG itself was only decoded off the page, through a data: URL: the
    // one object URL is the PNG's, and no SVG URL reaches the page.
    expect(createObjectURL.mock.results.map((r) => r.value)).toEqual(['blob:png']);
    expect(document.querySelector('img[src^="data:"]')).toBeNull();
    const sources = stopRecording();
    expect(sources).toContain('blob:png');
    expect(sources.filter((src) => src.includes('blob:svg'))).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: i18n.t('common.close') }));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:png'));
  });

  it('treats a download typed image/svg+xml as an SVG whatever its name', async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"/>';
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'badge.png', path: 'badge.png', size: svg.length })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob: new Blob([svg], { type: 'image/svg+xml' }), filename: 'badge.png' });
    stubObjectUrls();
    const { drawImage } = stubSvgDrawing();

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    fireEvent.click(await screen.findByText('badge.png'));

    expect(await screen.findByAltText('badge.png')).toHaveAttribute('src', 'blob:png');
    expect(drawImage).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['does not decode', { decodes: false }],
    ['taints the canvas', { exportable: false }],
  ])('shows the source of an SVG that %s instead of drawing it', async (_label, options) => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>';
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'odd.svg', path: 'odd.svg', size: svg.length })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob: new Blob([svg], { type: 'image/svg+xml' }), filename: 'odd.svg' });
    const { createObjectURL, revokeObjectURL } = stubObjectUrls();
    stubSvgDrawing(options);
    const stopRecording = recordImageSources();

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    fireEvent.click(await screen.findByText('odd.svg'));

    expect(await screen.findByText(i18n.t('nas.previewSvgSource'))).toBeInTheDocument();
    expect(screen.getByText(svg)).toBeInTheDocument();
    expect(screen.queryByAltText('odd.svg')).not.toBeInTheDocument();
    // No object URL at all: the SVG was decoded off the page through a data: URL.
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();
    expect(stopRecording().filter((src) => src.includes('blob:svg'))).toEqual([]);
  });

  it('creates no URL for an SVG drawing that lands after the page went away', async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"/>';
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'logo.svg', path: 'logo.svg', size: svg.length })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob: new Blob([svg], { type: 'image/svg+xml' }), filename: 'logo.svg' });
    const { createObjectURL } = stubObjectUrls();
    const { toBlob } = stubSvgDrawing();
    let finishExport = () => {};
    toBlob.mockImplementation((callback) => {
      finishExport = () => callback(new Blob(['png'], { type: 'image/png' }));
    });

    const { unmount } = renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    fireEvent.click(await screen.findByText('logo.svg'));
    await waitFor(() => expect(toBlob).toHaveBeenCalled());

    unmount();
    finishExport();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('shows the error state for a .svg file that is not SVG markup', async () => {
    const notSvg = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0xff, 0x00, 0x10]);
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'photo.svg', path: 'photo.svg', size: notSvg.length })],
        total_count: 1,
      }),
    );
    mockDownload.mockResolvedValue({ blob: new Blob([notSvg], { type: 'image/svg+xml' }), filename: 'photo.svg' });
    stubObjectUrls();
    stubSvgDrawing({ decodes: false });

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    fireEvent.click(await screen.findByText('photo.svg'));

    expect(await screen.findByText(i18n.t('nas.previewFailed'))).toBeInTheDocument();
    expect(screen.queryByText(i18n.t('nas.previewSvgSource'))).not.toBeInTheDocument();
  });

  it('filters the listing through the server-side q parameter', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'report.csv', path: 'report.csv', size: 5 })],
        total_count: 1,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('report.csv')).toBeInTheDocument());

    fireEvent.change(screen.getByRole('searchbox', { name: i18n.t('nas.searchPlaceholder') }), {
      target: { value: 'rep' },
    });

    // The debounced search re-queries the backend with the q term.
    await waitFor(() =>
      expect(mockEntries).toHaveBeenLastCalledWith(
        'nas-main',
        expect.objectContaining({ q: 'rep' }),
      ),
    );
  });

  it('shows a distinct empty state when a search yields no matches', async () => {
    mockEntries
      .mockResolvedValueOnce(
        makeListResponse({
          files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 1 })],
          total_count: 1,
        }),
      )
      .mockResolvedValue(makeListResponse());
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.change(screen.getByRole('searchbox', { name: i18n.t('nas.searchPlaceholder') }), {
      target: { value: 'zzz' },
    });

    await waitFor(() =>
      expect(screen.getByText(i18n.t('nas.noResults'))).toBeInTheDocument(),
    );
  });

  it('surfaces a notice when the directory scan is truncated', async () => {
    mockEntries.mockResolvedValue({
      ...makeListResponse({
        files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 1 })],
        total_count: 1,
      }),
      truncated: true,
    });
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());
    expect(screen.getByText(i18n.t('nas.entryCountMore', { count: 1 }))).toBeInTheDocument();
    expect(screen.getByText(i18n.t('nas.truncatedNotice'))).toBeInTheDocument();
  });

  /* ── Multi-select ZIP download ── */

  it('offers a selection checkbox on files but never on folders', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
        files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 10 })],
        total_count: 2,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    expect(selectBox('a.txt')).toBeInTheDocument();
    expect(
      screen.queryByRole('checkbox', { name: i18n.t('nas.selectFile', { name: 'logs' }) }),
    ).not.toBeInTheDocument();
    // Only the header checkbox and the single file's checkbox exist.
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);
  });

  it('downloads the selected files as one ZIP and clears the selection', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [
          makeEntry({ name: 'a.txt', path: 'a.txt', size: 10 }),
          makeEntry({ name: 'b.txt', path: 'docs/b.txt', size: 20 }),
          makeEntry({ name: 'c.txt', path: 'c.txt', size: 30 }),
        ],
        total_count: 3,
      }),
    );
    mockZip.mockResolvedValue({ blob: new Blob(['zip']), filename: 'nas-main-files.zip' });
    const clickSpy = stubBlobSaving();

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(selectBox('a.txt'));
    fireEvent.click(selectBox('b.txt'));

    const bar = screen.getByRole('group', { name: i18n.t('nas.selection') });
    expect(within(bar).getByText(i18n.t('nas.selectedCount', { count: 2 }))).toBeInTheDocument();
    // Summed from the listing entries — 10 B + 20 B; c.txt's own 30 B stays out of the bar.
    expect(within(bar).getByText('30 B')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: i18n.t('nas.downloadZip') }));

    // Exactly the selected paths, addressed relative to the alias — c.txt stays out.
    await waitFor(() => expect(mockZip).toHaveBeenCalledWith('nas-main', ['a.txt', 'docs/b.txt']));
    expect(clickSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByText(i18n.t('nas.selectedCount', { count: 2 }))).not.toBeInTheDocument(),
    );
    clickSpy.mockRestore();
  });

  it('selects and deselects every listed file from the header checkbox', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [
          makeEntry({ name: 'a.txt', path: 'a.txt', size: 10 }),
          makeEntry({ name: 'b.txt', path: 'b.txt', size: 20 }),
        ],
        total_count: 2,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    const selectAll = screen.getByRole('checkbox', { name: i18n.t('nas.selectAllFiles') });

    // Partial selection puts the header checkbox in the indeterminate state.
    fireEvent.click(selectBox('a.txt'));
    expect((selectAll as HTMLInputElement).indeterminate).toBe(true);

    fireEvent.click(selectAll);
    expect(screen.getByText(i18n.t('nas.selectedCount', { count: 2 }))).toBeInTheDocument();
    expect(selectBox('a.txt')).toBeChecked();
    expect(selectBox('b.txt')).toBeChecked();
    expect((selectAll as HTMLInputElement).indeterminate).toBe(false);

    fireEvent.click(selectAll);
    expect(screen.queryByText(i18n.t('nas.selectedCount', { count: 2 }))).not.toBeInTheDocument();
    expect(selectBox('a.txt')).not.toBeChecked();
  });

  it('surfaces the server detail when the ZIP download fails and keeps the selection', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 10 })],
        total_count: 1,
      }),
    );
    mockZip.mockRejectedValue(new Error('boom'));
    mockBlobDetail.mockResolvedValue('File not found: a.txt');

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(selectBox('a.txt'));
    fireEvent.click(screen.getByRole('button', { name: i18n.t('nas.downloadZip') }));

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent(i18n.t('nas.zipDownloadFailed'));
    expect(toast).toHaveTextContent('File not found: a.txt');
    // A failed batch must not throw the selection away — the user retries as-is.
    expect(screen.getByText(i18n.t('nas.selectedCount', { count: 1 }))).toBeInTheDocument();
    expect(selectBox('a.txt')).toBeChecked();
  });

  it('drops the selection when the browser changes directory', async () => {
    mockEntries
      .mockResolvedValueOnce(
        makeListResponse({
          folders: [makeEntry({ name: 'logs', path: 'logs', is_dir: true, size: null })],
          files: [makeEntry({ name: 'a.txt', path: 'a.txt', size: 10 })],
          total_count: 2,
        }),
      )
      .mockResolvedValueOnce(
        makeListResponse({
          path: 'logs',
          files: [makeEntry({ name: '2026.txt', path: 'logs/2026.txt', size: 10 })],
          total_count: 1,
        }),
      );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(selectBox('a.txt'));
    expect(screen.getByText(i18n.t('nas.selectedCount', { count: 1 }))).toBeInTheDocument();

    fireEvent.click(screen.getByText('logs'));

    await waitFor(() => expect(screen.getByText('2026.txt')).toBeInTheDocument());
    expect(screen.queryByText(i18n.t('nas.selectedCount', { count: 1 }))).not.toBeInTheDocument();
    expect(selectBox('2026.txt')).not.toBeChecked();
  });

  it('includes the batch ZIP endpoint in the API examples', async () => {
    mockEntries.mockResolvedValue(
      makeListResponse({
        files: [makeEntry({ name: 'file.txt', path: 'file.txt', size: 5 })],
        total_count: 1,
      }),
    );
    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: labelRe('nas.apiExamples') }));
    const dialog = await screen.findByRole('dialog');

    expect(dialog).toHaveTextContent('/api/nas/nas-main/download-zip');
    expect(dialog).toHaveTextContent('-X POST');
    expect(dialog).toHaveTextContent('{"paths":["file.txt"]}');
    // The single-file example must survive alongside it.
    expect(dialog).toHaveTextContent('/api/nas/nas-main/download?path=file.txt');
  });
});
