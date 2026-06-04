import { useEffect, useState } from 'react';
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

function resourceLabel(row: AlertResourceOwner): string {
  return row.display_name || row.resource_id;
}

function resourceKey(row: Pick<AlertResourceOwner, 'resource_type' | 'resource_id'>): string {
  return `${row.resource_type}:${row.resource_id}`;
}

export default function AlertRecipientsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('alerts.write');

  const settingsQuery = useQuery({ queryKey: ['alert-settings'], queryFn: getAlertSettings });
  const resourcesQuery = useQuery({ queryKey: ['alert-resource-owners'], queryFn: getAlertResourceOwners });

  const settings = settingsQuery.data;
  const resources = resourcesQuery.data ?? [];

  const [adminEmailsDraft, setAdminEmailsDraft] = useState('');
  const [resourceDrafts, setResourceDrafts] = useState<Record<string, string>>({});

  useEffect(() => {
    if (settings) setAdminEmailsDraft(emailsToText(settings.admin_emails));
  }, [settings]);

  useEffect(() => {
    const rows = resourcesQuery.data;
    if (!rows) return;
    setResourceDrafts((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const row of rows) {
        const key = resourceKey(row);
        if (!(key in next)) {
          next[key] = emailsToText(row.emails);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [resourcesQuery.data]);

  const updateAdminsMutation = useMutation({
    mutationFn: (admin_emails: string[]) => updateAlertSettings({ admin_emails }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['alert-settings'], updated);
      setAdminEmailsDraft(emailsToText(updated.admin_emails));
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

  const assignMutation = useMutation({
    mutationFn: ({
      resourceType,
      resourceId,
      emails,
    }: {
      resourceType: string;
      resourceId: string;
      emails: string[];
    }) => setAlertResourceOwner(resourceType, resourceId, { emails }),
    onSuccess: (updated) => {
      queryClient.setQueryData<AlertResourceOwner[]>(['alert-resource-owners'], (rows) =>
        rows?.map((row) => (resourceKey(row) === resourceKey(updated) ? updated : row)),
      );
      setResourceDrafts((prev) => ({ ...prev, [resourceKey(updated)]: emailsToText(updated.emails) }));
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
    updateAdminsMutation.mutate(parseEmails(adminEmailsDraft));
  }

  function handleTestAdmins() {
    const emails = parseEmails(adminEmailsDraft);
    if (settings?.mail_channel_id == null || emails.length === 0) return;
    testAdminsMutation.mutate({ mailChannelId: settings.mail_channel_id, emails });
  }

  function handleAssigneeSave(row: AlertResourceOwner) {
    assignMutation.mutate({
      resourceType: row.resource_type,
      resourceId: row.resource_id,
      emails: parseEmails(resourceDrafts[resourceKey(row)] ?? ''),
    });
  }

  const hasSettings = Boolean(settings);
  const canTestAdmins = Boolean(
    hasSettings &&
    settings?.mail_channel_id != null &&
    parseEmails(adminEmailsDraft).length > 0 &&
    !testAdminsMutation.isPending,
  );
  const isLoading = settingsQuery.isLoading || resourcesQuery.isLoading;
  const isError = settingsQuery.isError || resourcesQuery.isError;

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
            value={adminEmailsDraft}
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
      {resources.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('alerts.resourceType')}</th>
                <th>{t('alerts.resource')}</th>
                <th>{t('alerts.assignees')}</th>
                {canWrite && <th>{t('common.actions')}</th>}
              </tr>
            </thead>
            <tbody>
              {resources.map((row) => {
                const key = resourceKey(row);
                return (
                  <tr key={key}>
                    <td className="cell-target">{row.resource_type}</td>
                    <td className="cell-alias">{resourceLabel(row)}</td>
                    <td>
                      <textarea
                        className="form-textarea email-list-textarea"
                        rows={2}
                        aria-label={`${t('alerts.assignees')} - ${resourceLabel(row)}`}
                        value={resourceDrafts[key] ?? ''}
                        disabled={!canWrite || assignMutation.isPending}
                        onChange={(e) =>
                          setResourceDrafts((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        placeholder={t('alerts.assigneesHint')}
                      />
                    </td>
                    {canWrite && (
                      <td>
                        <button
                          className="btn btn-sm btn-primary"
                          onClick={() => handleAssigneeSave(row)}
                          disabled={assignMutation.isPending}
                        >
                          {t('alerts.save')}
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
