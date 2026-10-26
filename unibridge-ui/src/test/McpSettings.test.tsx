vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMcpSettings: vi.fn(),
  updateMcpSettings: vi.fn(),
  getMcpPreview: vi.fn(),
  getMcpTemplates: vi.fn(),
  getScopedApiKeys: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getMcpPreview,
  getMcpSettings,
  getMcpTemplates,
  getScopedApiKeys,
  updateMcpSettings,
  type ApiKey,
  type McpPreview,
  type McpSettings,
} from '../api/client';
import McpSettingsPage from '../pages/McpSettings';
import { renderWithProviders } from './helpers';

const mockedGetMcpSettings = vi.mocked(getMcpSettings);
const mockedUpdateMcpSettings = vi.mocked(updateMcpSettings);
const mockedGetMcpPreview = vi.mocked(getMcpPreview);
const mockedGetMcpTemplates = vi.mocked(getMcpTemplates);
const mockedGetScopedApiKeys = vi.mocked(getScopedApiKeys);

const WRITE_PERMISSIONS = ['admin.config.read', 'admin.config.write', 'apikeys.read'];
const READ_PERMISSIONS = ['admin.config.read'];

const defaultSettings: McpSettings = {
  enabled: true,
  tool_groups: { templates: true, db_explore: true, run_query: true, s3: true, nas: true },
  excluded_templates: [],
  max_rows: 200,
  max_result_bytes: 65536,
};

function makeKey(name: string): ApiKey {
  return {
    name,
    description: '',
    api_key: null,
    key_created: true,
    allowed_databases: ['sales'],
    allowed_routes: ['mcp-api', 'query-api'],
    rate_limit_per_minute: null,
    owner: null,
    created_at: null,
  };
}

const preview: McpPreview = {
  consumer: 'agent-key',
  mcp_enabled: true,
  has_mcp_grant: true,
  groups: [
    { group: 'templates', enabled: true, granted: true, required_route: 'query-api' },
    { group: 'db_explore', enabled: true, granted: true, required_route: 'query-api' },
    { group: 'run_query', enabled: true, granted: true, required_route: 'query-api' },
    { group: 's3', enabled: true, granted: false, required_route: 's3-api' },
    { group: 'nas', enabled: false, granted: false, required_route: 'nas-api' },
  ],
  tools: [
    { name: 'list_databases', title: 'List databases', description: 'Databases this key may query.', group: 'db_explore' },
    { name: 'search_templates', title: 'Search query templates', description: 'Find saved query templates.', group: 'templates' },
  ],
  templates: [{ path: 'sales/daily', name: 'Daily sales', database: 'sales', enabled: true }],
};

const templates = [
  { path: 'ops/disabled', name: 'Old report', database: 'ops', enabled: false },
  { path: 'sales/daily', name: 'Daily sales', database: 'sales', enabled: true },
  { path: 'sales/monthly', name: 'Monthly sales', database: 'sales', enabled: true },
];

describe('McpSettingsPage', () => {
  beforeEach(() => {
    mockedGetMcpSettings.mockReset();
    mockedUpdateMcpSettings.mockReset();
    mockedGetMcpPreview.mockReset();
    mockedGetMcpTemplates.mockReset();
    mockedGetMcpTemplates.mockResolvedValue(templates);
    mockedGetScopedApiKeys.mockReset();
    mockedGetMcpSettings.mockResolvedValue(defaultSettings);
    mockedUpdateMcpSettings.mockResolvedValue(defaultSettings);
    mockedGetScopedApiKeys.mockResolvedValue([makeKey('agent-key'), makeKey('other-key')]);
    mockedGetMcpPreview.mockResolvedValue(preview);
  });

  it('renders the settings and the connection guide', async () => {
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    await waitFor(() => {
      expect(screen.getByRole('checkbox', { name: 'Enable the MCP endpoint' })).toBeChecked();
    });
    expect(screen.getByRole('checkbox', { name: 'Read-only SQL (run_query)' })).toBeChecked();
    expect(screen.getByRole('spinbutton', { name: 'Max rows per result' })).toHaveValue(200);
    expect(screen.getByRole('spinbutton', { name: 'Max result size (bytes)' })).toHaveValue(65536);
    expect(screen.getByText('No MCP changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    const endpoint = `${window.location.origin}/api/mcp`;
    expect(screen.getByText(endpoint)).toBeInTheDocument();
    expect(
      screen.getByText(`claude mcp add --transport http unibridge ${endpoint} --header "apikey: <API_KEY>"`),
    ).toBeInTheDocument();
  });

  it('saves the full settings body', async () => {
    mockedUpdateMcpSettings.mockResolvedValue({
      ...defaultSettings,
      tool_groups: { ...defaultSettings.tool_groups, run_query: false },
      max_rows: 50,
    });
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    const runQuery = await screen.findByRole('checkbox', { name: 'Read-only SQL (run_query)' });
    await userEvent.click(runQuery);
    const maxRows = screen.getByRole('spinbutton', { name: 'Max rows per result' });
    await userEvent.clear(maxRows);
    await userEvent.type(maxRows, '50');

    expect(screen.getByText('Unsaved MCP changes')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockedUpdateMcpSettings).toHaveBeenCalledTimes(1);
    });
    expect(mockedUpdateMcpSettings.mock.calls[0][0]).toEqual({
      enabled: true,
      tool_groups: { templates: true, db_explore: true, run_query: false, s3: true, nas: true },
      excluded_templates: [],
      max_rows: 50,
      max_result_bytes: 65536,
    });
    expect(await screen.findByText('MCP settings saved.')).toBeInTheDocument();
  });

  it('hides chosen templates from MCP', async () => {
    mockedGetMcpSettings.mockResolvedValue({ ...defaultSettings, excluded_templates: ['sales/monthly'] });
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    const daily = await screen.findByRole('checkbox', { name: 'sales/daily' });
    expect(daily).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'sales/monthly' })).not.toBeChecked();
    expect(screen.getByText('1 of 2 enabled templates offered')).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();

    await userEvent.click(daily);
    expect(screen.getByText('Unsaved MCP changes')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => {
      expect(mockedUpdateMcpSettings).toHaveBeenCalledTimes(1);
    });
    expect(mockedUpdateMcpSettings.mock.calls[0][0].excluded_templates).toEqual([
      'sales/daily',
      'sales/monthly',
    ]);
  });

  it('offers or hides every template the filter shows', async () => {
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    await screen.findByRole('checkbox', { name: 'sales/daily' });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter by path, name or database' }), 'monthly');
    expect(screen.queryByRole('checkbox', { name: 'sales/daily' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Hide all shown' }));
    expect(screen.getByRole('checkbox', { name: 'sales/monthly' })).not.toBeChecked();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Filter by path, name or database' }));
    expect(screen.getByRole('checkbox', { name: 'sales/daily' })).toBeChecked();
    expect(screen.getByText('1 of 2 enabled templates offered')).toBeInTheDocument();
  });

  it('blocks saving an out-of-range limit', async () => {
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    const maxBytes = await screen.findByRole('spinbutton', { name: 'Max result size (bytes)' });
    await userEvent.clear(maxBytes);
    await userEvent.type(maxBytes, '10');

    expect(screen.getByText('Enter a whole number between 1024 and 1048576.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('hides saving and disables inputs without admin.config.write', async () => {
    renderWithProviders(<McpSettingsPage />, { permissions: READ_PERMISSIONS });

    const enabled = await screen.findByRole('checkbox', { name: 'Enable the MCP endpoint' });
    expect(enabled).toBeDisabled();
    expect(screen.getByText('You have read-only access to MCP settings.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('previews the tools of a selected key', async () => {
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    const select = await screen.findByRole('combobox', { name: 'API key' });
    await screen.findByRole('option', { name: 'agent-key' });
    await userEvent.selectOptions(select, 'agent-key');

    expect(await screen.findByText('search_templates')).toBeInTheDocument();
    expect(mockedGetMcpPreview).toHaveBeenCalledWith('agent-key');
    expect(screen.getByText('list_databases')).toBeInTheDocument();
    expect(screen.getByText('Find saved query templates.')).toBeInTheDocument();
    const keyTemplates = screen.getByRole('table', { name: 'Templates for this key' });
    expect(keyTemplates).toHaveTextContent('sales/daily');
    expect(screen.getByText('Templates for this key (1)')).toBeInTheDocument();
    expect(screen.getByText('Tools (2)')).toBeInTheDocument();
    const groups = screen.getByRole('table', { name: 'Tool groups for this key' });
    expect(groups).toHaveTextContent('s3-api');
    expect(screen.queryByText('This key lacks the mcp-api grant, so /api/mcp rejects it.')).not.toBeInTheDocument();
  });

  it('asks for a key name without apikeys.read and reports an unknown key', async () => {
    mockedGetMcpPreview.mockRejectedValue({ isAxiosError: true, response: { status: 404 } });
    renderWithProviders(<McpSettingsPage />, { permissions: READ_PERMISSIONS });

    const input = await screen.findByRole('textbox', { name: 'API key name' });
    expect(mockedGetScopedApiKeys).not.toHaveBeenCalled();
    await userEvent.type(input, 'ghost');
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('No API key with that name.')).toBeInTheDocument();
    expect(mockedGetMcpPreview).toHaveBeenCalledWith('ghost');
  });

  it('warns when the key lacks the mcp-api grant', async () => {
    mockedGetMcpPreview.mockResolvedValue({ ...preview, has_mcp_grant: false, tools: [] });
    renderWithProviders(<McpSettingsPage />, { permissions: WRITE_PERMISSIONS });

    const select = await screen.findByRole('combobox', { name: 'API key' });
    await screen.findByRole('option', { name: 'other-key' });
    await userEvent.selectOptions(select, 'other-key');

    expect(
      await screen.findByText('This key lacks the mcp-api grant, so /api/mcp rejects it.'),
    ).toBeInTheDocument();
    expect(screen.getByText('This key gets no tools.')).toBeInTheDocument();
  });
});
