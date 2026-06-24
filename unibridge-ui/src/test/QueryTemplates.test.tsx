vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  createQueryTemplate: vi.fn(),
  deleteQueryTemplate: vi.fn(),
  executeQueryTemplate: vi.fn(),
  getDatabases: vi.fn(),
  getQueryTemplates: vi.fn(),
  updateQueryTemplate: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
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

type QueryResultFixture = {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
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

    const listPanel = document.querySelector('.template-list-panel') as HTMLElement;
    expect(within(listPanel).getByRole('button', { name: /Users report/ })).toHaveAttribute('aria-pressed', 'true');
    expect(within(listPanel).getByRole('button', { name: 'Refresh query templates' })).toBeInTheDocument();
    expect(screen.getAllByText('Parameter syntax')).toHaveLength(2);
    expect(screen.getByText(/Params JSON is always an object/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'SQL' })).toHaveAttribute(
      'aria-describedby',
      'template-sql-help',
    );
    expect(document.getElementById('template-sql-help')).toHaveTextContent(
      'SQL placeholder syntax depends on the database.',
    );
    expect(screen.getByRole('textbox', { name: 'Params JSON' })).toHaveAttribute(
      'aria-describedby',
      'template-run-params-help',
    );
    expect(document.getElementById('template-run-params-help')).toHaveTextContent(
      'Params JSON is always an object.',
    );

    await user.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => {
      expect(mockedExecuteQueryTemplate).toHaveBeenCalledWith('reports/users', {
        params: { id: 1 },
        limit: undefined,
        timeout: undefined,
      });
      expect(screen.getByText('Alice')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent('1 row(s) returned');
  });

  it('announces truncated template run results as an alert', async () => {
    const user = userEvent.setup();
    mockedExecuteQueryTemplate.mockResolvedValueOnce({
      columns: ['id'],
      rows: [[1]],
      row_count: 1,
      elapsed_ms: 11,
      truncated: true,
    });

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Results were truncated');
    });
  });

  it('marks the template run button as busy while execution is pending', async () => {
    const user = userEvent.setup();
    let resolveExecution: (value: QueryResultFixture) => void = () => {};
    mockedExecuteQueryTemplate.mockReturnValueOnce(new Promise<QueryResultFixture>((resolve) => {
      resolveExecution = resolve;
    }));

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Run' }));

    expect(screen.getByRole('button', { name: 'Executing...' })).toHaveAttribute('aria-busy', 'true');

    resolveExecution({
      columns: ['id'],
      rows: [[1]],
      row_count: 1,
      elapsed_ms: 1,
      truncated: false,
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run' })).toHaveAttribute('aria-busy', 'false');
    });
  });

  it('announces template run errors as an alert', async () => {
    const user = userEvent.setup();
    mockedExecuteQueryTemplate.mockRejectedValueOnce(new Error('Template execution failed'));

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Template execution failed');
    });
  });

  it('filters saved templates by search text', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([
      template,
      {
        ...template,
        id: 2,
        path: 'reports/orders',
        name: 'Orders report',
        description: 'Order totals',
        sql: 'SELECT id FROM orders',
      },
    ]);

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    const listPanel = document.querySelector('.template-list-panel') as HTMLElement;
    await user.type(screen.getByRole('searchbox', { name: 'Search templates...' }), 'orders');

    expect(within(listPanel).queryByText('Users report')).not.toBeInTheDocument();
    expect(within(listPanel).getByText('Orders report')).toBeInTheDocument();

    await user.clear(screen.getByRole('searchbox', { name: 'Search templates...' }));
    await user.type(screen.getByRole('searchbox', { name: 'Search templates...' }), 'missing');

    expect(within(listPanel).getByText('No matching templates')).toBeInTheDocument();
    await user.click(within(listPanel).getByRole('button', { name: 'Clear search' }));
    expect(within(listPanel).getByText('Users report')).toBeInTheDocument();
    expect(within(listPanel).getByText('Orders report')).toBeInTheDocument();
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
    mockedUpdateQueryTemplate.mockResolvedValueOnce({ ...template, name: 'Renamed report' });

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getAllByText('Users report').length).toBeGreaterThanOrEqual(1);
    });

    await user.click(screen.getByRole('button', { name: 'Edit query template "Users report"' }));
    await user.clear(screen.getByLabelText('Name'));
    await user.type(screen.getByLabelText('Name'), 'Renamed report');
    await user.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => {
      expect(mockedUpdateQueryTemplate).toHaveBeenCalledWith(
        'reports/users',
        expect.objectContaining({ name: 'Renamed report' }),
      );
    });

    await user.click(screen.getByRole('button', { name: 'Delete query template "Renamed report"' }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith('Delete query template "reports/users"?');
      expect(mockedDeleteQueryTemplate.mock.calls[0][0]).toBe('reports/users');
    });
  });
});
