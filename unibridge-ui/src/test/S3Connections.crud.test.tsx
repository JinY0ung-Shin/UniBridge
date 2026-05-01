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

import { screen, waitFor, fireEvent } from '@testing-library/react';
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

describe('S3Connections CRUD', () => {
  beforeEach(() => {
    [mockGet, mockCreate, mockUpdate, mockDelete, mockTest].forEach((m) => m.mockReset());
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

  it('opens create modal and submits new connection', async () => {
    mockCreate.mockResolvedValue(makeS3Connection());
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText(/No S3 connections/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Add S3 Connection/i }));

    await waitFor(() =>
      expect(screen.getByPlaceholderText(/my-s3/i)).toBeInTheDocument(),
    );

    const aliasInput = screen.getByPlaceholderText(/my-s3/);
    await userEvent.type(aliasInput, 'new-bucket');

    const regionInput = screen.getByPlaceholderText('us-east-1');
    await userEvent.type(regionInput, 'us-west-2');

    const accessInput = screen.getByPlaceholderText('AKIAIOSFODNN7EXAMPLE');
    await userEvent.type(accessInput, 'AKIA-NEW');

    const secretInput = screen.getByPlaceholderText('********');
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
    fireEvent.click(screen.getByRole('button', { name: /^Edit$|^편집$/i }));

    await waitFor(() => expect(screen.getByText(/Edit "edit-me"|edit-me 편집/i)).toBeInTheDocument());

    const aliasInput = screen.getByPlaceholderText(/my-s3|edit-me/) as HTMLInputElement;
    expect(aliasInput.disabled).toBe(true);

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
    fireEvent.click(screen.getByRole('button', { name: /^Test$|^테스트$/i }));
    await waitFor(() => expect(mockTest).toHaveBeenCalledWith('t1'));
  });

  it('test connection error path', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 't2' })]);
    mockTest.mockResolvedValue({ status: 'error', message: 'creds bad' });
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('t2')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Test$|^테스트$/i }));
    await waitFor(() => expect(mockTest).toHaveBeenCalled());
  });

  it('test connection thrown error path', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 't3' })]);
    mockTest.mockRejectedValue(new Error('net'));
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('t3')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Test$|^테스트$/i }));
    await waitFor(() => expect(mockTest).toHaveBeenCalled());
  });

  it('curl modal renders generated commands and copy works', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'curl-me', default_bucket: 'mybkt' })]);
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('curl-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^cURL$/ }));
    await waitFor(() => expect(screen.getByText(/cURL — curl-me/)).toBeInTheDocument());
    expect(screen.getByText(/objects\/download\?bucket=mybkt/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Copy|복사|복사됨|Copied/i }));
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });

  it('curl modal handles missing default_bucket placeholder', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'no-bk', default_bucket: null })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('no-bk')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^cURL$/ }));
    await waitFor(() => expect(screen.getByText(/<BUCKET>/)).toBeInTheDocument());
  });

  it('browse navigates to /s3/browse/<alias>', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'browse-me' })]);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('browse-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Browse$|^찾아보기$/i }));
    expect(navigateMock).toHaveBeenCalledWith('/s3/browse/browse-me');
  });

  it('delete confirmed calls API', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'del-me' })]);
    mockDelete.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('del-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/i }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('del-me'));
    cs.mockRestore();
  });

  it('delete cancelled does not call API', async () => {
    mockGet.mockResolvedValue([makeS3Connection({ alias: 'cancel-me' })]);
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<S3Connections />);
    await waitFor(() => expect(screen.getByText('cancel-me')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/i }));
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
