import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  createAlertOwnerGroup,
  deleteAlertOwnerGroup,
  getAlertOwnerGroups,
  updateAlertOwnerGroup,
  type AlertOwnerGroup,
  type AlertOwnerGroupCreate,
} from '../../api/client';
import { useToast } from '../../components/useToast';
import { useCanWrite } from '../../components/useCanWrite';
import ResourceModal from '../../components/ResourceModal';

const emptyForm = () => ({
  name: '',
  emails: '',
  enabled: true,
});

function parseEmails(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((email) => email.trim())
    .filter(Boolean);
}

export default function AlertOwnerGroupsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('alerts.write');
  const [showModal, setShowModal] = useState(false);
  const [editingGroupId, setEditingGroupId] = useState<number | null>(null);
  const [form, setForm] = useState(emptyForm());

  const groupsQuery = useQuery({ queryKey: ['alert-owner-groups'], queryFn: getAlertOwnerGroups });
  const groups = groupsQuery.data ?? [];

  const createMutation = useMutation({
    mutationFn: (body: AlertOwnerGroupCreate) => createAlertOwnerGroup(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-owner-groups'] });
      closeModal();
      addToast({ type: 'success', title: t('alerts.addOwnerGroup'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.addOwnerGroup'), message: t('common.errorOccurred') }),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<AlertOwnerGroupCreate> }) =>
      updateAlertOwnerGroup(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-owner-groups'] });
      closeModal();
      addToast({ type: 'success', title: t('alerts.editOwnerGroup'), message: t('common.ok') });
    },
    onError: () => addToast({ type: 'error', title: t('alerts.editOwnerGroup'), message: t('common.errorOccurred') }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteAlertOwnerGroup(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alert-owner-groups'] }),
    onError: () => addToast({ type: 'error', title: t('common.delete'), message: t('common.errorOccurred') }),
  });

  function openCreate() {
    setForm(emptyForm());
    setEditingGroupId(null);
    setShowModal(true);
  }

  function openEdit(group: AlertOwnerGroup) {
    setForm({ name: group.name, emails: group.emails.join(', '), enabled: group.enabled });
    setEditingGroupId(group.id);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingGroupId(null);
    setForm(emptyForm());
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const body: AlertOwnerGroupCreate = {
      name: form.name,
      emails: parseEmails(form.emails),
      enabled: form.enabled,
    };
    if (editingGroupId !== null) updateMutation.mutate({ id: editingGroupId, body });
    else createMutation.mutate(body);
  }

  function handleDelete(group: AlertOwnerGroup) {
    if (window.confirm(t('alerts.deleteConfirm'))) deleteMutation.mutate(group.id);
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="alert-tab-content">
      <div className="section-header">
        <h2>{t('alerts.ownerGroupsTab')}</h2>
        {canWrite && (
          <button className="btn btn-primary" onClick={openCreate}>+ {t('alerts.addOwnerGroup')}</button>
        )}
      </div>

      {groupsQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {groupsQuery.isError && <div className="error-banner">{t('common.errorOccurred')}</div>}
      {!groupsQuery.isLoading && groups.length === 0 && !groupsQuery.isError && (
        <div className="empty-state"><h3>{t('alerts.noOwnerGroups')}</h3></div>
      )}
      {groups.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('alerts.emails')}</th>
                <th>{t('alerts.enabled')}</th>
                {canWrite && <th>{t('common.actions')}</th>}
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.id}>
                  <td className="cell-alias">{group.name}</td>
                  <td>{group.emails.join(', ')}</td>
                  <td>
                    <span className={`badge ${group.enabled ? 'badge-ok' : 'badge-unknown'}`}>
                      {group.enabled ? t('common.active') : t('common.disabled')}
                    </span>
                  </td>
                  {canWrite && (
                    <td>
                      <div className="action-buttons">
                        <button className="btn btn-sm btn-secondary" onClick={() => openEdit(group)}>
                          {t('common.edit')}
                        </button>
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => handleDelete(group)}
                          disabled={deleteMutation.isPending}
                        >
                          {t('common.delete')}
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showModal && (
        <ResourceModal
          title={editingGroupId !== null ? t('alerts.editOwnerGroup') : t('alerts.addOwnerGroup')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handleSubmit}>
            <div className="form-grid">
              <div className="form-group form-group--full">
                <label htmlFor="owner-group-name">{t('alerts.ownerGroupName')}</label>
                <input
                  id="owner-group-name"
                  type="text"
                  value={form.name}
                  onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="owner-group-emails">{t('alerts.emails')}</label>
                <textarea
                  id="owner-group-emails"
                  className="form-textarea"
                  rows={4}
                  value={form.emails}
                  onChange={(e) => setForm((prev) => ({ ...prev, emails: e.target.value }))}
                  required
                />
                <p className="form-hint">{t('alerts.ownerGroupEmailHint')}</p>
              </div>
              <div className="form-group form-group--full">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={form.enabled}
                    onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.checked }))}
                  />
                  {t('alerts.enabled')}
                </label>
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>
                {t('alerts.cancel')}
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving}>
                {isSaving ? t('common.saving') : t('alerts.save')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}
