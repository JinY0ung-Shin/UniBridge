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
  disk_mountpoints: string;
  disk_warn_pct: string;
  disk_crit_pct: string;
  cpu_warn_pct: string;
  mem_warn_pct: string;
}

const emptyForm: FormState = {
  name: '', address: '', description: '', enabled: true,
  disk_mountpoints: '',
  disk_warn_pct: '', disk_crit_pct: '', cpu_warn_pct: '', mem_warn_pct: '',
};

function pctOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

function strOrNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

function numToStr(value: number | null): string {
  return value == null ? '' : String(value);
}

function statusClass(status?: string | null): string {
  if (status === 'up') return 'status-badge--ok';
  if (status === 'down') return 'status-badge--alert';
  return 'status-badge--unknown';
}

/**
 * Surface the backend's reason for a failure instead of a generic toast.
 * FastAPI uses two `detail` shapes: a plain string for HTTPException (409/403,
 * threshold 422) and an array of `{msg, loc}` for request-body validation
 * (e.g. a bad host:port address). Handle both, falling back to `fallback`.
 */
function extractErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'isAxiosError' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
        .filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
  }
  return fallback;
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
  const [serverSearch, setServerSearch] = useState('');

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
    onError: (err) => addToast({
      type: 'error',
      title: t('servers.saveFailed'),
      message: extractErrorMessage(err, ''),
    }),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: MonitoredServerInput }) => updateServer(id, data),
    onSuccess: () => { invalidate(); closeModal(); },
    onError: (err) => addToast({
      type: 'error',
      title: t('servers.saveFailed'),
      message: extractErrorMessage(err, ''),
    }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteServer(id),
    onSuccess: invalidate,
    onError: (err) => addToast({
      type: 'error',
      title: t('servers.deleteFailed'),
      message: extractErrorMessage(err, ''),
    }),
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
      disk_mountpoints: server.disk_mountpoints ?? '',
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
    const diskMountpoints = strOrNull(form.disk_mountpoints);
    if (editingId == null) {
      createMutation.mutate({
        name: form.name.trim(),
        address: form.address.trim(),
        description: form.description.trim(),
        enabled: form.enabled,
        disk_mountpoints: diskMountpoints,
        ...thresholds,
      });
    } else {
      updateMutation.mutate({
        id: editingId,
        data: {
          address: form.address.trim(),
          description: form.description.trim(),
          enabled: form.enabled,
          disk_mountpoints: diskMountpoints,
          ...thresholds,
        },
      });
    }
  }

  const servers = serversQuery.data ?? [];
  const normalizedServerSearch = serverSearch.trim().toLowerCase();
  const filteredServers = normalizedServerSearch
    ? servers.filter((server) => [
        server.name,
        server.address,
        server.description,
        server.disk_mountpoints || t('servers.diskMountpointsInherited'),
        server.enabled ? (server.status ?? 'unknown') : t('servers.disabled'),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedServerSearch))
    : servers;
  const saving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <h1>{t('servers.title')}</h1>
          <p className="page-subtitle">{t('servers.subtitle')}</p>
        </div>
        <div className="page-header__actions connections-header-actions">
          {servers.length > 0 && (
            <input
              className="connection-search-input"
              type="search"
              value={serverSearch}
              onChange={(event) => setServerSearch(event.target.value)}
              placeholder={t('servers.searchPlaceholder')}
              aria-label={t('servers.searchPlaceholder')}
            />
          )}
          {canWrite && (
            <button type="button" className="btn btn-primary" onClick={openCreate}>{t('servers.add')}</button>
          )}
        </div>
      </div>

      {serversQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {serversQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}

      {!serversQuery.isLoading && !serversQuery.isError && (
        servers.length === 0 ? (
          <div className="empty-state"><p>{t('servers.empty')}</p></div>
        ) : filteredServers.length === 0 ? (
          <div className="empty-state">
            <h3>{t('servers.noSearchResults')}</h3>
            <p>{t('servers.noSearchResultsDesc')}</p>
            <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setServerSearch('')}>
              {t('common.clearSearch')}
            </button>
          </div>
        ) : (
          <div className="table-container">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">{t('servers.statusLabel')}</th>
                  <th scope="col">{t('servers.name')}</th>
                  <th scope="col">{t('servers.address')}</th>
                  <th scope="col">{t('servers.diskMountpoints')}</th>
                  <th scope="col">{t('servers.description')}</th>
                  <th scope="col">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filteredServers.map((s) => {
                  const isTesting = testMutation.isPending && testMutation.variables === s.id;
                  const isDeleting = deleteMutation.isPending && deleteMutation.variables === s.id;
                  return (
                    <tr key={s.id}>
                      <td>
                        <span className={`status-badge ${statusClass(s.enabled ? s.status : 'disabled')}`}>
                          {s.enabled ? (s.status ?? 'unknown') : t('servers.disabled')}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="link-button"
                          aria-label={t('servers.openDetails', { name: s.name })}
                          onClick={() => navigate(`/servers/${s.id}`)}
                        >
                          {s.name}
                        </button>
                      </td>
                      <td className="cell-target">{s.address}</td>
                      <td className="cell-target">{s.disk_mountpoints || t('servers.diskMountpointsInherited')}</td>
                      <td>{s.description}</td>
                      <td className="cell-actions">
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('servers.testServer', { name: s.name })}
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
                              aria-label={t('servers.editServer', { name: s.name })}
                              onClick={() => openEdit(s)}
                            >
                              {t('common.edit')}
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              aria-label={t('servers.deleteServer', { name: s.name })}
                              onClick={() => { if (window.confirm(t('servers.deleteConfirm', { name: s.name }))) deleteMutation.mutate(s.id); }}
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
          title={editingId == null ? t('servers.add') : t('servers.edit')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
            <div className="form-grid">
              <div className="form-group">
                <label htmlFor="server-name">{t('servers.name')}</label>
                <input
                  id="server-name"
                  value={form.name}
                  disabled={editingId != null}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="web-prod-1"
                  aria-label={t('servers.name')}
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="server-address">
                  {t('servers.address')} <span id="server-address-hint" className="hint">{t('servers.addressHint')}</span>
                </label>
                <input
                  id="server-address"
                  value={form.address}
                  onChange={(e) => setForm({ ...form, address: e.target.value })}
                  placeholder="10.0.0.5:39100"
                  aria-label={t('servers.address')}
                  aria-describedby="server-address-hint"
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="server-disk-mountpoints">
                  {t('servers.diskMountpoints')}{' '}
                  <span id="server-disk-mountpoints-hint" className="hint">{t('servers.diskMountpointsHint')}</span>
                </label>
                <input
                  id="server-disk-mountpoints"
                  value={form.disk_mountpoints}
                  onChange={(e) => setForm({ ...form, disk_mountpoints: e.target.value })}
                  placeholder={t('servers.diskMountpointsPlaceholder')}
                  aria-label={t('servers.diskMountpoints')}
                  aria-describedby="server-disk-mountpoints-hint"
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="server-description">{t('servers.description')}</label>
                <input
                  id="server-description"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  aria-label={t('servers.description')}
                />
              </div>
              <div className="form-group">
                <label>&nbsp;</label>
                <label className="method-check">
                  <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                  {t('servers.enabled')}
                </label>
              </div>
            </div>

            <div className="form-group form-group--full">
              <label>
                <span id="server-thresholds-label">{t('servers.thresholdsTitle')}</span>{' '}
                <span id="server-thresholds-hint" className="hint">{t('servers.thresholdsHint')}</span>
              </label>
            </div>
            <div
              className="form-grid"
              role="group"
              aria-labelledby="server-thresholds-label"
              aria-describedby="server-thresholds-hint"
            >
              <div className="form-group">
                <label htmlFor="server-disk-warn">{t('servers.diskWarn')}</label>
                <input id="server-disk-warn" type="number" min={0} max={100} value={form.disk_warn_pct} onChange={(e) => setForm({ ...form, disk_warn_pct: e.target.value })} placeholder="80" aria-label={t('servers.diskWarn')} />
              </div>
              <div className="form-group">
                <label htmlFor="server-disk-crit">{t('servers.diskCrit')}</label>
                <input id="server-disk-crit" type="number" min={0} max={100} value={form.disk_crit_pct} onChange={(e) => setForm({ ...form, disk_crit_pct: e.target.value })} placeholder="90" aria-label={t('servers.diskCrit')} />
              </div>
              <div className="form-group">
                <label htmlFor="server-cpu-warn">{t('servers.cpuWarn')}</label>
                <input id="server-cpu-warn" type="number" min={0} max={100} value={form.cpu_warn_pct} onChange={(e) => setForm({ ...form, cpu_warn_pct: e.target.value })} placeholder="90" aria-label={t('servers.cpuWarn')} />
              </div>
              <div className="form-group">
                <label htmlFor="server-mem-warn">{t('servers.memWarn')}</label>
                <input id="server-mem-warn" type="number" min={0} max={100} value={form.mem_warn_pct} onChange={(e) => setForm({ ...form, mem_warn_pct: e.target.value })} placeholder="90" aria-label={t('servers.memWarn')} />
              </div>
            </div>

            {(createMutation.isError || updateMutation.isError) && (
              <div className="form-error" role="alert">{t('servers.saveFailed')}</div>
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
    </div>
  );
}

export default Servers;
