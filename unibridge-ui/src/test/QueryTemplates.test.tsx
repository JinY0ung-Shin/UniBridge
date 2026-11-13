vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  createQueryTemplate: vi.fn(),
  deleteQueryTemplate: vi.fn(),
  executeQueryTemplate: vi.fn(),
  getDatabases: vi.fn(),
  getQueryTemplateGuide: vi.fn(),
  getQueryTemplateHealth: vi.fn(),
  getQueryTemplates: vi.fn(),
  getQueryTemplateUsage: vi.fn(),
  updateQueryTemplate: vi.fn(),
}));

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  createQueryTemplate,
  deleteQueryTemplate,
  executeQueryTemplate,
  getDatabases,
  getQueryTemplateGuide,
  getQueryTemplateHealth,
  getQueryTemplates,
  getQueryTemplateUsage,
  updateQueryTemplate,
  type QueryTemplateHealth,
} from '../api/client';
import QueryTemplates from '../pages/QueryTemplates';
import { ADMIN_PERMISSIONS, makeDatabase, renderWithProviders } from './helpers';

const mockedCreateQueryTemplate = vi.mocked(createQueryTemplate);
const mockedDeleteQueryTemplate = vi.mocked(deleteQueryTemplate);
const mockedExecuteQueryTemplate = vi.mocked(executeQueryTemplate);
const mockedGetDatabases = vi.mocked(getDatabases);
const mockedGetQueryTemplateGuide = vi.mocked(getQueryTemplateGuide);
const mockedGetQueryTemplateHealth = vi.mocked(getQueryTemplateHealth);
const mockedGetQueryTemplates = vi.mocked(getQueryTemplates);
const mockedGetQueryTemplateUsage = vi.mocked(getQueryTemplateUsage);
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
  sample_params: null as Record<string, unknown> | null,
  monitor_enabled: true,
  created_at: null,
  updated_at: null,
};

function makeHealth(overrides: Partial<QueryTemplateHealth> = {}): QueryTemplateHealth {
  return {
    level: 'ok',
    reason: null,
    message: 'Sample returned rows',
    error: null,
    row_count: 3,
    elapsed_ms: 12,
    checked_at: '2026-11-13T01:02:03Z',
    alerting: false,
    since: null,
    ...overrides,
  };
}

function listRow(path: string): HTMLElement {
  return screen.getByRole('button', { name: new RegExp(`templates/${path}`) });
}

function healthPill(path: string): HTMLElement {
  return listRow(path).querySelector('.template-health-pill') as HTMLElement;
}

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
    mockedGetQueryTemplateUsage.mockResolvedValue({ window_days: 7, retention_days: 0, templates: {} });
    mockedGetQueryTemplateHealth.mockResolvedValue({ interval_minutes: 10, templates: {} });
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

  it('counts saved templates in the list header, including search matches', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([
      template,
      { ...template, id: 2, path: 'reports/orders', name: 'Orders report', description: 'Order totals' },
    ]);

    renderWithProviders(<QueryTemplates />);

    expect(await screen.findByRole('heading', { name: 'Saved Templates 2' })).toBeInTheDocument();

    await user.type(screen.getByRole('searchbox', { name: 'Search templates...' }), 'orders');

    expect(screen.getByRole('heading', { name: 'Saved Templates 1 / 2' })).toBeInTheDocument();
  });

  it('shows 7-day run counts per template to audit readers', async () => {
    mockedGetQueryTemplates.mockResolvedValue([
      template,
      { ...template, id: 2, path: 'reports/orders', name: 'Orders report' },
    ]);
    mockedGetQueryTemplateUsage.mockResolvedValue({
      window_days: 7,
      retention_days: 3,
      templates: {
        'reports/users': {
          total: 1234,
          success: 1230,
          failed: 4,
          last_executed_at: '2026-11-03T01:02:03Z',
        },
      },
    });

    renderWithProviders(<QueryTemplates />);

    const usersRow = await screen.findByRole('button', { name: /templates\/reports\/users/ });
    await waitFor(() => expect(usersRow).toHaveTextContent('7d: 1,234'));
    expect(screen.getByRole('button', { name: /templates\/reports\/orders/ })).toHaveTextContent('7d: 0');
    expect(
      screen.getByText(/1,234 runs in the last 7 days · 1,230 succeeded · 4 failed · last run 2026/),
    ).toHaveTextContent('(only the 3 days kept by query audit retention are counted)');
  });

  it('hides run counts from users without query audit access', async () => {
    renderWithProviders(<QueryTemplates />, {
      permissions: ADMIN_PERMISSIONS.filter((permission) => permission !== 'query.audit.read'),
    });

    const usersRow = await screen.findByRole('button', { name: /templates\/reports\/users/ });
    expect(usersRow).not.toHaveTextContent('7d:');
    expect(mockedGetQueryTemplateUsage).not.toHaveBeenCalled();
  });

  it('opens the agent API guide from the page header', async () => {
    const user = userEvent.setup();
    const clipboardWriteText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    mockedGetQueryTemplateGuide.mockResolvedValue(
      '# Agent guide\n\n| Database | Placeholder |\n| --- | --- |\n| PostgreSQL | `:name` |\n',
    );

    renderWithProviders(<QueryTemplates />);

    await user.click(screen.getByRole('button', { name: 'Agent API' }));

    const dialog = screen.getByRole('dialog', { name: 'Agent API guide' });
    const guideUrl = `${window.location.origin}/api/query/templates/guide`;
    expect(within(dialog).getByText(new RegExp(guideUrl))).toBeInTheDocument();
    expect(within(dialog).getAllByText('query-api').length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText('query-template-write-api').length).toBeGreaterThan(0);
    expect(within(dialog).getByRole('row', { name: /PATCH \/api\/query\/templates\/\{path\}/ })).toHaveTextContent(
      'query-template-write-api',
    );
    expect(within(dialog).getByRole('link', { name: 'API Key Management' })).toHaveAttribute('href', '/api-keys');
    expect(within(dialog).queryByText(/Personal API keys cannot be granted/)).not.toBeInTheDocument();
    expect(within(dialog).getByText(/Disabled templates are hidden from agents/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Copy agent hand-off text' }));

    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(`curl -k -H 'apikey: <YOUR_API_KEY>' '${guideUrl}'`));
    expect(within(dialog).getByRole('button', { name: 'Agent hand-off text copied' })).toHaveTextContent('Copied');

    await user.click(within(dialog).getByRole('button', { name: 'Copy cURL command' }));

    // Edits carry expected_updated_at in the JSON body; deletes need it in the query string.
    const curlText = clipboardWriteText.mock.calls[1][0] as string;
    const templatesUrl = `${window.location.origin}/api/query/templates`;
    expect(curlText).toContain(`curl -k -X PATCH \\`);
    expect(curlText).toContain(`"expected_updated_at": "<UPDATED_AT>"`);
    // Create and edit both carry a sample.
    expect(curlText).toContain(`"sample_params": {"since": "2026-01-01T00:00:00Z"}}'`);
    expect(curlText).toContain(`"sample_params": {"since": "2026-06-01T00:00:00Z"}, "expected_updated_at"`);
    expect(within(dialog).getByText(/"sample_params": null removes the sample/)).toBeInTheDocument();
    expect(within(dialog).getByRole('row', { name: /GET \/api\/query\/templates / })).toHaveTextContent(
      'sample_params',
    );
    expect(curlText).toContain(`curl -k -X DELETE -H 'apikey: <YOUR_API_KEY>' \\\n  '${templatesUrl}/reports/new-users?expected_updated_at=<UPDATED_AT>'`);
    expect(within(dialog).getByRole('button', { name: 'cURL command copied' })).toHaveTextContent('Copied');

    // The full guide is fetched only when it is expanded.
    expect(mockedGetQueryTemplateGuide).not.toHaveBeenCalled();
    const disclosure = within(dialog).getByRole('button', { name: 'Full guide (the text agents read)' });
    await user.click(disclosure);

    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(await within(dialog).findByRole('heading', { name: 'Agent guide' })).toBeInTheDocument();
    expect(within(dialog).getByRole('region', { name: 'Scrollable guide table' })).toHaveAttribute('tabindex', '0');
    expect(mockedGetQueryTemplateGuide).toHaveBeenCalledTimes(1);

    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('explains why the full agent guide could not be loaded', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplateGuide.mockRejectedValue({
      response: { data: { detail: 'Required permission: query.execute' } },
    });

    renderWithProviders(<QueryTemplates />, {
      permissions: ['query.settings.read', 'apikeys.self'],
    });

    await user.click(screen.getByRole('button', { name: 'Agent API' }));
    const dialog = screen.getByRole('dialog', { name: 'Agent API guide' });
    expect(within(dialog).getByRole('link', { name: 'My API Key' })).toHaveAttribute('href', '/my-api-key');
    expect(within(dialog).getByText(/Personal API keys cannot be granted query-template-write-api/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Full guide (the text agents read)' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Failed to load the guide: Required permission: query.execute',
    );
  });

  it('reports a failed copy instead of claiming it was copied', async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('blocked')) },
    });

    renderWithProviders(<QueryTemplates />);

    await user.click(screen.getByRole('button', { name: 'Agent API' }));
    const dialog = screen.getByRole('dialog', { name: 'Agent API guide' });
    await user.click(within(dialog).getByRole('button', { name: 'Copy agent hand-off text' }));

    expect(await screen.findByText('Copy failed')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Copy agent hand-off text' })).toHaveTextContent('Copy');
  });

  it('omits the API key link for users who cannot manage keys', async () => {
    const user = userEvent.setup();

    renderWithProviders(<QueryTemplates />, {
      permissions: ADMIN_PERMISSIONS.filter((permission) => permission !== 'apikeys.read'),
    });

    await user.click(screen.getByRole('button', { name: 'Agent API' }));

    expect(within(screen.getByRole('dialog')).queryByRole('link')).not.toBeInTheDocument();
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
          sample_params: null,
          monitor_enabled: true,
        }),
      );
    });
  });

  it('sends the sample and the monitoring switch when creating a template', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([]);

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'maindb' })).toBeInTheDocument();
    });
    const sampleBox = screen.getByLabelText('Sample params (JSON)');
    expect(sampleBox).toHaveValue('');
    expect(sampleBox).toHaveAttribute('aria-describedby', 'template-sample-params-help');
    expect(document.getElementById('template-sample-params-help')).toHaveTextContent(
      'it must return at least one row',
    );
    expect(screen.getByLabelText('Monitor with sample')).toBeChecked();

    await user.type(screen.getByLabelText('Path'), 'reports/orders');
    await user.type(screen.getByLabelText('Name'), 'Orders report');
    await user.selectOptions(screen.getByLabelText('Database'), 'maindb');
    fireEvent.change(sampleBox, { target: { value: '{"id": 7, "tags": ["a"]}' } });
    await user.click(screen.getByLabelText('Monitor with sample'));
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockedCreateQueryTemplate).toHaveBeenCalledTimes(1));
    expect(mockedCreateQueryTemplate.mock.calls[0][0]).toEqual({
      path: 'reports/orders',
      name: 'Orders report',
      description: '',
      database: 'maindb',
      sql: 'SELECT * FROM users WHERE id = :id',
      default_limit: 100,
      timeout: null,
      enabled: true,
      sample_params: { id: 7, tags: ['a'] },
      monitor_enabled: false,
    });
    // A saved sample is checked right away, so the badges are fetched again.
    await waitFor(() => expect(mockedGetQueryTemplateHealth).toHaveBeenCalledTimes(2));
  });

  it('does not submit a sample that is not a JSON object', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([]);

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'maindb' })).toBeInTheDocument();
    });
    await user.type(screen.getByLabelText('Path'), 'reports/orders');
    await user.type(screen.getByLabelText('Name'), 'Orders report');
    await user.selectOptions(screen.getByLabelText('Database'), 'maindb');
    const sampleBox = screen.getByLabelText('Sample params (JSON)');

    for (const invalid of ['{"id": ', '[1, 2]', '"text"', 'null']) {
      fireEvent.change(sampleBox, { target: { value: invalid } });
      await user.click(screen.getByRole('button', { name: 'Create' }));
      expect(screen.getByRole('alert')).toHaveTextContent('Sample params must be a JSON object');
      expect(sampleBox).toHaveAttribute('aria-invalid', 'true');
    }
    expect(mockedCreateQueryTemplate).not.toHaveBeenCalled();

    // Editing the box takes the message away; `{}` is a sample for a template without parameters.
    fireEvent.change(sampleBox, { target: { value: '{}' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateQueryTemplate).toHaveBeenCalledWith(expect.objectContaining({ sample_params: {} }));
    });
  });

  it('shows why the backend refused a sample', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([]);
    mockedCreateQueryTemplate.mockRejectedValueOnce({
      response: { data: { detail: 'The sample returned no rows' } },
    });

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'maindb' })).toBeInTheDocument();
    });
    await user.type(screen.getByLabelText('Path'), 'reports/orders');
    await user.type(screen.getByLabelText('Name'), 'Orders report');
    await user.selectOptions(screen.getByLabelText('Database'), 'maindb');
    fireEvent.change(screen.getByLabelText('Sample params (JSON)'), { target: { value: '{"id": 0}' } });
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The sample returned no rows');
    // The form keeps what was typed so the sample can be corrected.
    expect(screen.getByLabelText('Sample params (JSON)')).toHaveValue('{"id": 0}');
  });

  it('loads the saved sample into the edit form and removes it when the box is emptied', async () => {
    const user = userEvent.setup();
    const sampled = { ...template, sample_params: { id: 42 }, monitor_enabled: false };
    mockedGetQueryTemplates.mockResolvedValue([sampled]);
    mockedUpdateQueryTemplate.mockResolvedValueOnce(sampled);

    renderWithProviders(<QueryTemplates />);

    await user.click(await screen.findByRole('button', { name: 'Edit query template "Users report"' }));
    const sampleBox = screen.getByLabelText('Sample params (JSON)');
    expect(sampleBox).toHaveValue('{\n  "id": 42\n}');
    expect(screen.getByLabelText('Monitor with sample')).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Update' }));
    await waitFor(() => {
      expect(mockedUpdateQueryTemplate).toHaveBeenLastCalledWith(
        'reports/users',
        expect.objectContaining({ sample_params: { id: 42 }, monitor_enabled: false }),
      );
    });

    await user.click(screen.getByRole('button', { name: 'Edit query template "Users report"' }));
    await user.clear(screen.getByLabelText('Sample params (JSON)'));
    await user.click(screen.getByLabelText('Monitor with sample'));
    await user.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(mockedUpdateQueryTemplate).toHaveBeenCalledTimes(2));
    expect(mockedUpdateQueryTemplate.mock.calls[1][1]).toEqual({
      name: 'Users report',
      description: 'Active users',
      database: 'maindb',
      sql: 'SELECT id, name FROM users WHERE id = :id',
      default_limit: 50,
      timeout: null,
      enabled: true,
      sample_params: null,
      monitor_enabled: true,
    });
  });

  it('shows a sample check badge per template and a summary for the selected one', async () => {
    const user = userEvent.setup();
    const sampled = { ...template, sample_params: { id: 1 } };
    mockedGetQueryTemplates.mockResolvedValue([
      { ...sampled, path: 'reports/ok', name: 'Ok report' },
      { ...sampled, id: 2, path: 'reports/empty', name: 'Empty report' },
      { ...sampled, id: 3, path: 'reports/broken', name: 'Broken report' },
      { ...template, id: 4, path: 'reports/plain', name: 'Plain report' },
      { ...sampled, id: 5, path: 'reports/new', name: 'New report' },
      { ...sampled, id: 6, path: 'reports/paused', name: 'Paused report', monitor_enabled: false },
    ]);
    mockedGetQueryTemplateHealth.mockResolvedValue({
      interval_minutes: 10,
      templates: {
        'reports/ok': makeHealth(),
        'reports/empty': makeHealth({
          level: 'warning',
          reason: 'no_rows',
          message: 'The sample returned no rows',
          row_count: 0,
          alerting: true,
          since: '2026-11-13T00:00:00Z',
        }),
        'reports/broken': makeHealth({
          level: 'error',
          reason: 'query_error',
          message: 'The sample run failed',
          error: 'relation "users" does not exist',
          row_count: null,
          elapsed_ms: null,
        }),
        // A result left over from before monitoring was switched off.
        'reports/paused': makeHealth({ level: 'error', reason: 'timeout', message: 'Timed out' }),
      },
    });

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => expect(healthPill('reports/ok')).toHaveTextContent('Sample OK'));
    expect(healthPill('reports/ok')).toHaveClass('status-pill--active');
    expect(healthPill('reports/empty')).toHaveTextContent('0 rows');
    expect(healthPill('reports/empty')).toHaveClass('status-pill--warning');
    expect(healthPill('reports/empty')).toHaveAttribute('title', 'The sample returned no rows');
    expect(healthPill('reports/broken')).toHaveTextContent('Sample error');
    expect(healthPill('reports/broken')).toHaveClass('status-pill--error');
    expect(healthPill('reports/plain')).toHaveTextContent('No sample');
    expect(healthPill('reports/plain')).toHaveClass('status-pill--disabled');
    expect(healthPill('reports/new')).toHaveTextContent('Not checked');
    expect(healthPill('reports/paused')).toHaveTextContent('Check off');
    expect(healthPill('reports/paused')).toHaveClass('status-pill--disabled');

    const detail = document.querySelector('.template-detail-panel') as HTMLElement;
    expect(
      within(detail).getByText(/^Sample check: OK · Sample returned rows · 3 row\(s\) · 12ms · last checked 2026/),
    ).toBeInTheDocument();

    await user.click(listRow('reports/empty'));
    const emptySummary = within(detail).getByText(/^Sample check: Warning · The sample returned no rows · 0 row/);
    expect(emptySummary).toHaveTextContent(/alert open since 2026/);
    expect(emptySummary).toHaveClass('template-usage-summary--warning');

    await user.click(listRow('reports/broken'));
    const brokenSummary = within(detail).getByText(/^Sample check: Error · The sample run failed/);
    expect(brokenSummary).toHaveTextContent('relation "users" does not exist · last checked');
    expect(brokenSummary).toHaveClass('template-usage-summary--error');

    await user.click(listRow('reports/plain'));
    expect(within(detail).getByText(/^Sample check: no sample is saved/)).toBeInTheDocument();

    await user.click(listRow('reports/new'));
    expect(within(detail).getByText('Sample check: not checked yet.')).toBeInTheDocument();

    await user.click(listRow('reports/paused'));
    expect(within(detail).getByText(/^Sample check: monitoring is switched off/)).toBeInTheDocument();
  });

  it('says so when periodic sample checks are switched off', async () => {
    const user = userEvent.setup();
    const sampled = { ...template, sample_params: { id: 1 } };
    mockedGetQueryTemplates.mockResolvedValue([
      { ...sampled, path: 'reports/ok', name: 'Ok report' },
      { ...sampled, id: 2, path: 'reports/new', name: 'New report' },
    ]);
    mockedGetQueryTemplateHealth.mockResolvedValue({
      interval_minutes: 0,
      templates: { 'reports/ok': makeHealth() },
    });

    renderWithProviders(<QueryTemplates />);

    // The last result stays on show; a template that was never checked is not "pending".
    await waitFor(() => expect(healthPill('reports/ok')).toHaveTextContent('Sample OK'));
    expect(healthPill('reports/new')).toHaveTextContent('Check off');
    const detail = document.querySelector('.template-detail-panel') as HTMLElement;
    expect(within(detail).getByText(/^Sample check: OK/)).toHaveTextContent(/Periodic checks are off\.$/);

    await user.click(listRow('reports/new'));
    expect(
      within(detail).getByText('Sample check: not checked yet. Periodic checks are off.'),
    ).toBeInTheDocument();
  });

  it('keeps the page working when the sample check results cannot be loaded', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([
      { ...template, sample_params: { id: 1 } },
      { ...template, id: 2, path: 'reports/plain', name: 'Plain report' },
    ]);
    mockedGetQueryTemplateHealth.mockRejectedValue(new Error('boom'));

    renderWithProviders(<QueryTemplates />);

    await waitFor(() => expect(healthPill('reports/users')).toHaveTextContent('—'));
    // Whether a sample exists does not depend on the failed request.
    expect(healthPill('reports/plain')).toHaveTextContent('No sample');
    expect(screen.getByText('Could not load sample check results.')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load query templates.')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('Alice')).toBeInTheDocument();
  });

  it('prefills the run panel from the selected template sample without clobbering typed params', async () => {
    const user = userEvent.setup();
    mockedGetQueryTemplates.mockResolvedValue([
      { ...template, sample_params: { id: 42, active: true } },
      { ...template, id: 2, path: 'reports/orders', name: 'Orders report', sample_params: {} },
      { ...template, id: 3, path: 'reports/plain', name: 'Plain report' },
    ]);

    renderWithProviders(<QueryTemplates />);

    const paramsBox = await screen.findByRole('textbox', { name: 'Params JSON' });
    expect(paramsBox).toHaveValue('{\n  "id": 42,\n  "active": true\n}');

    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => {
      expect(mockedExecuteQueryTemplate).toHaveBeenCalledWith('reports/users', {
        params: { id: 42, active: true },
        limit: undefined,
        timeout: undefined,
      });
    });

    await user.click(listRow('reports/orders'));
    expect(paramsBox).toHaveValue('{}');
    await user.click(listRow('reports/plain'));
    expect(paramsBox).toHaveValue('{\n  "id": 1\n}');

    // Typed params survive a refetch of the same template...
    await user.click(listRow('reports/users'));
    fireEvent.change(paramsBox, { target: { value: '{"id": 7}' } });
    await user.click(screen.getByRole('button', { name: 'Refresh query templates' }));
    await waitFor(() => expect(mockedGetQueryTemplates).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mockedGetQueryTemplateHealth).toHaveBeenCalledTimes(2));
    expect(paramsBox).toHaveValue('{"id": 7}');

    // ...and another template starts from its own sample again.
    await user.click(listRow('reports/orders'));
    expect(paramsBox).toHaveValue('{}');
    await user.click(listRow('reports/users'));
    expect(paramsBox).toHaveValue('{\n  "id": 42,\n  "active": true\n}');
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
