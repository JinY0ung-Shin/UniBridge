vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getS3Buckets: vi.fn(),
  getS3Objects: vi.fn(),
  getS3ObjectMetadata: vi.fn(),
  downloadS3Object: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ alias: 's3-main' }),
    useNavigate: () => vi.fn(),
  };
});

import { screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AxiosError, type AxiosResponse } from 'axios';
import {
  getS3Buckets,
  getS3Objects,
  getS3ObjectMetadata,
  downloadS3Object,
  type S3ListObjectsResponse,
} from '../api/client';
import S3Browser from '../pages/S3Browser';
import { renderWithProviders } from './helpers';

const mockBuckets = vi.mocked(getS3Buckets);
const mockObjects = vi.mocked(getS3Objects);
const mockMeta = vi.mocked(getS3ObjectMetadata);
const mockDownload = vi.mocked(downloadS3Object);

describe('S3Browser page', () => {
  beforeEach(() => {
    mockBuckets.mockReset();
    mockObjects.mockReset();
    mockMeta.mockReset();
    mockDownload.mockReset();
  });

  it('shows empty state when no buckets', async () => {
    mockBuckets.mockResolvedValue([]);
    renderWithProviders(<S3Browser />);
    await waitFor(() => {
      expect(screen.getByText(/No buckets|버킷이 없/i)).toBeInTheDocument();
    });
  });

  it('shows error banner when bucket fetch fails', async () => {
    mockBuckets.mockRejectedValue(new Error('boom'));
    renderWithProviders(<S3Browser />);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent(/Failed to load|불러오지 못/i);
  });

  it('auto-selects first bucket and lists objects with folder navigation', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [{ prefix: 'logs/' }],
      objects: [{ key: 'README.md', size: 1234, last_modified: '2026-04-30T12:00:00Z' }],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 2,
    });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('logs')).toBeInTheDocument());
    expect(screen.getByRole('combobox', { name: /Select Bucket|버킷 선택/i })).toHaveAttribute('id', 's3-bucket-select');
    expect(screen.getByText('README.md')).toBeInTheDocument();
    expect(screen.getByText(/1\.2 KB/)).toBeInTheDocument();
  });

  it('filters the currently loaded S3 folder listing', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [{ prefix: 'logs/' }],
      objects: [
        { key: 'README.md', size: 1234, last_modified: '2026-04-30T12:00:00Z' },
        { key: 'orders.csv', size: 100, last_modified: null },
      ],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 3,
    });

    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('README.md')).toBeInTheDocument());
    const filter = screen.getByRole('searchbox', { name: /Filter current folder|현재 폴더 필터/i });

    fireEvent.change(filter, { target: { value: 'orders' } });
    expect(screen.getByText('orders.csv')).toBeInTheDocument();
    expect(screen.queryByText('README.md')).not.toBeInTheDocument();
    expect(screen.queryByText('logs')).not.toBeInTheDocument();

    fireEvent.change(filter, { target: { value: 'missing' } });
    expect(screen.getByText(/No matching objects|일치하는 오브젝트/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Clear filter|필터 지우기/i }));
    expect(screen.getByText('README.md')).toBeInTheDocument();
    expect(screen.getByText('logs')).toBeInTheDocument();
  });

  it('navigates into a folder via click', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [{ prefix: 'logs/' }],
        objects: [],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'logs/2026.txt', size: 0, last_modified: null }],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('logs')).toBeInTheDocument());
    const folderRow = screen.getByRole('button', { name: 'Open folder logs' });
    expect(folderRow).toHaveAttribute('tabindex', '0');
    fireEvent.click(folderRow);
    await waitFor(() => expect(screen.getByText('2026.txt')).toBeInTheDocument());
    expect(screen.getByRole('navigation', { name: 'S3 path' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'logs' })).toHaveAttribute('aria-current', 'page');
  });

  it('navigates into a folder via keyboard activation', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [{ prefix: 'logs/' }],
        objects: [],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'logs/2026.txt', size: 0, last_modified: null }],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('logs')).toBeInTheDocument());

    fireEvent.keyDown(screen.getByRole('button', { name: 'Open folder logs' }), { key: 'Enter' });

    await waitFor(() => expect(screen.getByText('2026.txt')).toBeInTheDocument());
  });

  it('navigates up from a sub-prefix via ".." row', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [{ prefix: 'a/' }],
        objects: [],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [{ prefix: 'a/b/' }],
        objects: [],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [{ prefix: 'a/' }],
        objects: [],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a')).toBeInTheDocument());
    fireEvent.click(screen.getByText('a'));
    await waitFor(() => expect(screen.getByText('b')).toBeInTheDocument());
    fireEvent.keyDown(screen.getByRole('button', { name: 'Go to parent folder' }), { key: ' ' });
    await waitFor(() => {
      expect(screen.queryByText('b')).not.toBeInTheDocument();
    });
  });

  it('handles paginated load-more (continuation token)', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: 'tok-1',
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'b.txt', size: 1, last_modified: null }],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());
    const loadMore = screen.getByRole('button', { name: /Load More|더 불러오기/i });
    fireEvent.click(loadMore);
    await waitFor(() => expect(screen.getByText('b.txt')).toBeInTheDocument());
    expect(mockObjects).toHaveBeenLastCalledWith(
      's3-main',
      expect.objectContaining({ continuation_token: 'tok-1' }),
      expect.any(AbortSignal),
    );
  });

  it('loads the whole remaining listing with Load All', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: 'tok-1',
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [{ prefix: 'zz/' }],
        objects: [
          { key: 'b.txt', size: 1, last_modified: null },
          { key: 'c.txt', size: 1, last_modified: null },
        ],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 3,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));

    await waitFor(() => expect(screen.getByText('c.txt')).toBeInTheDocument());
    expect(screen.getByText('a.txt')).toBeInTheDocument();
    expect(screen.getByText('zz')).toBeInTheDocument();
    expect(mockObjects).toHaveBeenLastCalledWith(
      's3-main',
      expect.objectContaining({ continuation_token: 'tok-1', all: true }),
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole('button', { name: /Load All|전체 불러오기/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load More|더 불러오기/i })).not.toBeInTheDocument();
  });

  it('drops a full listing that finishes after switching folders', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    let finishLoadAll: (value: S3ListObjectsResponse) => void = () => {};
    mockObjects
      .mockResolvedValueOnce({
        folders: [{ prefix: 'logs/' }],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: 'tok-1',
        key_count: 2,
      })
      .mockImplementationOnce(() => new Promise((resolve) => { finishLoadAll = resolve; }))
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'logs/inside.txt', size: 1, last_modified: null }],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 1,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));
    const loadAllSignal = mockObjects.mock.calls[1][2];
    fireEvent.click(screen.getByRole('button', { name: 'Open folder logs' }));
    await waitFor(() => expect(screen.getByText('inside.txt')).toBeInTheDocument());
    expect(loadAllSignal?.aborted).toBe(true);

    finishLoadAll({
      folders: [],
      objects: [{ key: 'stale.txt', size: 1, last_modified: null }],
      is_truncated: true,
      next_continuation_token: 'tok-stale',
      key_count: 1,
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.queryByText('stale.txt')).not.toBeInTheDocument();
    expect(screen.getByText('inside.txt')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load All|전체 불러오기/i })).not.toBeInTheDocument();
  });

  it('aborts an in-flight full listing when the page unmounts', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: 'tok-1',
        key_count: 1,
      })
      .mockImplementationOnce(() => new Promise(() => {}));
    const { unmount } = renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));
    const loadAllSignal = mockObjects.mock.calls[1][2];
    expect(loadAllSignal?.aborted).toBe(false);

    unmount();
    expect(loadAllSignal?.aborted).toBe(true);
  });

  it('restarts the listing with Load All when the page has no token', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockResolvedValueOnce({
        folders: [],
        objects: [
          { key: 'a.txt', size: 1, last_modified: null },
          { key: 'b.txt', size: 1, last_modified: null },
        ],
        is_truncated: false,
        next_continuation_token: null,
        key_count: 2,
      });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    expect(screen.getByText(/1\+ items|항목 1개 이상/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load More|더 불러오기/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));

    await waitFor(() => expect(screen.getByText('b.txt')).toBeInTheDocument());
    expect(screen.getAllByText('a.txt')).toHaveLength(1);  // replaced, not appended
    expect(mockObjects).toHaveBeenLastCalledWith(
      's3-main',
      expect.objectContaining({ continuation_token: undefined, all: true }),
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole('button', { name: /Load All|전체 불러오기/i })).not.toBeInTheDocument();
  });

  it('explains when a full listing still cannot resume', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    const page = {
      folders: [],
      objects: [{ key: 'a.txt', size: 1, last_modified: null }],
      is_truncated: true,
      next_continuation_token: null,
      key_count: 1,
    };
    mockObjects.mockResolvedValueOnce(page).mockResolvedValueOnce(page);
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());
    expect(screen.queryByText(/no continuation token|이어받기 토큰을 주지 않아/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));

    await waitFor(() => {
      expect(screen.getByText(/no continuation token|이어받기 토큰을 주지 않아/)).toBeInTheDocument();
    });
    // Still retryable: the larger page may have been skipped or failed this time.
    expect(screen.getByRole('button', { name: /Load All|전체 불러오기/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /Load More|더 불러오기/i })).not.toBeInTheDocument();
    expect(screen.getByText(/1\+ items|항목 1개 이상/)).toBeInTheDocument();
  });

  it('keeps the rows when a Load All restart fails', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: null,
        key_count: 1,
      })
      .mockRejectedValueOnce(new Error('gateway timeout'));
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));

    await waitFor(() => expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument());
    expect(screen.getByText('a.txt')).toBeInTheDocument();
    expect(screen.getByText(/1\+ items|항목 1개 이상/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Load All|전체 불러오기/i })).toBeEnabled();
  });

  it('shows a busy toast when the full listing is rejected with 429', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects
      .mockResolvedValueOnce({
        folders: [],
        objects: [{ key: 'a.txt', size: 1, last_modified: null }],
        is_truncated: true,
        next_continuation_token: 'tok-1',
        key_count: 1,
      })
      .mockRejectedValueOnce(new AxiosError('busy', 'ERR_BAD_REQUEST', undefined, undefined, {
        status: 429,
        statusText: 'Too Many Requests',
        headers: {},
        config: {},
        data: { detail: 'Too many full listings in progress; retry shortly' },
      } as AxiosResponse));
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Load All|전체 불러오기/i }));

    await waitFor(() => {
      expect(screen.getByText(/Too many full listings|전체 불러오기 요청이 많아/i)).toBeInTheDocument();
    });
    expect(screen.getByText('a.txt')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Load All|전체 불러오기/i })).toBeEnabled();
  });

  it('caps rendered rows while the filter still searches every loaded entry', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    const objects = Array.from({ length: 2001 }, (_, i) => ({
      key: `file-${String(i).padStart(4, '0')}.bin`,
      size: 1,
      last_modified: null,
    }));
    mockObjects.mockResolvedValue({
      folders: [],
      objects,
      is_truncated: false,
      next_continuation_token: null,
      key_count: objects.length,
    });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('file-0000.bin')).toBeInTheDocument());

    expect(screen.getByText('file-1999.bin')).toBeInTheDocument();
    expect(screen.queryByText('file-2000.bin')).not.toBeInTheDocument();
    expect(screen.getByText(/first 2000 of 2001|2001개 중 처음 2000개/)).toBeInTheDocument();

    fireEvent.change(
      screen.getByRole('searchbox', { name: /Filter current folder|현재 폴더 필터/i }),
      { target: { value: 'file-2000' } },
    );
    expect(screen.getByText('file-2000.bin')).toBeInTheDocument();
    expect(screen.queryByText(/first 2000 of|개 중 처음/)).not.toBeInTheDocument();
  });

  it('shows toast on object listing failure', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockRejectedValue(new Error('list failed'));
    renderWithProviders(<S3Browser />);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument();
    });
  });

  it('downloads an object using download helper', async () => {
    const blob = new Blob(['hi']);
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [],
      objects: [{ key: 'file.txt', size: 5, last_modified: null }],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 1,
    });
    mockDownload.mockResolvedValue({ blob, filename: 'file.txt' });

    // Stub URL.createObjectURL/revokeObjectURL and anchor click for jsdom
    const createObjectURL = vi.fn(() => 'blob:mock');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());
    const downloadButton = screen.getByRole('button', { name: 'Download file.txt' });
    expect(downloadButton).toHaveAttribute('title', 'Download file.txt');
    fireEvent.click(downloadButton);
    await waitFor(() => expect(mockDownload).toHaveBeenCalled());
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('shows toast when download fails', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [],
      objects: [{ key: 'file.txt', size: 5, last_modified: null }],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 1,
    });
    mockDownload.mockRejectedValue(new Error('nope'));
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Download file.txt' }));
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to generate download|다운로드 URL 생성 실패/i),
      ).toBeInTheDocument();
    });
  });

  it('opens metadata modal on metadata click', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [],
      objects: [{ key: 'file.txt', size: 5, last_modified: null }],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 1,
    });
    mockMeta.mockResolvedValue({
      key: 'file.txt',
      size: 5,
      content_type: 'text/plain',
      last_modified: null,
      etag: '"abc"',
      storage_class: 'STANDARD',
      metadata: { foo: 'bar' },
    });
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Show metadata for file.txt' }));
    const dialog = await screen.findByRole('dialog', { name: /Metadata|메타데이터/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText('Content-Type')).toBeInTheDocument();
    expect(screen.getByText('text/plain')).toBeInTheDocument();
    expect(screen.getByText(/x-amz-meta-foo/)).toBeInTheDocument();
  });

  it('shows toast on metadata fetch failure', async () => {
    mockBuckets.mockResolvedValue([{ name: 'bk-1', creation_date: null }]);
    mockObjects.mockResolvedValue({
      folders: [],
      objects: [{ key: 'file.txt', size: 5, last_modified: null }],
      is_truncated: false,
      next_continuation_token: null,
      key_count: 1,
    });
    mockMeta.mockRejectedValue(new Error('nope'));
    renderWithProviders(<S3Browser />);
    await waitFor(() => expect(screen.getByText('file.txt')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Show metadata for file.txt' }));
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load metadata|메타데이터 조회 실패/i),
      ).toBeInTheDocument();
    });
  });
});
