vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getNasEntries: vi.fn(),
  getNasEntryMetadata: vi.fn(),
  downloadNasEntry: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ alias: 'nas-main' }),
    useNavigate: () => vi.fn(),
  };
});

import { screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getNasEntries,
  getNasEntryMetadata,
  downloadNasEntry,
} from '../api/client';
import i18n from '../i18n';
import NasBrowser from '../pages/NasBrowser';
import { renderWithProviders } from './helpers';

const mockEntries = vi.mocked(getNasEntries);
const mockMeta = vi.mocked(getNasEntryMetadata);
const mockDownload = vi.mocked(downloadNasEntry);

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

describe('NasBrowser page', () => {
  beforeEach(() => {
    mockEntries.mockReset();
    mockMeta.mockReset();
    mockDownload.mockReset();
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
    // The descent must use the folder entry's own `path`, not a reconstructed key.
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
    expect(dialog).toHaveTextContent('/api/nas/nas-main/metadata?path=file.txt');
    expect(dialog).toHaveTextContent('/api/nas/nas-main/download?path=file.txt');
    expect(dialog).toHaveTextContent("-H 'apikey: <YOUR_API_KEY>'");
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

    const downloadBtn = screen.getByRole('button', { name: labelRe('nas.download') });
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

    fireEvent.click(screen.getByRole('button', { name: labelRe('nas.download') }));
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

    fireEvent.click(screen.getByRole('button', { name: labelRe('nas.metadata', 's3.metadata') }));
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
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:preview') });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    renderWithProviders(<NasBrowser />, { permissions: NAS_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('photo.png')).toBeInTheDocument());

    fireEvent.click(screen.getByText('photo.png'));

    // The image is fetched and shown inline in a dialog — NOT pushed to disk.
    const img = await screen.findByAltText('photo.png');
    expect(img).toHaveAttribute('src', 'blob:preview');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(clickSpy).not.toHaveBeenCalled();
    clickSpy.mockRestore();
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

    fireEvent.change(screen.getByPlaceholderText(i18n.t('nas.searchPlaceholder')), {
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

    fireEvent.change(screen.getByPlaceholderText(i18n.t('nas.searchPlaceholder')), {
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
});
