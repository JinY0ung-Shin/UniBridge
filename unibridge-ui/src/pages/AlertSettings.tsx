import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getAlertChannels,
  createAlertChannel,
  updateAlertChannel,
  deleteAlertChannel,
  testAlertChannel,
  getAlertRules,
  createAlertRule,
  updateAlertRule,
  deleteAlertRule,
  testAlertRule,
  type AlertChannel,
  type AlertChannelCreate,
  type AlertRule,
  type AlertRuleCreate,
  type RuleChannelMapping,
} from '../api/client';
import { useToast } from '../components/ToastContext';
import './AlertSettings.css';

// ── Channel types ──────────────────────────────────────────────────────────────

type HeaderPair = { key: string; value: string };

const emptyChannelForm = (): AlertChannelCreate & { headerPairs: HeaderPair[] } => ({
  name: '',
  webhook_url: '',
  payload_template: '{"text": "{{message}}"}',
  enabled: true,
  headerPairs: [],
});

// ── Rule types ─────────────────────────────────────────────────────────────────

type RuleType = 'db_health' | 'upstream_health' | 'error_rate' | 'route_error_rate';

interface RuleChannelRow {
  channel_id: number;
  recipients: string; // comma-separated for UI input
}

const emptyRuleForm = () => ({
  name: '',
  type: 'db_health' as RuleType,
  target: '',
  threshold: 5,
  enabled: true,
  channelRows: [] as RuleChannelRow[],
});

// ── Helpers ───────────────────────────────────────────────────────────────────

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
  return str.length > max ? str.slice(0, max) + '…' : str;
}

function ruleTypeLabel(t: (k: string) => string, type: RuleType): string {
  const map: Record<RuleType, string> = {
    db_health: t('alerts.typeDbHealth'),
    upstream_health: t('alerts.typeUpstreamHealth'),
    error_rate: t('alerts.typeErrorRate'),
    route_error_rate: t('alerts.typeRouteErrorRate'),
  };
  return map[type] ?? type;
}

// ── Main component ────────────────────────────────────────────────────────────

function AlertSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [activeTab, setActiveTab] = useState<'channels' | 'rules'>('channels');

  // ── Channels state ────────────────────────────────────────────────────────

  const [showChannelModal, setShowChannelModal] = useState(false);
  const [editingChannelId, setEditingChannelId] = useState<number | null>(null);
  const [channelForm, setChannelForm] = useState(emptyChannelForm());
  const [testingChannelIds, setTestingChannelIds] = useState<Set<number>>(new Set());
  const [testingRuleIds, setTestingRuleIds] = useState<Set<number>>(new Set());

  // ── Rules state ───────────────────────────────────────────────────────────

  const [showRuleModal, setShowRuleModal] = useState(false);
  const [editingRuleId, setEditingRuleId] = useState<number | null>(null);
  const [ruleForm, setRuleForm] = useState(emptyRuleForm());

  // ── Queries ───────────────────────────────────────────────────────────────

  const channelsQuery = useQuery({
    queryKey: ['alert-channels'],
    queryFn: getAlertChannels,
  });

  const rulesQuery = useQuery({
    queryKey: ['alert-rules'],
    queryFn: getAlertRules,
  });

  const channels = channelsQuery.data ?? [];
  const rules = rulesQuery.data ?? [];

  // ── Channel mutations ─────────────────────────────────────────────────────

  const createChannelMutation = useMutation({
    mutationFn: (body: AlertChannelCreate) => createAlertChannel(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-channels'] });
      closeChannelModal();
      addToast({ type: 'success', title: t('alerts.addChannel'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.addChannel'), message: t('common.errorOccurred') });
    },
  });

  const updateChannelMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<AlertChannelCreate> }) =>
      updateAlertChannel(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-channels'] });
      closeChannelModal();
      addToast({ type: 'success', title: t('alerts.editChannel'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.editChannel'), message: t('common.errorOccurred') });
    },
  });

  const deleteChannelMutation = useMutation({
    mutationFn: (id: number) => deleteAlertChannel(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-channels'] });
    },
    onError: () => {
      addToast({ type: 'error', title: t('common.delete'), message: t('common.errorOccurred') });
    },
  });

  // ── Rule mutations ────────────────────────────────────────────────────────

  const createRuleMutation = useMutation({
    mutationFn: (body: AlertRuleCreate) => createAlertRule(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
      closeRuleModal();
      addToast({ type: 'success', title: t('alerts.addRule'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.addRule'), message: t('common.errorOccurred') });
    },
  });

  const updateRuleMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<AlertRuleCreate> }) =>
      updateAlertRule(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
      closeRuleModal();
      addToast({ type: 'success', title: t('alerts.editRule'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.editRule'), message: t('common.errorOccurred') });
    },
  });

  const deleteRuleMutation = useMutation({
    mutationFn: (id: number) => deleteAlertRule(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
    },
    onError: () => {
      addToast({ type: 'error', title: t('common.delete'), message: t('common.errorOccurred') });
    },
  });

  // ── Channel handlers ──────────────────────────────────────────────────────

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

  function handleChannelSubmit(e: React.FormEvent) {
    e.preventDefault();
    const { headerPairs, ...rest } = channelForm;
    const headers = headerPairs.length > 0 ? headerPairsToRecord(headerPairs) : undefined;
    const body: AlertChannelCreate = { ...rest, headers };
    if (editingChannelId !== null) {
      updateChannelMutation.mutate({ id: editingChannelId, body });
    } else {
      createChannelMutation.mutate(body);
    }
  }

  function handleDeleteChannel(ch: AlertChannel) {
    if (window.confirm(t('alerts.deleteConfirm'))) {
      deleteChannelMutation.mutate(ch.id);
    }
  }

  async function handleTestChannel(ch: AlertChannel) {
    setTestingChannelIds((prev) => new Set(prev).add(ch.id));
    try {
      const result = await testAlertChannel(ch.id);
      if (result.success) {
        addToast({ type: 'success', title: `${ch.name} — ${t('alerts.testSuccess')}` });
      } else {
        addToast({
          type: 'error',
          title: `${ch.name} — ${t('alerts.testFailed')}`,
          message: result.error ?? undefined,
        });
      }
    } catch {
      addToast({ type: 'error', title: `${ch.name} — ${t('alerts.testFailed')}` });
    } finally {
      setTestingChannelIds((prev) => {
        const next = new Set(prev);
        next.delete(ch.id);
        return next;
      });
    }
  }

  async function handleTestRule(rule: AlertRule) {
    if (rule.channels.length === 0) {
      addToast({ type: 'error', title: `${rule.name} — ${t('alerts.testRuleNoChannels')}` });
      return;
    }
    setTestingRuleIds((prev) => new Set(prev).add(rule.id));
    try {
      const { results } = await testAlertRule(rule.id);
      const sent = results.filter((r) => !r.skipped);
      const skipped = results.filter((r) => r.skipped);
      const succeeded = sent.filter((r) => r.success === true);
      const failed = sent.filter((r) => r.success === false);

      if (failed.length === 0 && skipped.length === 0) {
        addToast({
          type: 'success',
          title: `${rule.name} — ${t('alerts.testRuleAllOk', { count: succeeded.length })}`,
        });
      } else if (failed.length === 0 && skipped.length > 0) {
        addToast({
          type: 'info',
          title: `${rule.name} — ${t('alerts.testRulePartialSkipped', { sent: succeeded.length, skipped: skipped.length })}`,
          message: skipped.map((r) => `${r.channel_name}: ${r.error ?? ''}`).join(' • '),
        });
      } else {
        addToast({
          type: 'error',
          title: `${rule.name} — ${t('alerts.testRuleSomeFailed', { ok: succeeded.length, total: sent.length })}`,
          message: failed.map((r) => `${r.channel_name}: ${r.error ?? ''}`).join(' • '),
        });
      }
    } catch {
      addToast({ type: 'error', title: `${rule.name} — ${t('alerts.testFailed')}` });
    } finally {
      setTestingRuleIds((prev) => {
        const next = new Set(prev);
        next.delete(rule.id);
        return next;
      });
    }
  }

  function updateChannelField<K extends keyof typeof channelForm>(
    key: K,
    value: (typeof channelForm)[K],
  ) {
    setChannelForm((prev) => ({ ...prev, [key]: value }));
  }

  function addHeaderPair() {
    setChannelForm((prev) => ({
      ...prev,
      headerPairs: [...prev.headerPairs, { key: '', value: '' }],
    }));
  }

  function removeHeaderPair(idx: number) {
    setChannelForm((prev) => ({
      ...prev,
      headerPairs: prev.headerPairs.filter((_, i) => i !== idx),
    }));
  }

  function updateHeaderPair(idx: number, field: 'key' | 'value', val: string) {
    setChannelForm((prev) => ({
      ...prev,
      headerPairs: prev.headerPairs.map((p, i) => (i === idx ? { ...p, [field]: val } : p)),
    }));
  }

  // ── Rule handlers ─────────────────────────────────────────────────────────

  function openCreateRule() {
    setRuleForm(emptyRuleForm());
    setEditingRuleId(null);
    setShowRuleModal(true);
  }

  function openEditRule(rule: AlertRule) {
    setRuleForm({
      name: rule.name,
      type: rule.type,
      target: rule.target,
      threshold: rule.threshold ?? 5,
      enabled: rule.enabled,
      channelRows: rule.channels.map((c) => ({
        channel_id: c.channel_id,
        recipients: c.recipients.join(', '),
      })),
    });
    setEditingRuleId(rule.id);
    setShowRuleModal(true);
  }

  function closeRuleModal() {
    setShowRuleModal(false);
    setEditingRuleId(null);
    setRuleForm(emptyRuleForm());
  }

  function handleRuleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const channelMappings: RuleChannelMapping[] = ruleForm.channelRows
      .filter((r) => r.channel_id > 0)
      .map((r) => ({
        channel_id: r.channel_id,
        recipients: r.recipients
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      }));

    const body: AlertRuleCreate = {
      name: ruleForm.name,
      type: ruleForm.type,
      target: ruleForm.target,
      enabled: ruleForm.enabled,
      channels: channelMappings,
      ...(ruleForm.type === 'error_rate' || ruleForm.type === 'route_error_rate'
        ? { threshold: ruleForm.threshold }
        : {}),
    };

    if (editingRuleId !== null) {
      updateRuleMutation.mutate({ id: editingRuleId, body });
    } else {
      createRuleMutation.mutate(body);
    }
  }

  function handleDeleteRule(rule: AlertRule) {
    if (window.confirm(t('alerts.deleteConfirm'))) {
      deleteRuleMutation.mutate(rule.id);
    }
  }

  function updateRuleField<K extends keyof ReturnType<typeof emptyRuleForm>>(
    key: K,
    value: ReturnType<typeof emptyRuleForm>[K],
  ) {
    setRuleForm((prev) => ({ ...prev, [key]: value }));
  }

  function addChannelRow() {
    setRuleForm((prev) => ({
      ...prev,
      channelRows: [...prev.channelRows, { channel_id: 0, recipients: '' }],
    }));
  }

  function removeChannelRow(idx: number) {
    setRuleForm((prev) => ({
      ...prev,
      channelRows: prev.channelRows.filter((_, i) => i !== idx),
    }));
  }

  function updateChannelRow(
    idx: number,
    field: keyof RuleChannelRow,
    val: string | number,
  ) {
    setRuleForm((prev) => ({
      ...prev,
      channelRows: prev.channelRows.map((r, i) =>
        i === idx ? { ...r, [field]: val } : r,
      ),
    }));
  }

  const isSavingChannel = createChannelMutation.isPending || updateChannelMutation.isPending;
  const isSavingRule = createRuleMutation.isPending || updateRuleMutation.isPending;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="alert-settings">
      <div className="page-header">
        <div>
          <h1>{t('alerts.settingsTitle')}</h1>
          <p className="page-subtitle">{t('alerts.settingsSubtitle')}</p>
        </div>
        {activeTab === 'channels' ? (
          <button className="btn btn-primary" onClick={openCreateChannel}>
            + {t('alerts.addChannel')}
          </button>
        ) : (
          <button className="btn btn-primary" onClick={openCreateRule}>
            + {t('alerts.addRule')}
          </button>
        )}
      </div>

      {/* Tab bar */}
      <div className="alert-tabs">
        <button
          className={`alert-tab${activeTab === 'channels' ? ' alert-tab--active' : ''}`}
          onClick={() => setActiveTab('channels')}
        >
          {t('alerts.channelsTab')}
        </button>
        <button
          className={`alert-tab${activeTab === 'rules' ? ' alert-tab--active' : ''}`}
          onClick={() => setActiveTab('rules')}
        >
          {t('alerts.rulesTab')}
        </button>
      </div>

      {/* ── Channels Tab ───────────────────────────────────────────────────── */}
      {activeTab === 'channels' && (
        <div className="alert-tab-content">
          {channelsQuery.isLoading && (
            <div className="loading-message">{t('common.loading')}</div>
          )}
          {channelsQuery.isError && (
            <div className="error-banner">{t('common.errorOccurred')}</div>
          )}
          {!channelsQuery.isLoading && channels.length === 0 && !channelsQuery.isError && (
            <div className="empty-state">
              <h3>{t('alerts.noChannels')}</h3>
            </div>
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
                            {testingChannelIds.has(ch.id)
                              ? t('common.loading')
                              : t('alerts.testChannel')}
                          </button>
                          <button
                            className="btn btn-sm btn-secondary"
                            onClick={() => openEditChannel(ch)}
                          >
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
        </div>
      )}

      {/* ── Rules Tab ─────────────────────────────────────────────────────── */}
      {activeTab === 'rules' && (
        <div className="alert-tab-content">
          {rulesQuery.isLoading && (
            <div className="loading-message">{t('common.loading')}</div>
          )}
          {rulesQuery.isError && (
            <div className="error-banner">{t('common.errorOccurred')}</div>
          )}
          {!rulesQuery.isLoading && rules.length === 0 && !rulesQuery.isError && (
            <div className="empty-state">
              <h3>{t('alerts.noRules')}</h3>
            </div>
          )}
          {rules.length > 0 && (
            <div className="table-container">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('common.name')}</th>
                    <th>{t('alerts.ruleType')}</th>
                    <th>{t('alerts.ruleTarget')}</th>
                    <th>{t('alerts.threshold')}</th>
                    <th>{t('alerts.enabled')}</th>
                    <th>{t('alerts.channels')}</th>
                    <th>{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rules.map((rule) => (
                    <tr key={rule.id}>
                      <td className="cell-alias">{rule.name}</td>
                      <td>
                        <span className={`rule-type-badge rule-type-badge--${rule.type}`}>
                          {ruleTypeLabel(t, rule.type)}
                        </span>
                      </td>
                      <td className="cell-target">{rule.target || '*'}</td>
                      <td>{rule.threshold !== null ? `${rule.threshold}%` : '—'}</td>
                      <td>
                        <span
                          className={`badge ${rule.enabled ? 'badge-ok' : 'badge-unknown'}`}
                        >
                          {rule.enabled ? t('common.active') : t('common.disabled')}
                        </span>
                      </td>
                      <td>
                        <div className="channel-summary">
                          {rule.channels.length === 0 ? (
                            <span className="text-muted">—</span>
                          ) : (
                            rule.channels.map((c) => (
                              <span key={c.channel_id} className="channel-chip">
                                {c.channel_name}
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                      <td>
                        <div className="action-buttons">
                          <button
                            className="btn btn-sm btn-outline"
                            onClick={() => handleTestRule(rule)}
                            disabled={testingRuleIds.has(rule.id)}
                          >
                            {testingRuleIds.has(rule.id)
                              ? t('common.loading')
                              : t('alerts.testRule')}
                          </button>
                          <button
                            className="btn btn-sm btn-secondary"
                            onClick={() => openEditRule(rule)}
                          >
                            {t('common.edit')}
                          </button>
                          <button
                            className="btn btn-sm btn-danger"
                            onClick={() => handleDeleteRule(rule)}
                            disabled={deleteRuleMutation.isPending}
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
        </div>
      )}

      {/* ── Channel Modal ──────────────────────────────────────────────────── */}
      {showChannelModal && (
        <div className="modal-overlay" onClick={closeChannelModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>
                {editingChannelId !== null ? t('alerts.editChannel') : t('alerts.addChannel')}
              </h2>
              <button className="modal-close" onClick={closeChannelModal}>
                &times;
              </button>
            </div>
            <form onSubmit={handleChannelSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>{t('alerts.channelName')}</label>
                  <input
                    type="text"
                    value={channelForm.name}
                    onChange={(e) => updateChannelField('name', e.target.value)}
                    required
                    placeholder="e.g., Slack Ops"
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>{t('alerts.webhookUrl')}</label>
                  <input
                    type="url"
                    value={channelForm.webhook_url}
                    onChange={(e) => updateChannelField('webhook_url', e.target.value)}
                    required
                    placeholder="https://hooks.slack.com/services/..."
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>{t('alerts.payloadTemplate')}</label>
                  <textarea
                    className="form-textarea"
                    rows={5}
                    value={channelForm.payload_template}
                    onChange={(e) => updateChannelField('payload_template', e.target.value)}
                    required
                  />
                  {(() => {
                    const templateVars: [string, string][] = [
                      ['alert_type', t('alerts.varDesc_alert_type')],
                      ['target_name', t('alerts.varDesc_target_name')],
                      ['status', t('alerts.varDesc_status')],
                      ['message', t('alerts.varDesc_message')],
                      ['timestamp', t('alerts.varDesc_timestamp')],
                      ['recipients', t('alerts.varDesc_recipients')],
                      ['rate', t('alerts.varDesc_rate')],
                      ['threshold', t('alerts.varDesc_threshold')],
                      ['rule_name', t('alerts.varDesc_rule_name')],
                    ];
                    return (
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
                    );
                  })()}
                </div>

                {/* Headers */}
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
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => removeHeaderPair(idx)}
                      >
                        &times;
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="btn btn-sm btn-outline"
                    onClick={addHeaderPair}
                  >
                    + {t('alerts.addHeader')}
                  </button>
                </div>

                {/* Enabled */}
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
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={closeChannelModal}
                >
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

      {/* ── Rule Modal ─────────────────────────────────────────────────────── */}
      {showRuleModal && (
        <div className="modal-overlay" onClick={closeRuleModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>
                {editingRuleId !== null ? t('alerts.editRule') : t('alerts.addRule')}
              </h2>
              <button className="modal-close" onClick={closeRuleModal}>
                &times;
              </button>
            </div>
            <form onSubmit={handleRuleSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>{t('alerts.ruleName')}</label>
                  <input
                    type="text"
                    value={ruleForm.name}
                    onChange={(e) => updateRuleField('name', e.target.value)}
                    required
                    placeholder="e.g., DB Down Alert"
                  />
                </div>
                <div className="form-group">
                  <label>{t('alerts.ruleType')}</label>
                  <select
                    value={ruleForm.type}
                    onChange={(e) => updateRuleField('type', e.target.value as RuleType)}
                  >
                    <option value="db_health">{t('alerts.typeDbHealth')}</option>
                    <option value="upstream_health">{t('alerts.typeUpstreamHealth')}</option>
                    <option value="error_rate">{t('alerts.typeErrorRate')}</option>
                    <option value="route_error_rate">{t('alerts.typeRouteErrorRate')}</option>
                  </select>
                </div>
                <div className="form-group">
                  <label>{t('alerts.ruleTarget')}</label>
                  <input
                    type="text"
                    value={ruleForm.target}
                    onChange={(e) => updateRuleField('target', e.target.value)}
                    placeholder={t('alerts.targetAll')}
                  />
                </div>
                {(ruleForm.type === 'error_rate' || ruleForm.type === 'route_error_rate') && (
                  <div className="form-group">
                    <label>{t('alerts.threshold')}</label>
                    <input
                      type="number"
                      value={ruleForm.threshold}
                      onChange={(e) => updateRuleField('threshold', Number(e.target.value))}
                      min={0}
                      max={100}
                      step={0.1}
                    />
                  </div>
                )}
                <div className="form-group form-group--full">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={ruleForm.enabled}
                      onChange={(e) => updateRuleField('enabled', e.target.checked)}
                    />
                    {t('alerts.enabled')}
                  </label>
                </div>

                {/* Channel mappings */}
                <div className="form-group form-group--full">
                  <label>{t('alerts.channels')}</label>
                  {ruleForm.channelRows.map((row, idx) => (
                    <div key={idx} className="channel-mapping-row">
                      <select
                        value={row.channel_id}
                        onChange={(e) =>
                          updateChannelRow(idx, 'channel_id', Number(e.target.value))
                        }
                      >
                        <option value={0}>— {t('alerts.channels')} —</option>
                        {channels
                          .filter((ch) => ch.id === row.channel_id || !ruleForm.channelRows.some((r, i) => i !== idx && r.channel_id === ch.id))
                          .map((ch) => (
                          <option key={ch.id} value={ch.id}>
                            {ch.name}
                          </option>
                        ))}
                      </select>
                      <input
                        type="text"
                        className="recipients-input"
                        placeholder={t('alerts.recipients') + ' (comma-separated)'}
                        value={row.recipients}
                        onChange={(e) =>
                          updateChannelRow(idx, 'recipients', e.target.value)
                        }
                      />
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => removeChannelRow(idx)}
                      >
                        &times;
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="btn btn-sm btn-outline"
                    onClick={addChannelRow}
                  >
                    + {t('alerts.channels')}
                  </button>
                </div>
              </div>

              <div className="modal-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={closeRuleModal}
                >
                  {t('alerts.cancel')}
                </button>
                <button type="submit" className="btn btn-primary" disabled={isSavingRule}>
                  {isSavingRule ? t('common.saving') : t('alerts.save')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default AlertSettings;
