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
import {
  getS3Buckets,
  getS3Objects,
  getS3ObjectMetadata,
  downloadS3Object,
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
    expect(screen.getByText('README.md')).toBeInTheDocument();
    expect(screen.getByText(/1\.2 KB/)).toBeInTheDocument();
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
    fireEvent.click(screen.getByText('logs'));
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
    fireEvent.click(screen.getByText('..'));
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
    );
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
    fireEvent.click(screen.getByRole('button', { name: /Download|다운로드/i }));
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
    fireEvent.click(screen.getByRole('button', { name: /Download|다운로드/i }));
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
    fireEvent.click(screen.getByRole('button', { name: /metadata|메타/i }));
    await waitFor(() => expect(screen.getByText('Content-Type')).toBeInTheDocument());
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
    fireEvent.click(screen.getByRole('button', { name: /metadata|메타/i }));
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load metadata|메타데이터 조회 실패/i),
      ).toBeInTheDocument();
    });
  });
});
