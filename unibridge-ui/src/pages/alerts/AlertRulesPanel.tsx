import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  createAlertRule,
  deleteAlertRule,
  getAdminDatabases,
  getAlertRules,
  getGatewayRoutes,
  getGatewayUpstreams,
  testAlertRule,
  updateAlertRule,
  type AlertRule,
  type AlertRuleCreate,
  type RuleChannelDetail,
} from '../../api/client';
import { useToast } from '../../components/useToast';

type RuleType = 'db_health' | 'upstream_health' | 'error_rate' | 'route_error_rate';

const emptyRuleForm = () => ({
  name: '',
  type: 'db_health' as RuleType,
  target: '',
  threshold: 5,
  enabled: true,
});

function ruleTypeLabel(t: (k: string) => string, type: RuleType): string {
  const map: Record<RuleType, string> = {
    db_health: t('alerts.typeDbHealth'),
    upstream_health: t('alerts.typeUpstreamHealth'),
    error_rate: t('alerts.typeErrorRate'),
    route_error_rate: t('alerts.typeRouteErrorRate'),
  };
  return map[type] ?? type;
}

export default function AlertRulesPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [showRuleModal, setShowRuleModal] = useState(false);
  const [editingRuleId, setEditingRuleId] = useState<number | null>(null);
  const [ruleForm, setRuleForm] = useState(emptyRuleForm());
  const [testingRuleIds, setTestingRuleIds] = useState<Set<number>>(new Set());

  const rulesQuery = useQuery({ queryKey: ['alert-rules'], queryFn: getAlertRules });
  const databasesQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
    enabled: showRuleModal,
  });
  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
    enabled: showRuleModal,
  });
  const gatewayRoutesQuery = useQuery({
    queryKey: ['gateway-routes'],
    queryFn: getGatewayRoutes,
    enabled: showRuleModal,
  });

  const rules = rulesQuery.data ?? [];
  const editingRule = editingRuleId !== null ? rules.find((rule) => rule.id === editingRuleId) : null;

  const createRuleMutation = useMutation({
    mutationFn: (body: AlertRuleCreate) => createAlertRule(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
      closeRuleModal();
      addToast({ type: 'success', title: t('alerts.addRule'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.addRule'), message: t('common.errorOccurred') }),
  });

  const updateRuleMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<AlertRuleCreate> }) =>
      updateAlertRule(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] });
      closeRuleModal();
      addToast({ type: 'success', title: t('alerts.editRule'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.editRule'), message: t('common.errorOccurred') }),
  });

  const deleteRuleMutation = useMutation({
    mutationFn: (id: number) => deleteAlertRule(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: () => addToast({ type: 'error', title: t('common.delete'), message: t('common.errorOccurred') }),
  });

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
    const bodyBase: Omit<AlertRuleCreate, 'channels'> = {
      name: ruleForm.name,
      type: ruleForm.type,
      target: ruleForm.target,
      enabled: ruleForm.enabled,
      ...(ruleForm.type === 'error_rate' || ruleForm.type === 'route_error_rate'
        ? { threshold: ruleForm.threshold }
        : {}),
    };
    if (editingRuleId !== null) updateRuleMutation.mutate({ id: editingRuleId, body: bodyBase });
    else createRuleMutation.mutate({ ...bodyBase, channels: [] });
  }

  function handleDeleteRule(rule: AlertRule) {
    if (window.confirm(t('alerts.deleteConfirm'))) deleteRuleMutation.mutate(rule.id);
  }

  async function handleTestRule(rule: AlertRule) {
    if (rule.channels.length === 0) {
      addToast({ type: 'error', title: `${rule.name} - ${t('alerts.testRuleNoChannels')}` });
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
        addToast({ type: 'success', title: `${rule.name} - ${t('alerts.testRuleAllOk', { count: succeeded.length })}` });
      } else if (failed.length === 0 && skipped.length > 0) {
        addToast({
          type: 'info',
          title: `${rule.name} - ${t('alerts.testRulePartialSkipped', { sent: succeeded.length, skipped: skipped.length })}`,
          message: skipped.map((r) => `${r.channel_name}: ${r.error ?? ''}`).join(' • '),
        });
      } else {
        addToast({
          type: 'error',
          title: `${rule.name} - ${t('alerts.testRuleSomeFailed', { ok: succeeded.length, total: sent.length })}`,
          message: failed.map((r) => `${r.channel_name}: ${r.error ?? ''}`).join(' • '),
        });
      }
    } catch {
      addToast({ type: 'error', title: `${rule.name} - ${t('alerts.testFailed')}` });
    } finally {
      setTestingRuleIds((prev) => {
        const next = new Set(prev);
        next.delete(rule.id);
        return next;
      });
    }
  }

  function updateRuleField<K extends keyof ReturnType<typeof emptyRuleForm>>(
    key: K,
    value: ReturnType<typeof emptyRuleForm>[K],
  ) {
    setRuleForm((prev) => ({ ...prev, [key]: value }));
  }

  function handleRuleTypeChange(nextType: RuleType) {
    setRuleForm((prev) => ({
      ...prev,
      type: nextType,
      target: nextType === 'error_rate' ? '*' : '',
    }));
  }

  const isSavingRule = createRuleMutation.isPending || updateRuleMutation.isPending;
  const showLegacyErrorRateOption = editingRule?.type === 'error_rate' || ruleForm.type === 'error_rate';
  const hasOwnerRoutedLegacyRule = rules.some(
    (rule) =>
      (rule.type === 'db_health' || rule.type === 'upstream_health' || rule.type === 'route_error_rate') &&
      rule.channels.length > 0,
  );
  const legacyRecipientsLabel = (mapping: RuleChannelDetail) =>
    `${mapping.channel_name}: ${mapping.recipients.length > 0 ? mapping.recipients.join(', ') : '-'}`;

  return (
    <div className="alert-tab-content">
      <div className="section-header">
        <h2>{t('alerts.rulesTab')}</h2>
        <button className="btn btn-primary" onClick={openCreateRule}>+ {t('alerts.addRule')}</button>
      </div>
      {rulesQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {rulesQuery.isError && <div className="error-banner">{t('common.errorOccurred')}</div>}
      {hasOwnerRoutedLegacyRule && (
        <div className="alert-note alert-note--warning">
          {t('alerts.ownerRoutedLegacyRuleWarning')}
        </div>
      )}
      {!rulesQuery.isLoading && rules.length === 0 && !rulesQuery.isError && (
        <div className="empty-state"><h3>{t('alerts.noRules')}</h3></div>
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
                <th>{t('alerts.legacyRecipients')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.id}>
                  <td className="cell-alias">{rule.name}</td>
                  <td><span className={`rule-type-badge rule-type-badge--${rule.type}`}>{ruleTypeLabel(t, rule.type)}</span></td>
                  <td className="cell-target">{rule.target || '*'}</td>
                  <td>{rule.threshold !== null ? `${rule.threshold}%` : '-'}</td>
                  <td>
                    <span className={`badge ${rule.enabled ? 'badge-ok' : 'badge-unknown'}`}>
                      {rule.enabled ? t('common.active') : t('common.disabled')}
                    </span>
                  </td>
                  <td>
                    <div className="channel-summary">
                      {rule.channels.length === 0 ? (
                        <span className="text-muted">-</span>
                      ) : (
                        rule.channels.map((c) => (
                          <span key={c.channel_id} className="channel-chip" title={t('alerts.legacyRecipientsHint')}>
                            {legacyRecipientsLabel(c)}
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
                        {testingRuleIds.has(rule.id) ? t('common.loading') : t('alerts.testRule')}
                      </button>
                      <button className="btn btn-sm btn-secondary" onClick={() => openEditRule(rule)}>
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

      {showRuleModal && (
        <div className="modal-overlay" onClick={closeRuleModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingRuleId !== null ? t('alerts.editRule') : t('alerts.addRule')}</h2>
              <button className="modal-close" onClick={closeRuleModal}>&times;</button>
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
                  <select value={ruleForm.type} onChange={(e) => handleRuleTypeChange(e.target.value as RuleType)}>
                    <option value="db_health">{t('alerts.typeDbHealth')}</option>
                    <option value="upstream_health">{t('alerts.typeUpstreamHealth')}</option>
                    {showLegacyErrorRateOption && (
                      <option value="error_rate">{t('alerts.typeErrorRate')}</option>
                    )}
                    <option value="route_error_rate">{t('alerts.typeRouteErrorRate')}</option>
                  </select>
                </div>
                <div className="form-group">
                  <label>{t('alerts.ruleTarget')}</label>
                  {ruleForm.type === 'error_rate' ? (
                    <input type="text" value="*" disabled title={t('alerts.targetGlobalHint')} />
                  ) : ruleForm.type === 'db_health' ? (
                    <select
                      value={ruleForm.target}
                      onChange={(e) => updateRuleField('target', e.target.value)}
                      required
                    >
                      <option value="">- {t('alerts.selectTargetDb')} -</option>
                      {ruleForm.target && !(databasesQuery.data ?? []).some((db) => db.alias === ruleForm.target) && (
                        <option value={ruleForm.target}>{ruleForm.target} (missing)</option>
                      )}
                      {(databasesQuery.data ?? []).map((db) => (
                        <option key={db.alias} value={db.alias}>{db.alias}</option>
                      ))}
                    </select>
                  ) : ruleForm.type === 'upstream_health' ? (
                    <select
                      value={ruleForm.target}
                      onChange={(e) => updateRuleField('target', e.target.value)}
                      required
                    >
                      <option value="">- {t('alerts.selectTargetUpstream')} -</option>
                      {ruleForm.target && !(upstreamsQuery.data?.items ?? []).some((up) => up.id === ruleForm.target) && (
                        <option value={ruleForm.target}>{ruleForm.target} (missing)</option>
                      )}
                      {(upstreamsQuery.data?.items ?? []).map((up) => (
                        <option key={up.id} value={up.id}>{up.name ?? up.id}</option>
                      ))}
                    </select>
                  ) : (
                    <select
                      value={ruleForm.target}
                      onChange={(e) => updateRuleField('target', e.target.value)}
                      required
                    >
                      <option value="">- {t('alerts.selectTargetRoute')} -</option>
                      {ruleForm.target && !(gatewayRoutesQuery.data?.items ?? []).some((rt) => rt.id === ruleForm.target) && (
                        <option value={ruleForm.target}>{ruleForm.target} (missing)</option>
                      )}
                      {(gatewayRoutesQuery.data?.items ?? []).map((rt) => (
                        <option key={rt.id} value={rt.id}>{rt.name ? `${rt.name} (${rt.id})` : rt.id}</option>
                      ))}
                    </select>
                  )}
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
                {editingRule && editingRule.channels.length > 0 && (
                  <div className="form-group form-group--full">
                    <label>{t('alerts.legacyRecipients')}</label>
                    <p className="form-hint">{t('alerts.legacyRecipientsHint')}</p>
                    <div className="channel-summary">
                      {editingRule.channels.map((c) => (
                        <span key={c.channel_id} className="channel-chip">{legacyRecipientsLabel(c)}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeRuleModal}>
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
