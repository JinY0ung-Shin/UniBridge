import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  getServers,
  createServer,
  updateServer,
  deleteServer,
  testServer,
  type MonitoredServer,
  type MonitoredServerInput,
} from '../api/client';
import ResourceModal from '../components/ResourceModal';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import './Connections.css';
import './Servers.css';

interface FormState {
  name: string;
  address: string;
  description: string;
  enabled: boolean;
  disk_warn_pct: string;
  disk_crit_pct: string;
  cpu_warn_pct: string;
  mem_warn_pct: string;
}

const emptyForm: FormState = {
  name: '', address: '', description: '', enabled: true,
  disk_warn_pct: '', disk_crit_pct: '', cpu_warn_pct: '', mem_warn_pct: '',
};

function pctOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

function numToStr(value: number | null): string {
  return value == null ? '' : String(value);
}

function statusClass(status?: string | null): string {
  if (status === 'up') return 'status-badge--ok';
  if (status === 'down') return 'status-badge--alert';
  return 'status-badge--unknown';
}

function Servers() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('servers.write');

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>({ ...emptyForm });

  const serversQuery = useQuery({
    queryKey: ['servers'],
    queryFn: getServers,
    refetchInterval: 30_000,
  });

  function closeModal() {
    setShowModal(false);
    setEditingId(null);
    setForm({ ...emptyForm });
  }

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['servers'] });
  }

  const createMutation = useMutation({
    mutationFn: (data: MonitoredServerInput) => createServer(data),
    onSuccess: () => { invalidate(); closeModal(); },
    onError: () => addToast({ type: 'error', title: t('servers.saveFailed') }),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: MonitoredServerInput }) => updateServer(id, data),
    onSuccess: () => { invalidate(); closeModal(); },
    onError: () => addToast({ type: 'error', title: t('servers.saveFailed') }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteServer(id),
    onSuccess: invalidate,
    onError: () => addToast({ type: 'error', title: t('servers.deleteFailed') }),
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => testServer(id),
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

  function openEdit(server: MonitoredServer) {
    setForm({
      name: server.name,
      address: server.address,
      description: server.description ?? '',
      enabled: server.enabled,
      disk_warn_pct: numToStr(server.disk_warn_pct),
      disk_crit_pct: numToStr(server.disk_crit_pct),
      cpu_warn_pct: numToStr(server.cpu_warn_pct),
      mem_warn_pct: numToStr(server.mem_warn_pct),
    });
    setEditingId(server.id);
    setShowModal(true);
  }

  function submit() {
    const thresholds = {
      disk_warn_pct: pctOrNull(form.disk_warn_pct),
      disk_crit_pct: pctOrNull(form.disk_crit_pct),
      cpu_warn_pct: pctOrNull(form.cpu_warn_pct),
      mem_warn_pct: pctOrNull(form.mem_warn_pct),
    };
    if (editingId == null) {
      createMutation.mutate({
        name: form.name.trim(),
        address: form.address.trim(),
        description: form.description.trim(),
        enabled: form.enabled,
        ...thresholds,
      });
    } else {
      updateMutation.mutate({
        id: editingId,
        data: {
          address: form.address.trim(),
          description: form.description.trim(),
          enabled: form.enabled,
          ...thresholds,
        },
      });
    }
  }

  const servers = serversQuery.data ?? [];
  const saving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <h1>{t('servers.title')}</h1>
          <p className="page-subtitle">{t('servers.subtitle')}</p>
        </div>
        {canWrite && (
          <button className="btn btn-primary" onClick={openCreate}>{t('servers.add')}</button>
        )}
      </div>

      {serversQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {serversQuery.isError && <div className="error-banner">{t('common.errorOccurred')}</div>}

      {!serversQuery.isLoading && !serversQuery.isError && (
        servers.length === 0 ? (
          <div className="empty-state"><p>{t('servers.empty')}</p></div>
        ) : (
          <div className="table-container">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('servers.statusLabel')}</th>
                  <th>{t('servers.name')}</th>
                  <th>{t('servers.address')}</th>
                  <th>{t('servers.description')}</th>
                  <th>{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {servers.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <span className={`status-badge ${statusClass(s.enabled ? s.status : 'disabled')}`}>
                        {s.enabled ? (s.status ?? 'unknown') : t('servers.disabled')}
                      </span>
                    </td>
                    <td>
                      <button className="link-button" onClick={() => navigate(`/servers/${s.id}`)}>{s.name}</button>
                    </td>
                    <td className="cell-target">{s.address}</td>
                    <td>{s.description}</td>
                    <td className="cell-actions">
                      <button className="btn btn-sm" onClick={() => testMutation.mutate(s.id)} disabled={testMutation.isPending}>
                        {t('servers.test')}
                      </button>
                      {canWrite && (
                        <>
                          <button className="btn btn-sm" onClick={() => openEdit(s)}>{t('common.edit')}</button>
                          <button
                            className="btn btn-sm btn-danger"
                            onClick={() => { if (window.confirm(t('servers.deleteConfirm', { name: s.name }))) deleteMutation.mutate(s.id); }}
                          >
                            {t('common.delete')}
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {showModal && (
        <ResourceModal
          title={editingId == null ? t('servers.add') : t('servers.edit')}
          onClose={closeModal}
          closeLabel={t('common.cancel')}
        >
          <form
            className="modal-body"
            onSubmit={(e) => { e.preventDefault(); submit(); }}
          >
            <div className="form-group">
              <label>{t('servers.name')}</label>
              <input
                value={form.name}
                disabled={editingId != null}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="web-prod-1"
                required
              />
            </div>
            <div className="form-group">
              <label>{t('servers.address')}</label>
              <input
                value={form.address}
                onChange={(e) => setForm({ ...form, address: e.target.value })}
                placeholder="10.0.0.5:39100"
                required
              />
              <small className="form-hint">{t('servers.addressHint')}</small>
            </div>
            <div className="form-group">
              <label>{t('servers.description')}</label>
              <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </div>
            <div className="form-group form-group--checkbox">
              <label>
                <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                {t('servers.enabled')}
              </label>
            </div>

            <p className="form-section-label">{t('servers.thresholdsTitle')}</p>
            <p className="form-hint">{t('servers.thresholdsHint')}</p>
            <div className="form-row">
              <div className="form-group">
                <label>{t('servers.diskWarn')}</label>
                <input type="number" min={0} max={100} value={form.disk_warn_pct} onChange={(e) => setForm({ ...form, disk_warn_pct: e.target.value })} />
              </div>
              <div className="form-group">
                <label>{t('servers.diskCrit')}</label>
                <input type="number" min={0} max={100} value={form.disk_crit_pct} onChange={(e) => setForm({ ...form, disk_crit_pct: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>{t('servers.cpuWarn')}</label>
                <input type="number" min={0} max={100} value={form.cpu_warn_pct} onChange={(e) => setForm({ ...form, cpu_warn_pct: e.target.value })} />
              </div>
              <div className="form-group">
                <label>{t('servers.memWarn')}</label>
                <input type="number" min={0} max={100} value={form.mem_warn_pct} onChange={(e) => setForm({ ...form, mem_warn_pct: e.target.value })} />
              </div>
            </div>

            <div className="modal-footer">
              <button type="button" className="btn" onClick={closeModal}>{t('common.cancel')}</button>
              <button type="submit" className="btn btn-primary" disabled={saving}>
                {saving ? t('common.loading') : t('common.save')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}

export default Servers;
