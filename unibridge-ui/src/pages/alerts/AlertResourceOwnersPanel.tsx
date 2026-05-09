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
import { useCanWrite } from '../../components/useCanWrite';

function resourceLabel(row: AlertResourceOwner): string {
  return row.display_name || row.resource_id;
}

function resourceKey(row: Pick<AlertResourceOwner, 'resource_type' | 'resource_id'>): string {
  return `${row.resource_type}:${row.resource_id}`;
}

export default function AlertResourceOwnersPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('alerts.write');

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
    onMutate: async ({ resourceType, resourceId, ownerGroupId }) => {
      await queryClient.cancelQueries({ queryKey: ['alert-resource-owners'] });
      const previous = queryClient.getQueryData<AlertResourceOwner[]>(['alert-resource-owners']);
      const group = ownerGroups.find((item) => item.id === ownerGroupId);
      queryClient.setQueryData<AlertResourceOwner[]>(['alert-resource-owners'], (rows) =>
        rows?.map((row) =>
          row.resource_type === resourceType && row.resource_id === resourceId
            ? { ...row, owner_group_id: ownerGroupId, owner_group_name: group?.name ?? row.owner_group_name }
            : row,
        ),
      );
      return { previous };
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<AlertResourceOwner[]>(['alert-resource-owners'], (rows) =>
        rows?.map((row) => (resourceKey(row) === resourceKey(updated) ? updated : row)),
      );
      addToast({ type: 'success', title: t('alerts.resourceOwnerSaved'), message: t('common.ok') });
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(['alert-resource-owners'], context.previous);
      addToast({ type: 'error', title: t('alerts.resourceOwnerSaved'), message: t('common.errorOccurred') });
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: ({ resourceType, resourceId }: { resourceType: string; resourceId: string }) =>
      deleteAlertResourceOwner(resourceType, resourceId),
    onMutate: async ({ resourceType, resourceId }) => {
      await queryClient.cancelQueries({ queryKey: ['alert-resource-owners'] });
      const previous = queryClient.getQueryData<AlertResourceOwner[]>(['alert-resource-owners']);
      queryClient.setQueryData<AlertResourceOwner[]>(['alert-resource-owners'], (rows) =>
        rows?.map((row) =>
          row.resource_type === resourceType && row.resource_id === resourceId
            ? { ...row, owner_group_id: null, owner_group_name: null }
            : row,
        ),
      );
      return { previous };
    },
    onSuccess: () => {
      addToast({ type: 'success', title: t('alerts.resourceOwnerSaved'), message: t('common.ok') });
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(['alert-resource-owners'], context.previous);
      addToast({ type: 'error', title: t('alerts.resourceOwnerSaved'), message: t('common.errorOccurred') });
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
    },
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
                      disabled={!canWrite || assignMutation.isPending || deleteMutation.isPending}
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
