vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  createQueryTemplate: vi.fn(),
  deleteQueryTemplate: vi.fn(),
  executeQueryTemplate: vi.fn(),
  getDatabases: vi.fn(),
  getQueryTemplates: vi.fn(),
  updateQueryTemplate: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  createQueryTemplate,
  deleteQueryTemplate,
  executeQueryTemplate,
  getDatabases,
  getQueryTemplates,
  updateQueryTemplate,
} from '../api/client';
import QueryTemplates from '../pages/QueryTemplates';
import { makeDatabase, renderWithProviders } from './helpers';

const mockedCreateQueryTemplate = vi.mocked(createQueryTemplate);
const mockedDeleteQueryTemplate = vi.mocked(deleteQueryTemplate);
const mockedExecuteQueryTemplate = vi.mocked(executeQueryTemplate);
const mockedGetDatabases = vi.mocked(getDatabases);
const mockedGetQueryTemplates = vi.mocked(getQueryTemplates);
const mockedUpdateQueryTemplate = vi.mocked(updateQueryTemplate);

const template = {
  id: 1,
  path: 'reports/users',
  name: 'Users report',
  description: 'Active users',
  database: 'maindb',
  sql: 'SELECT id, name FROM users WHERE id = :id',
  default_limit: 50,
  timeout: null,
  enabled: true,
  created_at: null,
  updated_at: null,
};

describe('QueryTemplates', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetDatabases.mockResolvedValue([makeDatabase({ alias: 'maindb' })]);
    mockedGetQueryTemplates.mockResolvedValue([template]);
    mockedCreateQueryTemplate.mockResolvedValue(template);
    mockedUpdateQueryTemplate.mockResolvedValue(template);
    mockedDeleteQueryTemplate.mockResolvedValue(undefined);
    mockedExecuteQueryTemplate.mockResolvedValue({
      columns: ['id', 'name'],
      rows: [[1, 'Alice']],
      row_count: 1,
      elapsed_ms: 11,
      truncated: false,
    });
  });

  it('renders saved templates and runs the selected template', async () => {
    const user = userEvent.setup();
    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => {
      expect(mockedExecuteQueryTemplate).toHaveBeenCalledWith('reports/users', {
        params: { id: 1 },
        limit: undefined,
        timeout: undefined,
      });
      expect(screen.getByText('Alice')).toBeInTheDocument();
    });
  });

  it('creates a new query template', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([]);

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'maindb' })).toBeInTheDocument();
    });

    await user.clear(screen.getByLabelText('Path'));
    await user.type(screen.getByLabelText('Path'), 'reports/orders');
    await user.type(screen.getByLabelText('Name'), 'Orders report');
    await user.selectOptions(screen.getByLabelText('Database'), 'maindb');

    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateQueryTemplate).toHaveBeenCalledWith(
        expect.objectContaining({
          path: 'reports/orders',
          name: 'Orders report',
          database: 'maindb',
          enabled: true,
        }),
      );
    });
  });

  it('edits and deletes an existing template', async () => {
    const user = userEvent.setup();
    window.confirm = vi.fn(() => true);

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Edit' }));
    await user.clear(screen.getByLabelText('Name'));
    await user.type(screen.getByLabelText('Name'), 'Renamed report');
    await user.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => {
      expect(mockedUpdateQueryTemplate).toHaveBeenCalledWith(
        'reports/users',
        expect.objectContaining({ name: 'Renamed report' }),
      );
    });

    await user.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith('Delete query template "reports/users"?');
      expect(mockedDeleteQueryTemplate.mock.calls[0][0]).toBe('reports/users');
    });
  });
});
