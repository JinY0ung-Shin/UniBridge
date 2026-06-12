import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getAlertResourceOwners,
  getAlertSettings,
  setAlertResourceOwner,
  testRecipientDelivery,
  updateAlertSettings,
  type AlertResourceOwner,
} from '../../api/client';
import { useToast } from '../../components/useToast';
import { useCanWrite } from '../../components/useCanWrite';

function parseEmails(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((email) => email.trim())
    .filter(Boolean);
}

function emailsToText(emails: string[]): string {
  return emails.join('\n');
}

function sameEmails(a: string[], b: string[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function resourceLabel(row: AlertResourceOwner): string {
  return row.display_name || row.resource_id;
}

function resourceKey(row: Pick<AlertResourceOwner, 'resource_type' | 'resource_id'>): string {
  return `${row.resource_type}:${row.resource_id}`;
}

const RESOURCE_TYPE_ORDER = ['db', 's3', 'nas', 'route'];

function resourceTypeLabel(t: (key: string) => string, resourceType: string): string {
  const labelKeys: Record<string, string> = {
    db: 'alerts.resourceTypeDb',
    s3: 'alerts.resourceTypeS3',
    nas: 'alerts.resourceTypeNas',
    route: 'alerts.resourceTypeRoute',
  };
  const key = labelKeys[resourceType];
  return key ? t(key) : resourceType;
}

interface AssigneeChange {
  row: AlertResourceOwner;
  emails?: string[];
  alerts_enabled?: boolean;
}

export default function AlertRecipientsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('alerts.write');

  const settingsQuery = useQuery({ queryKey: ['alert-settings'], queryFn: getAlertSettings });
  const resourcesQuery = useQuery({ queryKey: ['alert-resource-owners'], queryFn: getAlertResourceOwners });

  const settings = settingsQuery.data;
  const resources = useMemo(() => resourcesQuery.data ?? [], [resourcesQuery.data]);
  const resourceGroups = useMemo(() => {
    const grouped = new Map<string, AlertResourceOwner[]>();
    for (const row of resources) {
      const rows = grouped.get(row.resource_type) ?? [];
      rows.push(row);
      grouped.set(row.resource_type, rows);
    }
    return Array.from(grouped.entries()).sort(([a], [b]) => {
      const aIndex = RESOURCE_TYPE_ORDER.indexOf(a);
      const bIndex = RESOURCE_TYPE_ORDER.indexOf(b);
      if (aIndex === -1 && bIndex === -1) return a.localeCompare(b);
      if (aIndex === -1) return 1;
      if (bIndex === -1) return -1;
      return aIndex - bIndex;
    });
  }, [resources]);

  const [adminEmailsDraft, setAdminEmailsDraft] = useState<string | null>(null);
  const [resourceDrafts, setResourceDrafts] = useState<Record<string, string>>({});
  const [resourceEnabledDrafts, setResourceEnabledDrafts] = useState<Record<string, boolean>>({});
  const adminEmailsValue = adminEmailsDraft ?? (settings ? emailsToText(settings.admin_emails) : '');

  function resourceDraftValue(row: AlertResourceOwner): string {
    return resourceDrafts[resourceKey(row)] ?? emailsToText(row.emails);
  }

  function resourceAlertsEnabledValue(row: AlertResourceOwner): boolean {
    return resourceEnabledDrafts[resourceKey(row)] ?? row.alerts_enabled;
  }

  const pendingAssigneeChanges = useMemo<AssigneeChange[]>(
    () =>
      resources
        .map((row) => {
          const key = resourceKey(row);
          const emails = parseEmails(resourceDrafts[key] ?? emailsToText(row.emails));
          const alertsEnabled = resourceEnabledDrafts[key] ?? row.alerts_enabled;
          return {
            row,
            emails: sameEmails(emails, row.emails) ? undefined : emails,
            alerts_enabled: alertsEnabled === row.alerts_enabled ? undefined : alertsEnabled,
          };
        })
        .filter(({ emails, alerts_enabled }) => emails !== undefined || alerts_enabled !== undefined),
    [resources, resourceDrafts, resourceEnabledDrafts],
  );

  const updateAdminsMutation = useMutation({
    mutationFn: (admin_emails: string[]) => updateAlertSettings({ admin_emails }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['alert-settings'], updated);
      setAdminEmailsDraft(null);
      addToast({ type: 'success', title: t('alerts.adminEmailsSaved'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.adminEmailsSaved'), message: t('common.errorOccurred') });
    },
  });

  const testAdminsMutation = useMutation({
    mutationFn: ({ mailChannelId, emails }: { mailChannelId: number; emails: string[] }) =>
      testRecipientDelivery(mailChannelId, emails),
    onSuccess: (result) => {
      addToast({
        type: result.success ? 'success' : 'error',
        title: result.success ? t('alerts.testRecipientsSuccess') : t('alerts.testRecipientsFailed'),
        message: result.error ?? undefined,
      });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.testRecipientsFailed') });
    },
  });

  const saveAssigneesMutation = useMutation({
    mutationFn: async (changes: AssigneeChange[]) => {
      const updated: AlertResourceOwner[] = [];
      for (const change of changes) {
        const body: { emails?: string[]; alerts_enabled?: boolean } = {};
        if (change.emails !== undefined) body.emails = change.emails;
        if (change.alerts_enabled !== undefined) body.alerts_enabled = change.alerts_enabled;
        updated.push(
          await setAlertResourceOwner(change.row.resource_type, change.row.resource_id, body),
        );
      }
      return updated;
    },
    onSuccess: (updatedRows) => {
      const updatedByKey = new Map(updatedRows.map((row) => [resourceKey(row), row]));
      queryClient.setQueryData<AlertResourceOwner[]>(['alert-resource-owners'], (rows) =>
        rows?.map((row) => updatedByKey.get(resourceKey(row)) ?? row),
      );
      setResourceDrafts((prev) => {
        const next = { ...prev };
        for (const row of updatedRows) {
          delete next[resourceKey(row)];
        }
        return next;
      });
      setResourceEnabledDrafts((prev) => {
        const next = { ...prev };
        for (const row of updatedRows) {
          delete next[resourceKey(row)];
        }
        return next;
      });
      addToast({ type: 'success', title: t('alerts.assigneeSaved'), message: t('common.ok') });
    },
    onError: () => {
      addToast({ type: 'error', title: t('alerts.assigneeSaved'), message: t('common.errorOccurred') });
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
    },
  });

  function handleAdminsSubmit(e: React.FormEvent) {
    e.preventDefault();
    updateAdminsMutation.mutate(parseEmails(adminEmailsValue));
  }

  function handleTestAdmins() {
    const emails = parseEmails(adminEmailsValue);
    if (settings?.mail_channel_id == null || emails.length === 0) return;
    testAdminsMutation.mutate({ mailChannelId: settings.mail_channel_id, emails });
  }

  function handleAssigneesSave() {
    if (pendingAssigneeChanges.length === 0) return;
    saveAssigneesMutation.mutate(pendingAssigneeChanges);
  }

  function handleAssigneesDiscard() {
    setResourceDrafts({});
    setResourceEnabledDrafts({});
  }

  const hasSettings = Boolean(settings);
  const canTestAdmins = Boolean(
    hasSettings &&
    settings?.mail_channel_id != null &&
    parseEmails(adminEmailsValue).length > 0 &&
    !testAdminsMutation.isPending,
  );
  const isLoading = settingsQuery.isLoading || resourcesQuery.isLoading;
  const isError = settingsQuery.isError || resourcesQuery.isError;
  const hasPendingAssigneeChanges = pendingAssigneeChanges.length > 0;

  return (
    <div className="alert-tab-content">
      <form className="settings-panel" onSubmit={handleAdminsSubmit}>
        <div className="section-header">
          <h2>{t('alerts.adminsTitle')}</h2>
        </div>
        <div className="form-group form-group--full">
          <label htmlFor="admin-emails">{t('alerts.adminEmails')}</label>
          <textarea
            id="admin-emails"
            className="form-textarea email-list-textarea"
            rows={4}
            value={adminEmailsValue}
            disabled={!hasSettings || !canWrite}
            onChange={(e) => setAdminEmailsDraft(e.target.value)}
            placeholder="ops@example.com, oncall@example.com"
          />
          <p className="form-hint">{t('alerts.adminEmailsHint')}</p>
        </div>
        {canWrite && (
          <div className="setting-control-row">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!hasSettings || updateAdminsMutation.isPending}
            >
              {updateAdminsMutation.isPending ? t('common.saving') : t('alerts.adminEmailsSaved')}
            </button>
            <button
              type="button"
              className="btn btn-outline"
              onClick={handleTestAdmins}
              disabled={!canTestAdmins}
            >
              {testAdminsMutation.isPending ? t('common.loading') : t('alerts.testAdmins')}
            </button>
          </div>
        )}
      </form>

      <div className="section-header">
        <h2>{t('alerts.assigneesTitle')}</h2>
      </div>

      {isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {isError && <div className="error-banner">{t('common.errorOccurred')}</div>}
      {!isLoading && resources.length === 0 && !isError && (
        <div className="empty-state"><h3>{t('alerts.noResources')}</h3></div>
      )}
      {resourceGroups.length > 0 && (
        <>
          {canWrite && (
            <div className="resource-owner-save-bar">
              <span>
                {hasPendingAssigneeChanges
                  ? t('alerts.assigneeChangeCount', { count: pendingAssigneeChanges.length })
                  : t('alerts.noAssigneeChanges')}
              </span>
              <div className="resource-owner-save-actions">
                <button
                  type="button"
                  className="btn btn-outline"
                  onClick={handleAssigneesDiscard}
                  disabled={!hasPendingAssigneeChanges || saveAssigneesMutation.isPending}
                >
                  {t('alerts.discardAssigneeChanges')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleAssigneesSave}
                  disabled={!hasPendingAssigneeChanges || saveAssigneesMutation.isPending}
                >
                  {saveAssigneesMutation.isPending ? t('common.saving') : t('alerts.saveAssigneeChanges')}
                </button>
              </div>
            </div>
          )}
          <div className="resource-owner-groups">
            {resourceGroups.map(([resourceType, rows]) => (
              <section className="resource-owner-group" key={resourceType}>
                <div className="resource-owner-group-header">
                  <h3>{resourceTypeLabel(t, resourceType)}</h3>
                  <span>{t('alerts.resourceCount', { count: rows.length })}</span>
                </div>
                <div className="table-container">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>{t('alerts.resource')}</th>
                        <th>{t('alerts.resourceAlerts')}</th>
                        <th>{t('alerts.assignees')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => {
                        const key = resourceKey(row);
                        const label = resourceLabel(row);
                        const alertsEnabled = resourceAlertsEnabledValue(row);
                        return (
                          <tr key={key}>
                            <td className="cell-alias">{label}</td>
                            <td className="cell-alert-toggle">
                              <button
                                type="button"
                                role="switch"
                                aria-checked={alertsEnabled}
                                aria-label={`${t('alerts.resourceAlerts')} - ${label}`}
                                className={`resource-alert-switch${alertsEnabled ? ' resource-alert-switch--on' : ''}`}
                                disabled={!canWrite || saveAssigneesMutation.isPending}
                                onClick={() =>
                                  setResourceEnabledDrafts((prev) => ({ ...prev, [key]: !alertsEnabled }))
                                }
                              >
                                <span className="resource-alert-switch-track" aria-hidden="true">
                                  <span className="resource-alert-switch-thumb" />
                                </span>
                                <span className="resource-alert-switch-text">
                                  {alertsEnabled ? t('alerts.resourceAlertsOn') : t('alerts.resourceAlertsOff')}
                                </span>
                              </button>
                            </td>
                            <td>
                              <textarea
                                className="form-textarea email-list-textarea"
                                rows={2}
                                aria-label={`${t('alerts.assignees')} - ${label}`}
                                value={resourceDraftValue(row)}
                                disabled={!canWrite || saveAssigneesMutation.isPending}
                                onChange={(e) =>
                                  setResourceDrafts((prev) => ({ ...prev, [key]: e.target.value }))
                                }
                                placeholder={t('alerts.assigneesHint')}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
