import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getExternalServices,
  createExternalService,
  updateExternalService,
  deleteExternalService,
  testExternalService,
  type ExternalService,
  type ExternalServiceInput,
} from '../api/client';
import ResourceModal from './ResourceModal';
import { useToast } from './useToast';
import { useCanWrite } from './useCanWrite';

interface FormState {
  name: string;
  address: string;
  metrics_path: string;
  scheme: 'http' | 'https';
  description: string;
  enabled: boolean;
}

const emptyForm: FormState = {
  name: '', address: '', metrics_path: '/metrics', scheme: 'http', description: '', enabled: true,
};

function statusClass(status?: string | null): string {
  if (status === 'up') return 'status-badge--ok';
  if (status === 'down') return 'status-badge--alert';
  return 'status-badge--unknown';
}

/**
 * Registry for API services monitored WITHOUT gateway onboarding: they expose
 * RED metrics per docs/api-metrics-convention.md and UniBridge's Prometheus
 * scrapes them. Rendered as a section on the Servers page (same registry +
 * file_sd pipeline as host monitoring).
 */
function ExternalServicesSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('servers.write');

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>({ ...emptyForm });

  const servicesQuery = useQuery({
    queryKey: ['external-services'],
    queryFn: getExternalServices,
    refetchInterval: 30_000,
  });

  function closeModal() {
    setShowModal(false);
    setEditingId(null);
    setForm({ ...emptyForm });
  }

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['external-services'] });
  }

  const saveError = (err: unknown) => {
    let message = '';
    if (err && typeof err === 'object' && 'isAxiosError' in err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      if (typeof detail === 'string') message = detail;
    }
    addToast({ type: 'error', title: t('externalServices.saveFailed'), message });
  };

  const createMutation = useMutation({
    mutationFn: (data: ExternalServiceInput) => createExternalService(data),
    onSuccess: () => { invalidate(); closeModal(); },
    onError: saveError,
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: ExternalServiceInput }) => updateExternalService(id, data),
    onSuccess: () => { invalidate(); closeModal(); },
    onError: saveError,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteExternalService(id),
    onSuccess: invalidate,
    onError: saveError,
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => testExternalService(id),
    onSuccess: (data) => addToast({
      type: data.status === 'up' ? 'success' : 'error',
      title: `${t('servers.statusLabel')}: ${data.status}`,
      message: data.detail ?? undefined,
    }),
  });

  function openCreate() {
    setForm({ ...emptyForm });
    setEditingId(null);
    setShowModal(true);
  }

  function openEdit(svc: ExternalService) {
    setForm({
      name: svc.name,
      address: svc.address,
      metrics_path: svc.metrics_path,
      scheme: svc.scheme === 'https' ? 'https' : 'http',
      description: svc.description ?? '',
      enabled: svc.enabled,
    });
    setEditingId(svc.id);
    setShowModal(true);
  }

  function submit() {
    const body: ExternalServiceInput = {
      address: form.address.trim(),
      metrics_path: form.metrics_path.trim() || '/metrics',
      scheme: form.scheme,
      description: form.description.trim(),
      enabled: form.enabled,
    };
    if (editingId == null) {
      createMutation.mutate({ ...body, name: form.name.trim() });
    } else {
      updateMutation.mutate({ id: editingId, data: body });
    }
  }

  const services = servicesQuery.data ?? [];
  const saving = createMutation.isPending || updateMutation.isPending;

  return (
    <section className="external-services" aria-labelledby="external-services-title">
      <div className="page-header" style={{ marginTop: 32 }}>
        <div>
          <h2 id="external-services-title" className="section-title">{t('externalServices.title')}</h2>
          <p className="page-subtitle">{t('externalServices.subtitle')}</p>
        </div>
        <div className="page-header__actions connections-header-actions">
          <Link to="/external/guide" className="btn btn-secondary">{t('externalServices.viewGuide')}</Link>
          {canWrite && (
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              {t('externalServices.add')}
            </button>
          )}
        </div>
      </div>

      {servicesQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {servicesQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}

      {!servicesQuery.isLoading && !servicesQuery.isError && (
        services.length === 0 ? (
          <div className="empty-state"><p>{t('externalServices.empty')}</p></div>
        ) : (
          <div className="table-container">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('servers.statusLabel')}</th>
                  <th scope="col">{t('externalServices.name')}</th>
                  <th scope="col">{t('externalServices.address')}</th>
                  <th scope="col">{t('externalServices.metricsPath')}</th>
                  <th scope="col">{t('servers.description')}</th>
                  <th scope="col">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {services.map((s) => {
                  const isDeleting = deleteMutation.isPending && deleteMutation.variables === s.id;
                  const isTesting = testMutation.isPending && testMutation.variables === s.id;
                  return (
                    <tr key={s.id}>
                      <td>
                        <span className={`status-badge ${statusClass(s.enabled ? s.status : 'disabled')}`}>
                          {s.enabled ? (s.status ?? 'unknown') : t('servers.disabled')}
                        </span>
                      </td>
                      <td>{s.name}</td>
                      <td className="cell-target">{`${s.scheme ?? 'http'}://${s.address}`}</td>
                      <td className="cell-target">{s.metrics_path}</td>
                      <td>{s.description}</td>
                      <td className="cell-actions">
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('externalServices.testService', { name: s.name })}
                          onClick={() => testMutation.mutate(s.id)}
                          disabled={testMutation.isPending}
                          aria-busy={isTesting}
                        >
                          {isTesting ? t('common.testing') : t('servers.test')}
                        </button>
                        {canWrite && (
                          <>
                            <button
                              type="button"
                              className="btn btn-sm btn-secondary"
                              aria-label={t('externalServices.editService', { name: s.name })}
                              onClick={() => openEdit(s)}
                            >
                              {t('common.edit')}
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              aria-label={t('externalServices.deleteService', { name: s.name })}
                              onClick={() => {
                                if (window.confirm(t('externalServices.deleteConfirm', { name: s.name }))) {
                                  deleteMutation.mutate(s.id);
                                }
                              }}
                              disabled={deleteMutation.isPending}
                              aria-busy={isDeleting}
                            >
                              {isDeleting ? t('common.deleting') : t('common.delete')}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {showModal && (
        <ResourceModal
          title={editingId == null ? t('externalServices.add') : t('externalServices.edit')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
            <div className="form-grid">
              <div className="form-group">
                <label htmlFor="ext-svc-name">
                  {t('externalServices.name')}{' '}
                  <span id="ext-svc-name-hint" className="hint">{t('externalServices.nameHint')}</span>
                </label>
                <input
                  id="ext-svc-name"
                  value={form.name}
                  disabled={editingId != null}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="order-api"
                  aria-label={t('externalServices.name')}
                  aria-describedby="ext-svc-name-hint"
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="ext-svc-address">
                  {t('externalServices.address')}{' '}
                  <span id="ext-svc-address-hint" className="hint">{t('externalServices.addressHint')}</span>
                </label>
                <input
                  id="ext-svc-address"
                  value={form.address}
                  onChange={(e) => setForm({ ...form, address: e.target.value })}
                  placeholder="10.0.0.7:8080"
                  aria-label={t('externalServices.address')}
                  aria-describedby="ext-svc-address-hint"
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="ext-svc-metrics-path">{t('externalServices.metricsPath')}</label>
                <input
                  id="ext-svc-metrics-path"
                  value={form.metrics_path}
                  onChange={(e) => setForm({ ...form, metrics_path: e.target.value })}
                  placeholder="/metrics"
                  aria-label={t('externalServices.metricsPath')}
                />
              </div>
              <div className="form-group">
                <label htmlFor="ext-svc-scheme">{t('externalServices.scheme')}</label>
                <select
                  id="ext-svc-scheme"
                  value={form.scheme}
                  onChange={(e) => setForm({ ...form, scheme: e.target.value === 'https' ? 'https' : 'http' })}
                  aria-label={t('externalServices.scheme')}
                >
                  <option value="http">http</option>
                  <option value="https">https</option>
                </select>
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="ext-svc-description">{t('servers.description')}</label>
                <input
                  id="ext-svc-description"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  aria-label={t('servers.description')}
                />
              </div>
              <div className="form-group">
                <label>&nbsp;</label>
                <label className="method-check">
                  <input
                    type="checkbox"
                    checked={form.enabled}
                    onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                  />
                  {t('servers.enabled')}
                </label>
              </div>
            </div>

            <p className="hint">{t('externalServices.conventionHint')}</p>

            {(createMutation.isError || updateMutation.isError) && (
              <div className="form-error" role="alert">{t('externalServices.saveFailed')}</div>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button type="submit" className="btn btn-primary" disabled={saving} aria-busy={saving}>
                {saving ? t('common.saving') : editingId == null ? t('common.create') : t('common.save')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </section>
  );
}

export default ExternalServicesSection;
