vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertChannels: vi.fn(),
  createAlertChannel: vi.fn(),
  updateAlertChannel: vi.fn(),
  deleteAlertChannel: vi.fn(),
  testAlertChannel: vi.fn(),
  getAlertSettings: vi.fn(),
  updateAlertSettings: vi.fn(),
  testRecipientDelivery: vi.fn(),
  getAlertResourceOwners: vi.fn(),
  setAlertResourceOwner: vi.fn(),
  deleteAlertResourceOwner: vi.fn(),
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
  getAlertSettings,
  updateAlertSettings,
  testRecipientDelivery,
  getAlertResourceOwners,
  setAlertResourceOwner,
  deleteAlertResourceOwner,
} from '../api/client';
import AlertSettings from '../pages/AlertSettings';
import { renderWithProviders } from './helpers';

const mocks = {
  getChannels: vi.mocked(getAlertChannels),
  createChannel: vi.mocked(createAlertChannel),
  updateChannel: vi.mocked(updateAlertChannel),
  deleteChannel: vi.mocked(deleteAlertChannel),
  testChannel: vi.mocked(testAlertChannel),
  getSettings: vi.mocked(getAlertSettings),
  updateSettings: vi.mocked(updateAlertSettings),
  testRecipientDelivery: vi.mocked(testRecipientDelivery),
  getResourceOwners: vi.mocked(getAlertResourceOwners),
  setResourceOwner: vi.mocked(setAlertResourceOwner),
  deleteResourceOwner: vi.mocked(deleteAlertResourceOwner),
};

const channelFixture = {
  id: 1,
  name: 'ops-slack',
  webhook_url: 'https://hooks.example.com/abc',
  payload_template: '{"text":"{{message}}"}',
  recipient_item_template: null,
  headers: { 'X-Token': 'tok' },
  enabled: true,
};

const settingsFixture = {
  mail_channel_id: null as number | null,
  admin_emails: [] as string[],
  route_error_threshold_pct: 10,
  check_interval_seconds: 60,
  trigger_after_failures: 2,
};

const dbResourceFixture = {
  resource_type: 'db',
  resource_id: 'orders-db',
  display_name: 'orders-db',
  emails: [] as string[],
};

/* ── delivery tab helper: clicks the Delivery tab button ── */
function goToDeliveryTab() {
  fireEvent.click(
    screen.getByRole('button', { name: /^Delivery$|^발송 설정$/ }),
  );
}

describe('AlertSettings page', () => {
  beforeEach(() => {
    Object.values(mocks).forEach((m) => m.mockReset());
    mocks.getSettings.mockResolvedValue({ ...settingsFixture });
    mocks.updateSettings.mockImplementation(async (body) => ({
      ...settingsFixture,
      mail_channel_id: 1,
      ...body,
    }));
    mocks.testRecipientDelivery.mockResolvedValue({ success: true, error: null });
    mocks.getResourceOwners.mockResolvedValue([]);
    mocks.setResourceOwner.mockImplementation(async (type, id, body) => ({
      resource_type: type,
      resource_id: id,
      display_name: id,
      emails: body.emails,
    }));
    mocks.getChannels.mockResolvedValue([]);
  });

  it('renders the two tabs and shows the recipients tab by default', async () => {
    renderWithProviders(<AlertSettings />);
    expect(screen.getByRole('button', { name: /^Assignees \/ Admins$|^담당자 \/ 관리자$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Delivery$|^발송 설정$/ })).toBeInTheDocument();
    // Admins section visible on the default tab
    await waitFor(() => expect(screen.getByText(/^Admins$|^관리자$/)).toBeInTheDocument());
  });

  it('shows empty resources state on the recipients tab', async () => {
    renderWithProviders(<AlertSettings />);
    await waitFor(() =>
      expect(screen.getByText(/No resources available|사용 가능한 리소스가 없/i)).toBeInTheDocument(),
    );
  });

  it('saves admin emails parsed from the textarea', async () => {
    renderWithProviders(<AlertSettings />);
    const adminEmails = await screen.findByLabelText(/Admin emails|관리자 이메일/i);
    // textarea is disabled until settings have loaded; wait before typing
    await waitFor(() => expect(adminEmails).toBeEnabled());
    await userEvent.type(adminEmails, 'ops@example.com, oncall@example.com');

    fireEvent.click(screen.getByRole('button', { name: /^Admin emails saved$|^관리자 이메일 저장$/i }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalled());
    expect(mocks.updateSettings.mock.calls[0][0]).toEqual({
      admin_emails: ['ops@example.com', 'oncall@example.com'],
    });
  });

  it('prefills admin emails from loaded settings', async () => {
    mocks.getSettings.mockResolvedValue({
      ...settingsFixture,
      admin_emails: ['admin@example.com'],
    });
    renderWithProviders(<AlertSettings />);
    const adminEmails = await screen.findByLabelText(/Admin emails|관리자 이메일/i);
    await waitFor(() => expect(adminEmails).toHaveValue('admin@example.com'));
  });

  it('test admins button is disabled without a mail channel and enabled once configured', async () => {
    mocks.getSettings.mockResolvedValue({
      ...settingsFixture,
      mail_channel_id: null,
      admin_emails: ['admin@example.com'],
    });
    renderWithProviders(<AlertSettings />);
    const testButton = await screen.findByRole('button', {
      name: /^Send test to admins$|^관리자에게 테스트 발송$/i,
    });
    expect(testButton).toBeDisabled();
  });

  it('tests admin recipient delivery with the configured mail channel', async () => {
    mocks.getSettings.mockResolvedValue({
      ...settingsFixture,
      mail_channel_id: 5,
      admin_emails: ['admin@example.com'],
    });
    renderWithProviders(<AlertSettings />);
    const testButton = await screen.findByRole('button', {
      name: /^Send test to admins$|^관리자에게 테스트 발송$/i,
    });
    await waitFor(() => expect(testButton).toBeEnabled());
    fireEvent.click(testButton);

    await waitFor(() => expect(mocks.testRecipientDelivery).toHaveBeenCalled());
    expect(mocks.testRecipientDelivery).toHaveBeenCalledWith(5, ['admin@example.com']);
  });

  it('saves resource-owner assignees via setAlertResourceOwner(type, id, { emails })', async () => {
    mocks.getResourceOwners.mockResolvedValue([{ ...dbResourceFixture }]);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('orders-db')).toBeInTheDocument());

    const assigneeInput = screen.getByLabelText(/Assignees - orders-db|담당자 - orders-db/i);
    await userEvent.type(assigneeInput, 'owner@example.com, second@example.com');

    fireEvent.click(screen.getByRole('button', { name: /^Save$|^저장$/ }));

    await waitFor(() => expect(mocks.setResourceOwner).toHaveBeenCalled());
    expect(mocks.setResourceOwner).toHaveBeenCalledWith('db', 'orders-db', {
      emails: ['owner@example.com', 'second@example.com'],
    });
  });

  it('clears assignees when the textarea is emptied', async () => {
    mocks.getResourceOwners.mockResolvedValue([
      { ...dbResourceFixture, emails: ['owner@example.com'] },
    ]);
    renderWithProviders(<AlertSettings />);
    await waitFor(() => expect(screen.getByText('orders-db')).toBeInTheDocument());

    const assigneeInput = screen.getByLabelText(/Assignees - orders-db|담당자 - orders-db/i);
    await waitFor(() => expect(assigneeInput).toHaveValue('owner@example.com'));
    await userEvent.clear(assigneeInput);

    fireEvent.click(screen.getByRole('button', { name: /^Save$|^저장$/ }));

    await waitFor(() => expect(mocks.setResourceOwner).toHaveBeenCalled());
    expect(mocks.setResourceOwner).toHaveBeenCalledWith('db', 'orders-db', { emails: [] });
  });

  it('delivery tab saves selected mail channel via settings form', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    const mailSelect = screen.getByLabelText(/Mail Channel|메일 채널/i);
    await userEvent.selectOptions(mailSelect, '1');
    fireEvent.click(screen.getByRole('button', { name: /^Save Settings$|^설정 저장$/i }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalled());
    expect(mocks.updateSettings.mock.calls[0][0]).toMatchObject({ mail_channel_id: 1 });
  });

  it('delivery tab saves updated trigger_after_failures', async () => {
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    const failuresInput = await screen.findByLabelText(
      /연속 실패 횟수|Consecutive failures/i,
    );
    await waitFor(() => expect(failuresInput).toHaveValue(2));

    await userEvent.clear(failuresInput);
    await userEvent.type(failuresInput, '5');

    fireEvent.click(screen.getByRole('button', { name: /^Save Settings$|^설정 저장$/i }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalled());
    expect(mocks.updateSettings.mock.calls[0][0]).toEqual(
      expect.objectContaining({ trigger_after_failures: 5 }),
    );
  });

  it('delivery tab disables settings save before settings have loaded', async () => {
    mocks.getSettings.mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();

    expect(
      screen.getByRole('button', { name: /^Save Settings$|^설정 저장$/i }),
    ).toBeDisabled();
  });

  it('delivery tab shows empty channels state', async () => {
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() =>
      expect(screen.getByText(/No channels|채널이 없/i)).toBeInTheDocument(),
    );
  });

  it('delivery tab renders channels table with truncated webhook url', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    expect(screen.getByText(/hooks.example.com/)).toBeInTheDocument();
  });

  it('opens add channel modal and submits new channel', async () => {
    mocks.createChannel.mockResolvedValue({ ...channelFixture, id: 99, name: 'new' });
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText(/No channels|채널이 없/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /\+\s*Add Channel|\+\s*채널 추가/i }));
    const dialog = await screen.findByRole('dialog', { name: /^Add Channel$|^채널 추가$/ });
    expect(dialog).toHaveAttribute('aria-modal', 'true');

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
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Edit$|^편집$|^수정$/ }));
    await waitFor(() =>
      expect(screen.getByText(/^Edit Channel$|^채널 수정$/)).toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/Channel Name|채널 이름/i)).toHaveValue('ops-slack');
    fireEvent.submit(screen.getByLabelText(/Channel Name|채널 이름/i).closest('form')!);
    await waitFor(() => expect(mocks.updateChannel).toHaveBeenCalled());
    expect(mocks.updateChannel.mock.calls[0][0]).toBe(1);
  });

  it('delete channel asks for confirmation and skips on cancel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
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
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(mocks.deleteChannel).toHaveBeenCalledWith(1));
    confirmSpy.mockRestore();
  });

  it('test channel success calls testAlertChannel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.testChannel.mockResolvedValue({ success: true, error: null });
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Test Send$|^테스트 발송$/i }));
    await waitFor(() => expect(mocks.testChannel).toHaveBeenCalledWith(1));
  });

  it('test channel failure calls testAlertChannel', async () => {
    mocks.getChannels.mockResolvedValue([channelFixture]);
    mocks.testChannel.mockResolvedValue({ success: false, error: 'timeout' });
    renderWithProviders(<AlertSettings />);
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Test Send$|^테스트 발송$/i }));
    await waitFor(() => expect(mocks.testChannel).toHaveBeenCalled());
  });
});

describe('AlertSettings page (alerts.read only)', () => {
  beforeEach(() => {
    Object.values(mocks).forEach((m) => m.mockReset());
    mocks.getSettings.mockResolvedValue({
      ...settingsFixture,
      mail_channel_id: 1,
      admin_emails: ['admin@example.com'],
    });
    mocks.getResourceOwners.mockResolvedValue([
      { ...dbResourceFixture, emails: ['owner@example.com'] },
    ]);
    mocks.getChannels.mockResolvedValue([channelFixture]);
  });

  it('recipients tab: hides admin save/test buttons and disables inputs for viewer', async () => {
    renderWithProviders(<AlertSettings />, { permissions: ['alerts.read'] });
    await waitFor(() => expect(screen.getByText('orders-db')).toBeInTheDocument());

    expect(
      screen.queryByRole('button', { name: /^Admin emails saved$|^관리자 이메일 저장$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /^Send test to admins$|^관리자에게 테스트 발송$/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Save$|^저장$/ })).not.toBeInTheDocument();

    expect(screen.getByLabelText(/Admin emails|관리자 이메일/i)).toBeDisabled();
    expect(screen.getByLabelText(/Assignees - orders-db|담당자 - orders-db/i)).toBeDisabled();
  });

  it('delivery tab: hides write buttons and disables settings form for viewer', async () => {
    renderWithProviders(<AlertSettings />, { permissions: ['alerts.read'] });
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    expect(
      screen.queryByRole('button', { name: /\+\s*Add Channel|\+\s*채널 추가/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /^Save Settings$|^설정 저장$/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Test Send$|^테스트 발송$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Edit$|^편집$|^수정$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Delete$|^삭제$/ })).not.toBeInTheDocument();

    expect(screen.getByLabelText(/Mail Channel|메일 채널/i)).toBeDisabled();
  });

  it('delivery tab: masks webhook URL path for viewer', async () => {
    mocks.getChannels.mockResolvedValue([
      { ...channelFixture, webhook_url: 'https://hooks.example.com/services/T1/B2/SECRETXYZ' },
    ]);
    renderWithProviders(<AlertSettings />, { permissions: ['alerts.read'] });
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    expect(screen.queryByText(/SECRETXYZ/)).not.toBeInTheDocument();
    expect(screen.queryByText(/services\/T1\/B2/)).not.toBeInTheDocument();
    expect(screen.getByText(/hooks\.example\.com\/\*\*\*/)).toBeInTheDocument();
  });

  it('delivery tab: writer sees full webhook URL and Test Send button', async () => {
    mocks.getChannels.mockResolvedValue([
      { ...channelFixture, webhook_url: 'https://hooks.example.com/services/T1/B2/SECRETXYZ' },
    ]);
    renderWithProviders(<AlertSettings />, { permissions: ['alerts.read', 'alerts.write'] });
    goToDeliveryTab();
    await waitFor(() => expect(screen.getByText('ops-slack')).toBeInTheDocument());

    expect(screen.getByText(/services\/T1\/B2\/SECRETXYZ/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Test Send$|^테스트 발송$/i })).toBeInTheDocument();
  });
});
