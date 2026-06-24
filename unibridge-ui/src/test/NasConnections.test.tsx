vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getNasConnections: vi.fn(),
  createNasConnection: vi.fn(),
  updateNasConnection: vi.fn(),
  deleteNasConnection: vi.fn(),
  testNasConnection: vi.fn(),
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
  getNasConnections,
  createNasConnection,
  updateNasConnection,
  deleteNasConnection,
  testNasConnection,
} from '../api/client';
import i18n from '../i18n';
import NasConnections from '../pages/NasConnections';
import { renderWithProviders } from './helpers';

const mockGet = vi.mocked(getNasConnections);
const mockCreate = vi.mocked(createNasConnection);
const mockUpdate = vi.mocked(updateNasConnection);
const mockDelete = vi.mocked(deleteNasConnection);
const mockTest = vi.mocked(testNasConnection);

/**
 * NAS connections never carry credentials/secrets (local mount). Only the six
 * business columns from the contract are exposed: alias, base_path,
 * max_download_bytes, read_only, show_hidden, follow_symlinks.
 */
function makeNasConnection(overrides = {}) {
  return {
    alias: 'nas-main',
    base_path: '/mnt/share',
    read_only: true,
    max_download_bytes: null as number | null,
    show_hidden: false,
    follow_symlinks: false,
    status: 'registered',
    ...overrides,
  };
}

/** Resolve a button by label, tolerant of the exact translated wording. */
function nasButton(...keys: string[]) {
  const labels = keys.map((k) => i18n.t(k));
  const re = new RegExp(`^(${labels.map(escapeRegExp).join('|')})$`, 'i');
  return screen.getByRole('button', { name: re });
}

function escapeRegExp(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Admin permissions including NAS (helpers.ADMIN_PERMISSIONS predates NAS).
const NAS_ADMIN_PERMISSIONS = ['nas.connections.read', 'nas.connections.write', 'nas.browse'];

describe('NasConnections', () => {
  beforeEach(() => {
    [mockGet, mockCreate, mockUpdate, mockDelete, mockTest].forEach((m) => m.mockReset());
    mockGet.mockResolvedValue([]);
    navigateMock.mockReset();
  });

  it('renders connection rows and exposes NO credential fields anywhere', async () => {
    mockGet.mockResolvedValue([makeNasConnection()]);
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });

    await waitFor(() => expect(screen.getByText('nas-main')).toBeInTheDocument());

    // base_path is admin metadata and may be shown in the table.
    expect(screen.getByText('/mnt/share')).toBeInTheDocument();

    // Hard guarantee: NO S3-style credential / endpoint controls leaked in.
    expect(screen.queryByText(/access key/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/endpoint/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/region/i)).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('AKIAIOSFODNN7EXAMPLE')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('********')).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('filters connections by search text', async () => {
    mockGet.mockResolvedValue([
      makeNasConnection({ alias: 'nas-main', base_path: '/mnt/share' }),
      makeNasConnection({ alias: 'logs-archive', base_path: '/srv/logs' }),
    ]);
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('nas-main')).toBeInTheDocument());

    const search = screen.getByRole('searchbox', { name: /search nas connections|nas 연결 검색/i });
    await userEvent.type(search, 'logs');

    expect(screen.queryByText('nas-main')).not.toBeInTheDocument();
    expect(screen.getByText('logs-archive')).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'missing');
    expect(screen.getByText(i18n.t('nas.noSearchResults'))).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('nas-main')).toBeInTheDocument();
    expect(screen.getByText('logs-archive')).toBeInTheDocument();
  });

  it('shows empty state and add button for an admin (write permission)', async () => {
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() =>
      expect(screen.getByText(i18n.t('nas.noConnections'))).toBeInTheDocument(),
    );
    expect(nasButton('nas.addConnection')).toBeInTheDocument();
  });

  it('hides write actions for a read-only user', async () => {
    mockGet.mockResolvedValue([makeNasConnection()]);
    renderWithProviders(<NasConnections />, {
      permissions: ['nas.connections.read', 'nas.browse'],
    });

    await waitFor(() => expect(screen.getByText('nas-main')).toBeInTheDocument());

    expect(
      screen.queryByRole('button', { name: new RegExp(`^${escapeRegExp(i18n.t('nas.addConnection'))}$`, 'i') }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: new RegExp(`^${escapeRegExp(i18n.t('common.edit'))}$`, 'i') }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: new RegExp(`^${escapeRegExp(i18n.t('common.delete'))}$`, 'i') }),
    ).not.toBeInTheDocument();
    // Read-only users can still test + browse.
    expect(screen.getByRole('button', { name: 'Test NAS connection nas-main' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Browse NAS connection nas-main' })).toBeInTheDocument();
  });

  it('opens the create modal and submits a new connection (no credential inputs)', async () => {
    mockCreate.mockResolvedValue(makeNasConnection());
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText(i18n.t('nas.noConnections'))).toBeInTheDocument());

    fireEvent.click(nasButton('nas.addConnection'));

    const dialog = await screen.findByRole('dialog');
    const aliasInput = within(dialog).getByRole('textbox', { name: i18n.t('nas.alias') });
    expect(aliasInput).toHaveAttribute('id', 'nas-alias');
    await userEvent.type(aliasInput, 'new-nas');

    const basePathInput = within(dialog).getByRole('textbox', { name: i18n.t('nas.basePath') });
    expect(basePathInput).toHaveAttribute('aria-describedby', 'nas-base-path-hint');
    expect(document.getElementById('nas-base-path-hint')).toHaveTextContent(
      'Absolute path on the server',
    );
    expect(within(dialog).getByRole('spinbutton', { name: i18n.t('nas.maxDownloadBytes') })).toHaveAttribute(
      'aria-describedby',
      'nas-max-download-bytes-hint',
    );
    expect(document.getElementById('nas-max-download-bytes-hint')).toHaveTextContent(
      'download cap in bytes',
    );
    await userEvent.type(basePathInput, '/mnt/data');

    // The create modal must NOT carry any S3 credential fields.
    expect(within(dialog).queryByPlaceholderText('AKIAIOSFODNN7EXAMPLE')).not.toBeInTheDocument();
    expect(within(dialog).queryByPlaceholderText('********')).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/secret/i)).not.toBeInTheDocument();
    expect(dialog.querySelector('input[type="password"]')).toBeNull();

    fireEvent.submit(aliasInput.closest('form')!);
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0][0].alias).toBe('new-nas');
    expect(mockCreate.mock.calls[0][0].base_path).toBe('/mnt/data');
    // Contract: the create payload carries no credential keys.
    expect(mockCreate.mock.calls[0][0]).not.toHaveProperty('access_key_id');
    expect(mockCreate.mock.calls[0][0]).not.toHaveProperty('secret_access_key');
  });

  it('opens the edit modal with alias disabled and issues a partial PUT', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 'edit-me' })]);
    mockUpdate.mockResolvedValue(makeNasConnection({ alias: 'edit-me' }));
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('edit-me')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Edit NAS connection edit-me' }));

    const dialog = await screen.findByRole('dialog');
    const aliasInput = within(dialog).getByRole('textbox', { name: i18n.t('nas.alias') }) as HTMLInputElement;
    expect(aliasInput.disabled).toBe(true);

    fireEvent.submit(aliasInput.closest('form')!);
    await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
    expect(mockUpdate.mock.calls[0][0]).toBe('edit-me');
    // read_only is never updatable per contract.
    expect(mockUpdate.mock.calls[0][1]).not.toHaveProperty('read_only');
  });

  it('runs a connection test and reports the result', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 't1' })]);
    mockTest.mockResolvedValue({ status: 'ok', message: 'ok' });
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('t1')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Test NAS connection t1' }));
    await waitFor(() => expect(mockTest).toHaveBeenCalledWith('t1'));
  });

  it('surfaces a test failure without throwing', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 't2' })]);
    mockTest.mockRejectedValue(new Error('mount gone'));
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('t2')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Test NAS connection t2' }));
    await waitFor(() => expect(mockTest).toHaveBeenCalled());
  });

  it('navigates to the browser for the alias', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 'browse-me' })]);
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('browse-me')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Browse NAS connection browse-me' }));
    expect(navigateMock).toHaveBeenCalledWith('/nas/browse/browse-me');
  });

  it('deletes a connection after confirmation', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 'del-me' })]);
    mockDelete.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('del-me')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Delete NAS connection del-me' }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('del-me'));
    cs.mockRestore();
  });

  it('does not delete when the confirmation is cancelled', async () => {
    mockGet.mockResolvedValue([makeNasConnection({ alias: 'cancel-me' })]);
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('cancel-me')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Delete NAS connection cancel-me' }));
    expect(mockDelete).not.toHaveBeenCalled();
    cs.mockRestore();
  });

  it('shows an error banner when the fetch fails', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    renderWithProviders(<NasConnections />, { permissions: NAS_ADMIN_PERMISSIONS });
    await waitFor(() =>
      expect(screen.getByText(i18n.t('nas.loadFailed'))).toBeInTheDocument(),
    );
  });
});
