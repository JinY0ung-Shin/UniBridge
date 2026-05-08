vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertChannels: vi.fn(),
  createAlertChannel: vi.fn(),
  updateAlertChannel: vi.fn(),
  deleteAlertChannel: vi.fn(),
  testAlertChannel: vi.fn(),
  testFallbackOwnerGroup: vi.fn(),
  getAlertSettings: vi.fn(),
  updateAlertSettings: vi.fn(),
  getAlertOwnerGroups: vi.fn(),
  createAlertOwnerGroup: vi.fn(),
  updateAlertOwnerGroup: vi.fn(),
  deleteAlertOwnerGroup: vi.fn(),
  getAlertResourceOwners: vi.fn(),
  setAlertResourceOwner: vi.fn(),
  deleteAlertResourceOwner: vi.fn(),
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
  testFallbackOwnerGroup,
  getAlertSettings,
  updateAlertSettings,
  getAlertOwnerGroups,
  createAlertOwnerGroup,
  updateAlertOwnerGroup,
  deleteAlertOwnerGroup,
  getAlertResourceOwners,
  setAlertResourceOwner,
  deleteAlertResourceOwner,
  getAlertRules,
  createAlertRule,
  updateAlertRule,
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
  testFallbackOwnerGroup: vi.mocked(testFallbackOwnerGroup),
  getSettings: vi.mocked(getAlertSettings),
  updateSettings: vi.mocked(updateAlertSettings),
  getOwnerGroups: vi.mocked(getAlertOwnerGroups),
  createOwnerGroup: vi.mocked(createAlertOwnerGroup),
  updateOwnerGroup: vi.mocked(updateAlertOwnerGroup),
  deleteOwnerGroup: vi.mocked(deleteAlertOwnerGroup),
  getResourceOwners: vi.mocked(getAlertResourceOwners),
  setResourceOwner: vi.mocked(setAlertResourceOwner),
  deleteResourceOwner: vi.mocked(deleteAlertResourceOwner),
  getRules: vi.mocked(getAlertRules),
  createRule: vi.mocked(createAlertRule),
  updateRule: vi.mocked(updateAlertRule),
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
    mocks.getSettings.mockResolvedValue({
      mail_channel_id: null,
      fallback_owner_group_id: null,
      route_error_threshold_pct: 10,
      check_interval_seconds: 60,
    });
    mocks.updateSettings.mockResolvedValue({
      mail_channel_id: 1,
      fallback_owner_group_id: null,
      route_error_threshold_pct: 10,
      check_interval_seconds: 60,
    });
    mocks.getOwnerGroups.mockResolvedValue([]);
    mocks.createOwnerGroup.mockResolvedValue({
      id: 2,
      name: 'orders',
      emails: ['primary@example.com', 'backup@example.com'],
      enabled: true,
    });
    mocks.getResourceOwners.mockResolvedValue([]);
    mocks.getChannels.mockResolvedValue([]);
    mocks.getRules.mockResolvedValue([]);
    mocks.updateRule.mockResolvedValue(ruleFixture);
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

  it('mail channel tab saves selected channel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    const mailSelect = screen.getByLabelText(/Mail Channel|메일 채널/i);
    await userEvent.selectOptions(mailSelect, '1');
    fireEvent.click(screen.getByRole('button', { name: /^Save Settings$|^설정 저장$/i }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalled());
    expect(mocks.updateSettings.mock.calls[0][0]).toMatchObject({ mail_channel_id: 1 });
  });

  it('disables alert settings save before settings have loaded', async () => {
    mocks.getSettings.mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<AlertSettings />);

    expect(screen.getByRole('button', { name: /^Save Settings$|^설정 저장$/i })).toBeDisabled();
  });

  it('tests the selected fallback owner group with the selected mail channel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.getOwnerGroups.mockResolvedValue([
      { id: 2, name: 'orders-team', emails: ['orders@example.com'], enabled: true },
    ]);
    mocks.testFallbackOwnerGroup.mockResolvedValue({ success: true, error: null });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    const testButton = screen.getByRole('button', {
      name: /^Test Fallback Group$|^대체 그룹 테스트$/,
    });
    expect(testButton).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText(/Mail Channel|메일 채널/i), '1');
    expect(testButton).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText(/Fallback Owner Group|대체 소유자 그룹/i), '2');
    expect(testButton).toBeEnabled();
    fireEvent.click(testButton);

    await waitFor(() => expect(mocks.testFallbackOwnerGroup).toHaveBeenCalledWith(1, 2));
  });

  it('owner group tab creates group from comma-separated emails', async () => {
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Owner Groups$|^소유자 그룹$/i }));
    await waitFor(() => expect(screen.getByText(/No owner groups|소유자 그룹이 없/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /\+\s*Add Owner Group|\+\s*소유자 그룹 추가/i }));
    await userEvent.type(screen.getByLabelText(/Owner Group Name|소유자 그룹 이름/i), 'orders');
    await userEvent.type(
      screen.getByLabelText(/Emails|이메일/i),
      'primary@example.com, backup@example.com',
    );
    fireEvent.submit(screen.getByLabelText(/Owner Group Name|소유자 그룹 이름/i).closest('form')!);

    await waitFor(() => expect(mocks.createOwnerGroup).toHaveBeenCalled());
    expect(mocks.createOwnerGroup.mock.calls[0][0]).toMatchObject({
      name: 'orders',
      emails: ['primary@example.com', 'backup@example.com'],
      enabled: true,
    });
  });

  it('resource owners tab assigns owner group', async () => {
    mocks.getOwnerGroups.mockResolvedValue([
      { id: 2, name: 'orders-team', emails: ['orders@example.com'], enabled: true },
    ]);
    mocks.getResourceOwners.mockResolvedValue([
      {
        resource_type: 'db',
        resource_id: 'orders-db',
        display_name: 'orders-db',
        owner_group_id: null,
        owner_group_name: null,
      },
    ]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Resource Owners$|^리소스 소유자$/i }));
    await waitFor(() => expect(screen.getByText('orders-db')).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText(/Owner group for orders-db/i), '2');

    await waitFor(() =>
      expect(mocks.setResourceOwner).toHaveBeenCalledWith('db', 'orders-db', { owner_group_id: 2 }),
    );
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
    expect(screen.getByText('ops-slack: ops@example.com')).toBeInTheDocument();
    expect(screen.getByText(/DB, upstream, and route error rules now use resource owner routing/i)).toBeInTheDocument();
  });

  it('shows owner-routing warning for route error rules with legacy recipients', async () => {
    mocks.getRules.mockResolvedValue([
      {
        ...ruleFixture,
        id: 13,
        name: 'route-errors',
        type: 'route_error_rate',
        target: 'orders-route',
        threshold: 7,
      },
    ]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('route-errors')).toBeInTheDocument());
    expect(screen.getByText(/DB, upstream, and route error rules now use resource owner routing/i)).toBeInTheDocument();
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

    await userEvent.type(screen.getByLabelText(/Channel Name|채널 이름/i), 'new-ch');
    await userEvent.type(screen.getByLabelText(/Webhook URL/i), 'https://hooks.example.com/new');
    fireEvent.change(screen.getByLabelText(/Recipient Item Template|수신자 항목 템플릿/i), {
      target: { value: '{"emailAddress":"{{email}}","recipientType":"TO"}' },
    });

    fireEvent.submit(screen.getByLabelText(/Channel Name|채널 이름/i).closest('form')!);
    await waitFor(() => expect(mocks.createChannel).toHaveBeenCalled());
    const arg = mocks.createChannel.mock.calls[0][0];
    expect(arg.name).toBe('new-ch');
    expect(arg.webhook_url).toBe('https://hooks.example.com/new');
    expect(arg.recipient_item_template).toBe('{"emailAddress":"{{email}}","recipientType":"TO"}');
  });

  it('open edit channel pre-fills form and submits update', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.updateChannel.mockResolvedValue({ ...channelFixture });
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Edit$|^편집$/ }));
    await waitFor(() => expect(screen.getByText(/^Edit Channel$/)).toBeInTheDocument());
    expect(screen.getByLabelText(/Channel Name|채널 이름/i)).toHaveValue('ops-slack');
    fireEvent.submit(screen.getByLabelText(/Channel Name|채널 이름/i).closest('form')!);
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
    expect(screen.queryByPlaceholderText(/Recipients|수신자/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /^Error Rate$|^에러율$/ })).not.toBeInTheDocument();
  });

  it('edit rule shows legacy recipients read-only and preserves mappings on save', async () => {
    mocks.getRules.mockResolvedValue([ruleFixture]);
    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText('db-down')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^Edit$|^편집$/ }));
    await waitFor(() => expect(screen.getByText(/^Edit Rule$|^규칙 수정$/)).toBeInTheDocument());
    expect(screen.getAllByText(/Legacy recipients|기존 수신자 설정/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText('ops-slack: ops@example.com').length).toBeGreaterThan(0);
    expect(screen.queryByPlaceholderText(/Recipients|수신자/i)).not.toBeInTheDocument();

    fireEvent.submit(screen.getByPlaceholderText(/DB Down Alert/i).closest('form')!);

    await waitFor(() => expect(mocks.updateRule).toHaveBeenCalled());
    expect(mocks.updateRule.mock.calls[0][1]).not.toHaveProperty('channels');
  });

  it('submits upstream alert target using upstream id when a display name exists', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.getUpstreams.mockResolvedValue({
      items: [
        {
          id: 'upstream-1',
          name: 'payments-api',
          type: 'roundrobin',
          nodes: { 'payments:8080': 1 },
        },
      ],
      total: 1,
    });
    mocks.createRule.mockResolvedValue({
      ...ruleFixture,
      id: 99,
      name: 'payments-upstream-down',
      type: 'upstream_health',
      target: 'upstream-1',
    });

    renderWithProviders(<AlertSettings />);
    fireEvent.click(screen.getByRole('button', { name: /^Rules$|^규칙$/ }));
    await waitFor(() => expect(screen.getByText(/No rules|규칙이 없/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /\+\s*Add Rule/i }));
    await waitFor(() => expect(screen.getByText(/^Add Rule$/)).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText(/DB Down Alert/i), 'payments-upstream-down');
    const typeSelect = screen.getAllByRole('combobox')[0];
    await userEvent.selectOptions(typeSelect, 'upstream_health');

    const upstreamOption = await screen.findByRole('option', { name: 'payments-api' });
    const targetSelect = screen.getAllByRole('combobox')[1];
    await userEvent.selectOptions(targetSelect, upstreamOption);

    fireEvent.submit(screen.getByPlaceholderText(/DB Down Alert/i).closest('form')!);

    await waitFor(() => expect(mocks.createRule).toHaveBeenCalled());
    expect(mocks.createRule.mock.calls[0][0].target).toBe('upstream-1');
    expect(mocks.createRule.mock.calls[0][0].channels).toEqual([]);
  });
});
