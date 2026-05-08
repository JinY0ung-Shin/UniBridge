import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  createAlertChannel,
  deleteAlertChannel,
  getAlertChannels,
  getAlertOwnerGroups,
  getAlertSettings,
  testAlertChannel,
  updateAlertChannel,
  updateAlertSettings,
  type AlertChannel,
  type AlertChannelCreate,
  type AlertSettings,
} from '../../api/client';
import { useToast } from '../../components/useToast';

type HeaderPair = { key: string; value: string };

const defaultSettings: AlertSettings = {
  mail_channel_id: null,
  fallback_owner_group_id: null,
  route_error_threshold_pct: 10,
  check_interval_seconds: 60,
};

const emptyChannelForm = (): AlertChannelCreate & { headerPairs: HeaderPair[] } => ({
  name: '',
  webhook_url: '',
  payload_template: '{"text": "{{message}}"}',
  recipient_item_template: '',
  enabled: true,
  headerPairs: [],
});

function headerPairsToRecord(pairs: HeaderPair[]): Record<string, string> {
  const rec: Record<string, string> = {};
  for (const { key, value } of pairs) {
    if (key.trim()) rec[key.trim()] = value;
  }
  return rec;
}

function recordToHeaderPairs(rec: Record<string, string> | null): HeaderPair[] {
  if (!rec) return [];
  return Object.entries(rec).map(([key, value]) => ({ key, value }));
}

function truncate(str: string, max = 60): string {
  return str.length > max ? str.slice(0, max) + '...' : str;
}

export default function AlertMailChannelPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [showChannelModal, setShowChannelModal] = useState(false);
  const [editingChannelId, setEditingChannelId] = useState<number | null>(null);
  const [channelForm, setChannelForm] = useState(emptyChannelForm());
  const [settingsDraft, setSettingsDraft] = useState<Partial<AlertSettings>>({});
  const [testingChannelIds, setTestingChannelIds] = useState<Set<number>>(new Set());

  const settingsQuery = useQuery({ queryKey: ['alert-settings'], queryFn: getAlertSettings });
  const channelsQuery = useQuery({ queryKey: ['alert-channels'], queryFn: getAlertChannels });
  const ownerGroupsQuery = useQuery({ queryKey: ['alert-owner-groups'], queryFn: getAlertOwnerGroups });

  const channels = channelsQuery.data ?? [];
  const ownerGroups = ownerGroupsQuery.data ?? [];
  const settingsForm: AlertSettings = { ...defaultSettings, ...settingsQuery.data, ...settingsDraft };

  const updateSettingsMutation = useMutation({
    mutationFn: (body: Partial<AlertSettings>) => updateAlertSettings(body),
    onSuccess: (settings) => {
      queryClient.setQueryData(['alert-settings'], settings);
      setSettingsDraft({});
      addToast({ type: 'success', title: t('alerts.saveSettings'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.saveSettings'), message: t('common.errorOccurred') });
    },
  });

  const createChannelMutation = useMutation({
    mutationFn: (body: AlertChannelCreate) => createAlertChannel(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-channels'] });
      closeChannelModal();
      addToast({ type: 'success', title: t('alerts.addChannel'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.addChannel'), message: t('common.errorOccurred') }),
  });

  const updateChannelMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<AlertChannelCreate> }) =>
      updateAlertChannel(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-channels'] });
      closeChannelModal();
      addToast({ type: 'success', title: t('alerts.editChannel'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.editChannel'), message: t('common.errorOccurred') }),
  });

  const deleteChannelMutation = useMutation({
    mutationFn: (id: number) => deleteAlertChannel(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alert-channels'] }),
    onError: () => addToast({ type: 'error', title: t('common.delete'), message: t('common.errorOccurred') }),
  });

  function openCreateChannel() {
    setChannelForm(emptyChannelForm());
    setEditingChannelId(null);
    setShowChannelModal(true);
  }

  function openEditChannel(ch: AlertChannel) {
    setChannelForm({
      name: ch.name,
      webhook_url: ch.webhook_url,
      payload_template: ch.payload_template,
      recipient_item_template: ch.recipient_item_template ?? '',
      enabled: ch.enabled,
      headerPairs: recordToHeaderPairs(ch.headers),
    });
    setEditingChannelId(ch.id);
    setShowChannelModal(true);
  }

  function closeChannelModal() {
    setShowChannelModal(false);
    setEditingChannelId(null);
    setChannelForm(emptyChannelForm());
  }

  function updateChannelField<K extends keyof typeof channelForm>(key: K, value: (typeof channelForm)[K]) {
    setChannelForm((prev) => ({ ...prev, [key]: value }));
  }

  function addHeaderPair() {
    setChannelForm((prev) => ({ ...prev, headerPairs: [...prev.headerPairs, { key: '', value: '' }] }));
  }

  function removeHeaderPair(idx: number) {
    setChannelForm((prev) => ({ ...prev, headerPairs: prev.headerPairs.filter((_, i) => i !== idx) }));
  }

  function updateHeaderPair(idx: number, field: 'key' | 'value', val: string) {
    setChannelForm((prev) => ({
      ...prev,
      headerPairs: prev.headerPairs.map((p, i) => (i === idx ? { ...p, [field]: val } : p)),
    }));
  }

  function handleChannelSubmit(e: React.FormEvent) {
    e.preventDefault();
    const { headerPairs, ...rest } = channelForm;
    const headers = headerPairs.length > 0 ? headerPairsToRecord(headerPairs) : undefined;
    const body: AlertChannelCreate = {
      ...rest,
      recipient_item_template: rest.recipient_item_template?.trim() || null,
      headers,
    };
    if (editingChannelId !== null) updateChannelMutation.mutate({ id: editingChannelId, body });
    else createChannelMutation.mutate(body);
  }

  function handleDeleteChannel(ch: AlertChannel) {
    if (window.confirm(t('alerts.deleteConfirm'))) deleteChannelMutation.mutate(ch.id);
  }

  async function handleTestChannel(ch: AlertChannel) {
    setTestingChannelIds((prev) => new Set(prev).add(ch.id));
    try {
      const result = await testAlertChannel(ch.id);
      addToast({
        type: result.success ? 'success' : 'error',
        title: `${ch.name} - ${result.success ? t('alerts.testSuccess') : t('alerts.testFailed')}`,
        message: result.error ?? undefined,
      });
    } catch {
      addToast({ type: 'error', title: `${ch.name} - ${t('alerts.testFailed')}` });
    } finally {
      setTestingChannelIds((prev) => {
        const next = new Set(prev);
        next.delete(ch.id);
        return next;
      });
    }
  }

  function handleSettingsSubmit(e: React.FormEvent) {
    e.preventDefault();
    updateSettingsMutation.mutate({
      mail_channel_id: settingsForm.mail_channel_id,
      fallback_owner_group_id: settingsForm.fallback_owner_group_id,
      route_error_threshold_pct: settingsForm.route_error_threshold_pct,
      check_interval_seconds: settingsForm.check_interval_seconds,
    });
  }

  const isSavingChannel = createChannelMutation.isPending || updateChannelMutation.isPending;
  const hasSettings = Boolean(settingsQuery.data);
  const isLoading = channelsQuery.isLoading || settingsQuery.isLoading || ownerGroupsQuery.isLoading;
  const isError = channelsQuery.isError || settingsQuery.isError || ownerGroupsQuery.isError;
  const templateVars: [string, string][] = [
    ['alert_type', t('alerts.varDesc_alert_type')],
    ['target_name', t('alerts.varDesc_target_name')],
    ['status', t('alerts.varDesc_status')],
    ['message', t('alerts.varDesc_message')],
    ['timestamp', t('alerts.varDesc_timestamp')],
    ['recipients', t('alerts.varDesc_recipients')],
    ['recipients_json', t('alerts.varDesc_recipients_json')],
    ['rate', t('alerts.varDesc_rate')],
    ['threshold', t('alerts.varDesc_threshold')],
    ['rule_name', t('alerts.varDesc_rule_name')],
  ];

  return (
    <div className="alert-tab-content">
      <form className="settings-panel" onSubmit={handleSettingsSubmit}>
        <div className="settings-grid">
          <div className="form-group">
            <label htmlFor="mail-channel-select">{t('alerts.mailChannel')}</label>
            <select
              id="mail-channel-select"
              value={settingsForm.mail_channel_id ?? ''}
              disabled={!hasSettings}
              onChange={(e) =>
                setSettingsDraft((prev) => ({
                  ...prev,
                  mail_channel_id: e.target.value ? Number(e.target.value) : null,
                }))
              }
            >
              <option value="">{t('alerts.unassigned')}</option>
              {channels.map((ch) => (
                <option key={ch.id} value={ch.id}>{t('alerts.channelOption', { name: ch.name })}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label htmlFor="fallback-owner-group-select">{t('alerts.fallbackOwnerGroup')}</label>
            <select
              id="fallback-owner-group-select"
              value={settingsForm.fallback_owner_group_id ?? ''}
              disabled={!hasSettings}
              onChange={(e) =>
                setSettingsDraft((prev) => ({
                  ...prev,
                  fallback_owner_group_id: e.target.value ? Number(e.target.value) : null,
                }))
              }
            >
              <option value="">{t('alerts.unassigned')}</option>
              {ownerGroups.map((group) => (
                <option key={group.id} value={group.id}>{group.name}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label htmlFor="route-threshold">{t('alerts.routeErrorThreshold')}</label>
            <input
              id="route-threshold"
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={settingsForm.route_error_threshold_pct}
              disabled={!hasSettings}
              onChange={(e) =>
                setSettingsDraft((prev) => ({ ...prev, route_error_threshold_pct: Number(e.target.value) }))
              }
            />
          </div>
          <div className="form-group">
            <label htmlFor="check-interval">{t('alerts.checkInterval')}</label>
            <input
              id="check-interval"
              type="number"
              min={30}
              max={3600}
              value={settingsForm.check_interval_seconds}
              disabled={!hasSettings}
              onChange={(e) =>
                setSettingsDraft((prev) => ({ ...prev, check_interval_seconds: Number(e.target.value) }))
              }
            />
          </div>
        </div>
        <button type="submit" className="btn btn-primary" disabled={!hasSettings || updateSettingsMutation.isPending}>
          {updateSettingsMutation.isPending ? t('common.saving') : t('alerts.saveSettings')}
        </button>
      </form>

      <div className="section-header">
        <h2>{t('alerts.channels')}</h2>
        <button className="btn btn-primary" onClick={openCreateChannel}>+ {t('alerts.addChannel')}</button>
      </div>

      {isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {isError && <div className="error-banner">{t('common.errorOccurred')}</div>}
      {!isLoading && channels.length === 0 && !isError && (
        <div className="empty-state"><h3>{t('alerts.noChannels')}</h3></div>
      )}
      {channels.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('alerts.webhookUrl')}</th>
                <th>{t('alerts.enabled')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {channels.map((ch) => (
                <tr key={ch.id}>
                  <td className="cell-alias">{ch.name}</td>
                  <td className="cell-webhook-url">{truncate(ch.webhook_url)}</td>
                  <td>
                    <span className={`badge ${ch.enabled ? 'badge-ok' : 'badge-unknown'}`}>
                      {ch.enabled ? t('common.active') : t('common.disabled')}
                    </span>
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-outline"
                        onClick={() => handleTestChannel(ch)}
                        disabled={testingChannelIds.has(ch.id)}
                      >
                        {testingChannelIds.has(ch.id) ? t('common.loading') : t('alerts.testChannel')}
                      </button>
                      <button className="btn btn-sm btn-secondary" onClick={() => openEditChannel(ch)}>
                        {t('common.edit')}
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => handleDeleteChannel(ch)}
                        disabled={deleteChannelMutation.isPending}
                      >
                        {t('common.delete')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showChannelModal && (
        <div className="modal-overlay" onClick={closeChannelModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingChannelId !== null ? t('alerts.editChannel') : t('alerts.addChannel')}</h2>
              <button className="modal-close" onClick={closeChannelModal}>&times;</button>
            </div>
            <form onSubmit={handleChannelSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label htmlFor="alert-channel-name">{t('alerts.channelName')}</label>
                  <input
                    id="alert-channel-name"
                    type="text"
                    value={channelForm.name}
                    onChange={(e) => updateChannelField('name', e.target.value)}
                    required
                    placeholder="e.g., Slack Ops"
                  />
                </div>
                <div className="form-group form-group--full">
                  <label htmlFor="alert-channel-webhook">{t('alerts.webhookUrl')}</label>
                  <input
                    id="alert-channel-webhook"
                    type="url"
                    value={channelForm.webhook_url}
                    onChange={(e) => updateChannelField('webhook_url', e.target.value)}
                    required
                    placeholder="https://hooks.slack.com/services/..."
                  />
                </div>
                <div className="form-group form-group--full">
                  <label htmlFor="alert-channel-payload-template">{t('alerts.payloadTemplate')}</label>
                  <textarea
                    id="alert-channel-payload-template"
                    className="form-textarea"
                    rows={5}
                    value={channelForm.payload_template}
                    onChange={(e) => updateChannelField('payload_template', e.target.value)}
                    required
                  />
                  <details className="template-vars">
                    <summary className="form-hint template-vars-toggle">
                      {t('alerts.templateVarsToggle', { count: templateVars.length })}
                    </summary>
                    <p className="form-hint">{t('alerts.templateHelp')}</p>
                    <dl className="template-vars-list">
                      {templateVars.map(([name, desc]) => (
                        <div key={name} className="template-var-row">
                          <dt><code>{`{{${name}}}`}</code></dt>
                          <dd>{desc}</dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                </div>
                <div className="form-group form-group--full">
                  <label htmlFor="alert-channel-recipient-item-template">{t('alerts.recipientItemTemplate')}</label>
                  <textarea
                    id="alert-channel-recipient-item-template"
                    className="form-textarea"
                    rows={3}
                    value={channelForm.recipient_item_template ?? ''}
                    onChange={(e) => updateChannelField('recipient_item_template', e.target.value)}
                    placeholder='{"email":"{{email}}"}'
                  />
                  <p className="form-hint">{t('alerts.recipientItemTemplateHelp')}</p>
                </div>
                <div className="form-group form-group--full">
                  <label>{t('alerts.headers')}</label>
                  {channelForm.headerPairs.map((pair, idx) => (
                    <div key={idx} className="header-row">
                      <input
                        type="text"
                        className="header-key"
                        placeholder={t('alerts.headerName')}
                        value={pair.key}
                        onChange={(e) => updateHeaderPair(idx, 'key', e.target.value)}
                      />
                      <input
                        type="text"
                        className="header-value"
                        placeholder={t('alerts.headerValue')}
                        value={pair.value}
                        onChange={(e) => updateHeaderPair(idx, 'value', e.target.value)}
                      />
                      <button type="button" className="btn btn-sm btn-danger" onClick={() => removeHeaderPair(idx)}>
                        &times;
                      </button>
                    </div>
                  ))}
                  <button type="button" className="btn btn-sm btn-outline" onClick={addHeaderPair}>
                    + {t('alerts.addHeader')}
                  </button>
                </div>
                <div className="form-group form-group--full">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={channelForm.enabled}
                      onChange={(e) => updateChannelField('enabled', e.target.checked)}
                    />
                    {t('alerts.enabled')}
                  </label>
                </div>
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeChannelModal}>
                  {t('alerts.cancel')}
                </button>
                <button type="submit" className="btn btn-primary" disabled={isSavingChannel}>
                  {isSavingChannel ? t('common.saving') : t('alerts.save')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
