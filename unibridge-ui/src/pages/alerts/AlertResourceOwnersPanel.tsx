import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  deleteAlertResourceOwner,
  getAlertOwnerGroups,
  getAlertResourceOwners,
  setAlertResourceOwner,
  type AlertResourceOwner,
} from '../../api/client';
import { useToast } from '../../components/useToast';

function resourceLabel(row: AlertResourceOwner): string {
  return row.display_name || row.resource_id;
}

export default function AlertResourceOwnersPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const groupsQuery = useQuery({ queryKey: ['alert-owner-groups'], queryFn: getAlertOwnerGroups });
  const resourcesQuery = useQuery({ queryKey: ['alert-resource-owners'], queryFn: getAlertResourceOwners });

  const ownerGroups = groupsQuery.data ?? [];
  const resources = resourcesQuery.data ?? [];

  const assignMutation = useMutation({
    mutationFn: ({
      resourceType,
      resourceId,
      ownerGroupId,
    }: {
      resourceType: string;
      resourceId: string;
      ownerGroupId: number;
    }) => setAlertResourceOwner(resourceType, resourceId, { owner_group_id: ownerGroupId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
      addToast({ type: 'success', title: t('alerts.resourceOwnerSaved'), message: t('common.ok') });
    },
    onError: () =>
      addToast({ type: 'error', title: t('alerts.resourceOwnerSaved'), message: t('common.errorOccurred') }),
  });

  const deleteMutation = useMutation({
    mutationFn: ({ resourceType, resourceId }: { resourceType: string; resourceId: string }) =>
      deleteAlertResourceOwner(resourceType, resourceId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
      addToast({ type: 'success', title: t('alerts.resourceOwnerSaved'), message: t('common.ok') });
    },
    onError: () =>
      addToast({ type: 'error', title: t('alerts.resourceOwnerSaved'), message: t('common.errorOccurred') }),
  });

  function handleOwnerChange(row: AlertResourceOwner, value: string) {
    if (value) {
      assignMutation.mutate({
        resourceType: row.resource_type,
        resourceId: row.resource_id,
        ownerGroupId: Number(value),
      });
    } else {
      deleteMutation.mutate({ resourceType: row.resource_type, resourceId: row.resource_id });
    }
  }

  const isLoading = groupsQuery.isLoading || resourcesQuery.isLoading;
  const isError = groupsQuery.isError || resourcesQuery.isError;

  return (
    <div className="alert-tab-content">
      {isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {isError && <div className="error-banner">{t('common.errorOccurred')}</div>}
      {!isLoading && resources.length === 0 && !isError && (
        <div className="empty-state"><h3>{t('alerts.noResourceOwners')}</h3></div>
      )}
      {resources.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('alerts.resourceType')}</th>
                <th>{t('alerts.resource')}</th>
                <th>{t('alerts.ownerGroupsTab')}</th>
              </tr>
            </thead>
            <tbody>
              {resources.map((row) => (
                <tr key={`${row.resource_type}:${row.resource_id}`}>
                  <td className="cell-target">{row.resource_type}</td>
                  <td className="cell-alias">{resourceLabel(row)}</td>
                  <td>
                    <select
                      aria-label={`Owner group for ${resourceLabel(row)}`}
                      value={row.owner_group_id ?? ''}
                      onChange={(e) => handleOwnerChange(row, e.target.value)}
                      disabled={assignMutation.isPending || deleteMutation.isPending}
                    >
                      <option value="">{t('alerts.unassigned')}</option>
                      {ownerGroups.map((group) => (
                        <option key={group.id} value={group.id}>
                          {group.name}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
