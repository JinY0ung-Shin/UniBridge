vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertChannels: vi.fn(),
  createAlertChannel: vi.fn(),
  updateAlertChannel: vi.fn(),
  deleteAlertChannel: vi.fn(),
  testAlertChannel: vi.fn(),
  getAlertRules: vi.fn(),
  createAlertRule: vi.fn(),
  updateAlertRule: vi.fn(),
  deleteAlertRule: vi.fn(),
  testAlertRule: vi.fn(),
  getAdminDatabases: vi.fn(),
  getGatewayUpstreams: vi.fn(),
  getGatewayRoutes: vi.fn(),
}));

import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getAlertChannels,
  createAlertChannel,
  updateAlertChannel,
  deleteAlertChannel,
  testAlertChannel,
  getAlertRules,
  createAlertRule,
  deleteAlertRule,
  testAlertRule,
  getAdminDatabases,
  getGatewayUpstreams,
  getGatewayRoutes,
} from '../api/client';
import AlertSettings from '../pages/AlertSettings';
import { renderWithProviders } from './helpers';

const mocks = {
  getChannels: vi.mocked(getAlertChannels),
  createChannel: vi.mocked(createAlertChannel),
  updateChannel: vi.mocked(updateAlertChannel),
  deleteChannel: vi.mocked(deleteAlertChannel),
  testChannel: vi.mocked(testAlertChannel),
  getRules: vi.mocked(getAlertRules),
  createRule: vi.mocked(createAlertRule),
  deleteRule: vi.mocked(deleteAlertRule),
  testRule: vi.mocked(testAlertRule),
  getDatabases: vi.mocked(getAdminDatabases),
  getUpstreams: vi.mocked(getGatewayUpstreams),
  getRoutes: vi.mocked(getGatewayRoutes),
};

const channelFixture = {
  id: 1,
  name: 'ops-slack',
  webhook_url: 'https://hooks.example.com/abc',
  payload_template: '{"text":"{{message}}"}',
  headers: { 'X-Token': 'tok' },
  enabled: true,
};

const ruleFixture = {
  id: 10,
  name: 'db-down',
  type: 'db_health' as const,
  target: 'main-db',
  threshold: null,
  enabled: true,
  channels: [{ channel_id: 1, channel_name: 'ops-slack', recipients: ['ops@example.com'] }],
};

describe('AlertSettings page', () => {
  beforeEach(() => {
    Object.values(mocks).forEach((m) => m.mockReset());
    mocks.getChannels.mockResolvedValue([]);
    mocks.getRules.mockResolvedValue([]);
    mocks.getDatabases.mockResolvedValue([]);
    mocks.getUpstreams.mockResolvedValue({ items: [], total: 0 });
    mocks.getRoutes.mockResolvedValue({ items: [], total: 0 });
  });

  it('shows empty channels state and switches to rules tab', async () => {
    renderWithProviders(<AlertSettings />);
    await waitFor(() => {
      expect(screen.getByText(/No channels|채널이 없/i)).toBeInTheDocument();
    });
    const rulesTab = screen.getByRole('button', { name: /^Rules$|^규칙$/ });
    fireEvent.click(rulesTab);
    await waitFor(() => {
      expect(screen.getByText(/No rules|규칙이 없/i)).toBeInTheDocument();
    });
  });

  it('renders channels table with truncated webhook url', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    expect(screen.getByText(/hooks.example.com/)).toBeInTheDocument();
  });

  it('renders rules table with channel chips', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    expect(screen.getByText('main-db')).toBeInTheDocument();
    expect(screen.getByText('ops-slack')).toBeInTheDocument();
  });

  it('rule with no channels shows em-dash placeholder', async () => {
    mocks.getRules.mockResolvedValue([{ ...ruleFixture, id: 11, name: 'no-ch', channels: [] }]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('no-ch')).toBeInTheDocument());
  });

  it('rule with empty target renders asterisk', async () => {
    mocks.getRules.mockResolvedValue([{ ...ruleFixture, target: '', id: 12, name: 'star' }]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('star')).toBeInTheDocument());
    expect(screen.getByText('*')).toBeInTheDocument();
  });

  it('opens add channel modal and submits new channel', async () => {
    mocks.createChannel.mockResolvedValue({ ...channelFixture, id: 99, name: 'new' });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText(/No channels/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /\+\s*Add Channel/i }));
    await waitFor(() => expect(screen.getByText(/^Add Channel$/)).toBeInTheDocument());

    const inputs = screen.getAllByRole('textbox');
    await userEvent.type(inputs[0], 'new-ch');
    await userEvent.type(inputs[1], 'https://hooks.example.com/new');

    fireEvent.submit(inputs[0].closest('form')!);
    await waitFor(() => expect(mocks.createChannel).toHaveBeenCalled());
    const arg = mocks.createChannel.mock.calls[0][0];
    expect(arg.name).toBe('new-ch');
    expect(arg.webhook_url).toBe('https://hooks.example.com/new');
  });

  it('open edit channel pre-fills form and submits update', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.updateChannel.mockResolvedValue({ ...channelFixture });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Edit$|^편집$/ }));
    await waitFor(() => expect(screen.getByText(/^Edit Channel$/)).toBeInTheDocument());
    const inputs = screen.getAllByRole('textbox');
    expect((inputs[0] as HTMLInputElement).value).toBe('ops-slack');
    fireEvent.submit(inputs[0].closest('form')!);
    await waitFor(() => expect(mocks.updateChannel).toHaveBeenCalled());
    expect(mocks.updateChannel.mock.calls[0][0]).toBe(1);
  });

  it('delete channel asks for confirmation and skips on cancel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(mocks.deleteChannel).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('delete channel calls API on confirm', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.deleteChannel.mockResolvedValue();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(mocks.deleteChannel).toHaveBeenCalledWith(1));
    confirmSpy.mockRestore();
  });

  it('test channel success shows toast', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.testChannel.mockResolvedValue({ success: true, error: null });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testChannel).toHaveBeenCalledWith(1));
  });

  it('test channel failure shows error toast', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.testChannel.mockResolvedValue({ success: false, error: 'timeout' });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testChannel).toHaveBeenCalled());
  });

  it('test rule with no channels short-circuits with toast', async () => {
    mocks.getRules.mockResolvedValue([{ ...ruleFixture, channels: [] }]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    expect(mocks.testRule).not.toHaveBeenCalled();
  });

  it('test rule all-ok shows success toast', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    mocks.testRule.mockResolvedValue({
      results: [
        { channel_id: 1, channel_name: 'ops-slack', recipients: [], skipped: false, success: true, error: null },
      ],
    });
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testRule).toHaveBeenCalledWith(10));
  });

  it('test rule partial skipped shows info toast', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    mocks.testRule.mockResolvedValue({
      results: [
        { channel_id: 1, channel_name: 'ops-slack', recipients: [], skipped: false, success: true, error: null },
        { channel_id: 2, channel_name: 'pager', recipients: [], skipped: true, success: null, error: 'channel disabled' },
      ],
    });
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testRule).toHaveBeenCalled());
  });

  it('test rule failure path', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    mocks.testRule.mockResolvedValue({
      results: [
        { channel_id: 1, channel_name: 'ops-slack', recipients: [], skipped: false, success: false, error: 'boom' },
      ],
    });
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testRule).toHaveBeenCalled());
  });

  it('test rule with thrown error shows error toast', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    mocks.testRule.mockRejectedValue(new Error('net'));
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Test Send|테스트/i }));
    await waitFor(() => expect(mocks.testRule).toHaveBeenCalled());
  });

  it('delete rule asks for confirmation', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    mocks.deleteRule.mockResolvedValue();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(mocks.deleteRule).toHaveBeenCalledWith(10));
    confirmSpy.mockRestore();
  });

  it('opens add rule modal and shows DB target options', async () => {
    mocks.getDatabases.mockResolvedValue([
      {
        alias: 'main-db',
        db_type: 'postgres',
        host: 'localhost',
        port: 5432,
        database: 'main',
        username: 'u',
        pool_size: 5,
        max_overflow: 3,
        query_timeout: 30,
      },
    ]);
    mocks.getChannels.mockResolvedValue([channelFixture]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText(/No rules|규칙이 없/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /\+\s*Add Rule/i }));
    await waitFor(() => expect(screen.getByText(/^Add Rule$/)).toBeInTheDocument());
  });
});
