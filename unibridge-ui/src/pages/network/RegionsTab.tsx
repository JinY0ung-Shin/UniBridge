import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { createRegion, deleteRegion, updateRegion, type Region, type RegionInput } from '../../api/network';
import ResourceModal from '../../components/ResourceModal';
import { useCanWrite } from '../../components/useCanWrite';
import { NETWORK_KEY, networkErrorMessage, splitList } from './networkUtils';
import { useNetworkMutation, useRegions } from './useNetworkActions';

function RegionFormModal({ region, onClose }: { region: Region | null; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    name: region?.name ?? '',
    description: region?.description ?? '',
    cidrs: (region?.cidrs ?? []).join('\n'),
  });
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: RegionInput) => (region ? updateRegion(region.id, body) : createRegion(body)),
    onMutate: () => setError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: NETWORK_KEY });
      onClose();
    },
    onError: (err) => setError(networkErrorMessage(err, t('network.saveFailed'))),
  });

  return (
    <ResourceModal
      title={region ? t('network.regions.edit') : t('network.regions.add')}
      onClose={onClose}
      closeLabel={t('common.close')}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({ name: form.name.trim(), description: form.description.trim(), cidrs: splitList(form.cidrs) });
        }}
      >
        <div className="form-grid">
          <div className="form-group form-group--full">
            <label htmlFor="region-name">{t('network.regions.name')}</label>
            <input id="region-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="region-description">{t('network.description')}</label>
            <input
              id="region-description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="region-cidrs">
              {t('network.regions.cidrs')} <span id="region-cidrs-hint" className="hint">{t('network.regions.cidrsHint')}</span>
            </label>
            <textarea
              id="region-cidrs"
              rows={4}
              value={form.cidrs}
              onChange={(e) => setForm({ ...form, cidrs: e.target.value })}
              placeholder={'10.10.0.0/16\n10.11.0.0/16'}
              aria-describedby="region-cidrs-hint"
            />
          </div>
        </div>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>{t('common.cancel')}</button>
          <button type="submit" className="btn btn-primary" disabled={save.isPending} aria-busy={save.isPending}>
            {save.isPending ? t('common.saving') : region ? t('common.save') : t('common.create')}
          </button>
        </div>
      </form>
    </ResourceModal>
  );
}

function RegionsTab() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const regionsQuery = useRegions();
  const remove = useNetworkMutation(deleteRegion, 'network.deleteFailed');
  const [editing, setEditing] = useState<Region | null | 'new'>(null);
  const regions = regionsQuery.data ?? [];

  return (
    <div>
      <div className="network-toolbar">
        <p className="page-subtitle">{t('network.regions.hint')}</p>
        {canWrite && (
          <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>{t('network.regions.add')}</button>
        )}
      </div>

      {regionsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {regionsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}
      {regionsQuery.isSuccess && (regions.length === 0 ? (
        <div className="empty-state"><p>{t('network.regions.empty')}</p></div>
      ) : (
        <div className="table-container">
          <table className="data-table network-table" aria-label={t('network.tabs.regions')}>
            <thead>
              <tr>
                <th scope="col">{t('network.regions.name')}</th>
                <th scope="col">{t('network.description')}</th>
                <th scope="col">{t('network.regions.cidrs')}</th>
                <th scope="col">{t('network.regions.hostCount')}</th>
                {canWrite && <th scope="col">{t('common.actions')}</th>}
              </tr>
            </thead>
            <tbody>
              {regions.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.description}</td>
                  <td className="mono">{r.cidrs.length ? r.cidrs.join(', ') : <span className="network-muted">—</span>}</td>
                  <td>{r.host_count}</td>
                  {canWrite && (
                    <td className="cell-actions">
                      <button
                        type="button"
                        className="btn btn-sm btn-secondary"
                        aria-label={t('network.editItem', { name: r.name })}
                        onClick={() => setEditing(r)}
                      >
                        {t('common.edit')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        aria-label={t('network.deleteItem', { name: r.name })}
                        onClick={() => {
                          if (window.confirm(t('network.regions.deleteConfirm', { name: r.name }))) remove.mutate(r.id);
                        }}
                        disabled={remove.isPending}
                      >
                        {t('common.delete')}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {editing && (
        <RegionFormModal region={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}

export default RegionsTab;
