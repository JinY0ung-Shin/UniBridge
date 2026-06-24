vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getS3Connections: vi.fn(),
  createS3Connection: vi.fn(),
  updateS3Connection: vi.fn(),
  deleteS3Connection: vi.fn(),
  testS3Connection: vi.fn(),
}));

const navigateMock = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getS3Connections,
  createS3Connection,
  updateS3Connection,
  deleteS3Connection,
  testS3Connection,
} from '../api/client';
import S3Connections from '../pages/S3Connections';
import { renderWithProviders, makeS3Connection } from './helpers';

const mockGet = vi.mocked(getS3Connections);
const mockCreate = vi.mocked(createS3Connection);
const mockUpdate = vi.mocked(updateS3Connection);
const mockDelete = vi.mocked(deleteS3Connection);
const mockTest = vi.mocked(testS3Connection);
const clipboardWriteText = vi.fn();

describe('S3Connections CRUD', () => {
  beforeEach(() => {
    [mockGet, mockCreate, mockUpdate, mockDelete, mockTest].forEach((m) => m.mockReset());
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockReset();
    clipboardWriteText.mockResolvedValue(undefined);
    mockGet.mockResolvedValue([]);
    navigateMock.mockReset();
  });

  it('shows empty state and add button for admin', async () => {
    renderWithProviders(<S3Connections />);
    await waitFor(() =>
      expect(screen.getByText(/No S3 connections|S3 연결이 없/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /Add S3 Connection|S3 연결/i })).toBeInTheDocument();
  });

  it('renders connection rows with default-bucket fallback', async () => {
    mockGet.mockResolvedValue([
      { ...makeS3Connection(), alias: 'aws-s3', endpoint_url: undefined, default_bucket: undefined },
      makeS3Connection({ alias: 'minio-1', status: 'error' }),
    ]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('aws-s3')).toBeInTheDocument());
    expect(screen.getByText('AWS S3')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.getByText('minio-1')).toBeInTheDocument();
  });

  it('filters connections by search text', async () => {
    mockGet.mockResolvedValue([
      { ...makeS3Connection(), alias: 'aws-s3', endpoint_url: undefined, default_bucket: undefined },
      makeS3Connection({ alias: 'minio-1', endpoint_url: 'https://minio.example.com', default_bucket: 'warehouse' }),
    ]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('aws-s3')).toBeInTheDocument());

    const search = screen.getByRole('searchbox', { name: /search s3 connections/i });
    await userEvent.type(search, 'warehouse');

    expect(screen.queryByText('aws-s3')).not.toBeInTheDocument();
    expect(screen.getByText('minio-1')).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'missing');
    expect(screen.getByText(/No matching S3 connections|일치하는 S3 연결/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('aws-s3')).toBeInTheDocument();
    expect(screen.getByText('minio-1')).toBeInTheDocument();
  });

  it('opens create modal and submits new connection', async () => {
    mockCreate.mockResolvedValue(makeS3Connection());
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText(/No S3 connections/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Add S3 Connection/i }));

    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Alias' })).toBeInTheDocument(),
    );
    expect(screen.getByRole('dialog', { name: /Add S3 Connection/i })).toHaveAttribute('aria-modal', 'true');

    const aliasInput = screen.getByRole('textbox', { name: 'Alias' });
    expect(aliasInput).toHaveAttribute('id', 's3-alias');
    await userEvent.type(aliasInput, 'new-bucket');

    const regionInput = screen.getByRole('textbox', { name: 'Region' });
    expect(regionInput).toHaveAttribute('id', 's3-region');
    await userEvent.type(regionInput, 'us-west-2');

    expect(screen.getByRole('textbox', { name: 'Endpoint URL' })).toHaveAttribute(
      'aria-describedby',
      's3-endpoint-url-hint',
    );
    expect(document.getElementById('s3-endpoint-url-hint')).toHaveTextContent(
      'Endpoint for S3-compatible storage',
    );
    expect(screen.getByRole('textbox', { name: 'Default Bucket' })).toHaveAttribute(
      'aria-describedby',
      's3-default-bucket-hint',
    );
    expect(document.getElementById('s3-default-bucket-hint')).toHaveTextContent(
      'Bucket to open by default',
    );

    const accessInput = screen.getByRole('textbox', { name: 'Access Key ID' });
    await userEvent.type(accessInput, 'AKIA-NEW');

    const secretInput = screen.getByLabelText('Secret Access Key');
    await userEvent.type(secretInput, 'sekret');

    fireEvent.submit(aliasInput.closest('form')!);
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0][0].alias).toBe('new-bucket');
    expect(mockCreate.mock.calls[0][0].region).toBe('us-west-2');
  });

  it('opens edit modal with alias disabled and partial PUT', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'edit-me' })]);
    mockUpdate.mockResolvedValue(makeS3Connection({ alias: 'edit-me' }));
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('edit-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Edit S3 connection edit-me' }));

    await waitFor(() => expect(screen.getByText(/Edit "edit-me"|edit-me 편집/i)).toBeInTheDocument());

    const aliasInput = screen.getByRole('textbox', { name: 'Alias' }) as HTMLInputElement;
    expect(aliasInput.disabled).toBe(true);
    expect(screen.getByRole('textbox', { name: 'Access Key ID' })).toHaveAttribute(
      'aria-describedby',
      's3-access-key-id-hint',
    );
    expect(screen.getByLabelText('Secret Access Key')).toHaveAttribute(
      'aria-describedby',
      's3-secret-access-key-hint',
    );
    expect(document.getElementById('s3-secret-access-key-hint')).toHaveTextContent(
      'leave blank to keep current',
    );

    fireEvent.submit(aliasInput.closest('form')!);
    await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
    expect(mockUpdate.mock.calls[0][0]).toBe('edit-me');
    // Without typing access/secret keys they should be omitted
    expect(mockUpdate.mock.calls[0][1]).not.toHaveProperty('access_key_id');
    expect(mockUpdate.mock.calls[0][1]).not.toHaveProperty('secret_access_key');
  });

  it('test connection success shows badge', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 't1' })]);
    mockTest.mockResolvedValue({ status: 'ok', message: 'ok' });
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('t1')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Test S3 connection t1' }));
    await waitFor(() => expect(mockTest).toHaveBeenCalledWith('t1'));
  });

  it('test connection error path', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 't2' })]);
    mockTest.mockResolvedValue({ status: 'error', message: 'creds bad' });
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('t2')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Test S3 connection t2' }));
    await waitFor(() => expect(mockTest).toHaveBeenCalled());
  });

  it('test connection thrown error path', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 't3' })]);
    mockTest.mockRejectedValue(new Error('net'));
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('t3')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Test S3 connection t3' }));
    await waitFor(() => expect(mockTest).toHaveBeenCalled());
  });

  it('curl modal renders generated commands and copy works', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'curl-me', default_bucket: 'mybkt' })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('curl-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Show cURL for S3 connection curl-me' }));
    await waitFor(() => expect(screen.getByText(/cURL — curl-me/)).toBeInTheDocument());
    expect(screen.getByRole('dialog', { name: /cURL — curl-me/ })).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText(/objects\/download\?bucket=mybkt/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Copy cURL command' }));
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalled();
    });
    const dialog = screen.getByRole('dialog', { name: /cURL — curl-me/ });
    expect(within(dialog).getByRole('button', { name: 'cURL command copied' })).toHaveTextContent(/Copied|복사됨/i);
    expect(within(dialog).getByRole('status')).toHaveTextContent(/Copied|복사됨/i);
  });

  it('curl modal shows copy failure when clipboard write rejects', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('blocked'));
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'curl-fail', default_bucket: 'mybkt' })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('curl-fail')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Show cURL for S3 connection curl-fail' }));
    await waitFor(() => expect(screen.getByText(/cURL — curl-fail/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Copy cURL command' }));

    await waitFor(() => {
      expect(screen.getByText(/Failed to copy cURL command|cURL 명령을 복사하지 못/i)).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Copy cURL command' })).toHaveTextContent(/Copy|복사/i);
    expect(screen.queryByRole('button', { name: 'cURL command copied' })).not.toBeInTheDocument();
  });

  it('curl modal handles missing default_bucket placeholder', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'no-bk', default_bucket: null })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('no-bk')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Show cURL for S3 connection no-bk' }));
    await waitFor(() => expect(screen.getByText(/<BUCKET>/)).toBeInTheDocument());
  });

  it('browse navigates to /s3/browse/<alias>', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'browse-me' })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('browse-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Browse S3 connection browse-me' }));
    expect(navigateMock).toHaveBeenCalledWith('/s3/browse/browse-me');
  });

  it('delete confirmed calls API', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'del-me' })]);
    mockDelete.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('del-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete S3 connection del-me' }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('del-me'));
    cs.mockRestore();
  });

  it('delete cancelled does not call API', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'cancel-me' })]);
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('cancel-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete S3 connection cancel-me' }));
    expect(mockDelete).not.toHaveBeenCalled();
    cs.mockRestore();
  });

  it('shows error banner when fetch fails', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    renderWithProviders(<S3Connections />);
    await waitFor(() =>
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument(),
    );
  });
});
