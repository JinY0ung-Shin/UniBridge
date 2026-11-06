import { useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  createFirewallOpening,
  createNetworkHost,
  createPortUsage,
  updateFirewallOpening,
  updateNetworkHost,
  updatePortUsage,
  type FirewallOpening,
  type FirewallOpeningInput,
  type NetworkHost,
  type NetworkHostInput,
  type NetworkHostKind,
  type NetworkProtocol,
  type OpeningStatus,
  type PortUsage,
  type PortUsageInput,
} from '../../api/network';
import { getServers } from '../../api/client';
import ResourceModal from '../../components/ResourceModal';
import { usePermissions } from '../../components/usePermissions';
import { NETWORK_KEY, intOrUndefined, networkErrorMessage } from './networkUtils';
import { useAllNetworkHosts, useRegions } from './useNetworkActions';

/** Save mutation for a modal form: shows the backend's reason inside the form. */
function useFormSave<TVars>(mutationFn: (vars: TVars) => Promise<unknown>, onDone: () => void) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn,
    onMutate: () => setError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: NETWORK_KEY });
      onDone();
    },
    onError: (err) => setError(networkErrorMessage(err, t('network.saveFailed'))),
  });
  return { mutation, error };
}

interface FormShellProps {
  title: string;
  error: string | null;
  saving: boolean;
  isEdit: boolean;
  onClose: () => void;
  onSubmit: () => void;
  children: ReactNode;
}

function FormShell({ title, error, saving, isEdit, onClose, onSubmit, children }: FormShellProps) {
  const { t } = useTranslation();
  return (
    <ResourceModal title={title} onClose={onClose} closeLabel={t('common.close')}>
      <form onSubmit={(e: FormEvent) => { e.preventDefault(); onSubmit(); }}>
        <div className="form-grid">{children}</div>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>{t('common.cancel')}</button>
          <button type="submit" className="btn btn-primary" disabled={saving} aria-busy={saving}>
            {saving ? t('common.saving') : isEdit ? t('common.save') : t('common.create')}
          </button>
        </div>
      </form>
    </ResourceModal>
  );
}

function hostOptionLabel(h: NetworkHost): string {
  return `${h.name} (${h.ip})${h.region_name ? ` · ${h.region_name}` : ''}`;
}

/* ── Firewall opening ── */

interface OpeningFormModalProps {
  opening?: FirewallOpening | null;
  /** Prefill for a new opening (e.g. from a host detail page). */
  defaults?: { src_host_id?: number; dst_host_id?: number };
  onClose: () => void;
}

export function OpeningFormModal({ opening, defaults, onClose }: OpeningFormModalProps) {
  const { t } = useTranslation();
  const hostsQuery = useAllNetworkHosts();
  const hosts = hostsQuery.data ?? [];
  const isEdit = opening != null;

  const [srcMode, setSrcMode] = useState<'host' | 'cidr'>(opening?.src_cidr ? 'cidr' : 'host');
  // A new opening may name a destination IP no host has: the backend registers
  // it as an external host (dst-<ip>). Editing keeps to registered hosts.
  const [dstMode, setDstMode] = useState<'host' | 'ip'>('host');
  const [form, setForm] = useState({
    src_host_id: String(opening?.src_host_id ?? defaults?.src_host_id ?? ''),
    src_cidr: opening?.src_cidr ?? '',
    dst_host_id: String(opening?.dst_host_id ?? defaults?.dst_host_id ?? ''),
    dst_ip: '',
    port_from: opening ? String(opening.port_from) : '',
    port_to: opening && opening.port_to !== opening.port_from ? String(opening.port_to) : '',
    protocol: (opening?.protocol ?? 'tcp') as NetworkProtocol,
    status: (opening?.status ?? 'open') as OpeningStatus,
    ticket_no: opening?.ticket_no ?? '',
    requester: opening?.requester ?? '',
    purpose: opening?.purpose ?? '',
  });

  const { mutation, error } = useFormSave(
    (body: FirewallOpeningInput) => (opening ? updateFirewallOpening(opening.id, body) : createFirewallOpening(body)),
    onClose,
  );

  function submit() {
    const portFrom = intOrUndefined(form.port_from);
    const body: FirewallOpeningInput = {
      ...(srcMode === 'cidr'
        ? { src_cidr: form.src_cidr.trim() }
        : { src_host_id: intOrUndefined(form.src_host_id) }),
      ...(dstMode === 'ip'
        ? { dst_host: form.dst_ip.trim(), create_missing_destination: true }
        : { dst_host_id: intOrUndefined(form.dst_host_id) }),
      port_from: portFrom,
      port_to: intOrUndefined(form.port_to) ?? portFrom,
      protocol: form.protocol,
      status: form.status,
      ticket_no: form.ticket_no.trim(),
      requester: form.requester.trim(),
      purpose: form.purpose.trim(),
    };
    mutation.mutate(body);
  }

  return (
    <FormShell
      title={isEdit ? t('network.openings.edit') : t('network.openings.add')}
      error={error}
      saving={mutation.isPending}
      isEdit={isEdit}
      onClose={onClose}
      onSubmit={submit}
    >
      <div className="form-group form-group--full" role="radiogroup" aria-label={t('network.openings.sourceType')}>
        <label>{t('network.openings.sourceType')}</label>
        <div className="network-inline">
          <label className="method-check">
            <input type="radio" name="opening-src-mode" checked={srcMode === 'host'} onChange={() => setSrcMode('host')} />
            {t('network.openings.sourceHost')}
          </label>
          <label className="method-check">
            <input type="radio" name="opening-src-mode" checked={srcMode === 'cidr'} onChange={() => setSrcMode('cidr')} />
            {t('network.openings.sourceCidr')}
          </label>
        </div>
      </div>
      {srcMode === 'host' ? (
        <div className="form-group form-group--full">
          <label htmlFor="opening-src-host">{t('network.openings.source')}</label>
          <select
            id="opening-src-host"
            value={form.src_host_id}
            onChange={(e) => setForm({ ...form, src_host_id: e.target.value })}
            required
          >
            <option value="">{t('network.selectHost')}</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{hostOptionLabel(h)}</option>)}
          </select>
        </div>
      ) : (
        <div className="form-group form-group--full">
          <label htmlFor="opening-src-cidr">
            {t('network.openings.sourceCidr')} <span className="hint">{t('network.openings.cidrHint')}</span>
          </label>
          <input
            id="opening-src-cidr"
            value={form.src_cidr}
            onChange={(e) => setForm({ ...form, src_cidr: e.target.value })}
            placeholder="10.20.0.0/16"
            required
          />
        </div>
      )}
      {!isEdit && (
        <div className="form-group form-group--full" role="radiogroup" aria-label={t('network.openings.destinationType')}>
          <label>{t('network.openings.destinationType')}</label>
          <div className="network-inline">
            <label className="method-check">
              <input type="radio" name="opening-dst-mode" checked={dstMode === 'host'} onChange={() => setDstMode('host')} />
              {t('network.openings.destinationHost')}
            </label>
            <label className="method-check">
              <input type="radio" name="opening-dst-mode" checked={dstMode === 'ip'} onChange={() => setDstMode('ip')} />
              {t('network.openings.destinationIp')}
            </label>
          </div>
        </div>
      )}
      {dstMode === 'host' ? (
        <div className="form-group form-group--full">
          <label htmlFor="opening-dst-host">{t('network.openings.destination')}</label>
          <select
            id="opening-dst-host"
            value={form.dst_host_id}
            onChange={(e) => setForm({ ...form, dst_host_id: e.target.value })}
            required
          >
            <option value="">{t('network.selectHost')}</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{hostOptionLabel(h)}</option>)}
          </select>
        </div>
      ) : (
        <div className="form-group form-group--full">
          <label htmlFor="opening-dst-ip">
            {t('network.openings.destinationIp')} <span className="hint">{t('network.openings.destinationIpHint')}</span>
          </label>
          <input
            id="opening-dst-ip"
            value={form.dst_ip}
            onChange={(e) => setForm({ ...form, dst_ip: e.target.value })}
            placeholder="10.166.130.10"
            required
          />
        </div>
      )}
      <div className="form-group">
        <label htmlFor="opening-port-from">{t('network.openings.portFrom')}</label>
        <input
          id="opening-port-from"
          type="number"
          min={1}
          max={65535}
          value={form.port_from}
          onChange={(e) => setForm({ ...form, port_from: e.target.value })}
          required
        />
      </div>
      <div className="form-group">
        <label htmlFor="opening-port-to">
          {t('network.openings.portTo')} <span className="hint">{t('network.openings.portToHint')}</span>
        </label>
        <input
          id="opening-port-to"
          type="number"
          min={1}
          max={65535}
          value={form.port_to}
          onChange={(e) => setForm({ ...form, port_to: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label htmlFor="opening-protocol">{t('network.protocol')}</label>
        <select
          id="opening-protocol"
          value={form.protocol}
          onChange={(e) => setForm({ ...form, protocol: e.target.value as NetworkProtocol })}
        >
          <option value="tcp">tcp</option>
          <option value="udp">udp</option>
        </select>
      </div>
      <div className="form-group">
        <label htmlFor="opening-status">{t('network.openings.status')}</label>
        <select
          id="opening-status"
          value={form.status}
          onChange={(e) => setForm({ ...form, status: e.target.value as OpeningStatus })}
        >
          <option value="open">{t('network.status.open')}</option>
          <option value="closed">{t('network.status.closed')}</option>
        </select>
      </div>
      <div className="form-group">
        <label htmlFor="opening-ticket">{t('network.openings.ticket')}</label>
        <input id="opening-ticket" value={form.ticket_no} onChange={(e) => setForm({ ...form, ticket_no: e.target.value })} />
      </div>
      <div className="form-group">
        <label htmlFor="opening-requester">{t('network.openings.requester')}</label>
        <input id="opening-requester" value={form.requester} onChange={(e) => setForm({ ...form, requester: e.target.value })} />
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="opening-purpose">{t('network.openings.purpose')}</label>
        <textarea id="opening-purpose" rows={2} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} />
      </div>
    </FormShell>
  );
}

/* ── Port usage ── */

interface UsageFormModalProps {
  usage?: PortUsage | null;
  defaults?: { host_id?: number };
  onClose: () => void;
}

export function UsageFormModal({ usage, defaults, onClose }: UsageFormModalProps) {
  const { t } = useTranslation();
  const hostsQuery = useAllNetworkHosts();
  const hosts = hostsQuery.data ?? [];
  const isEdit = usage != null && usage.id != null;

  const [form, setForm] = useState({
    host_id: String(usage?.host_id ?? defaults?.host_id ?? ''),
    port: usage ? String(usage.port) : '',
    protocol: (usage?.protocol ?? 'tcp') as NetworkProtocol,
    service_name: usage?.service_name ?? '',
    description: usage?.description ?? '',
    owner: usage?.owner ?? '',
  });

  const { mutation, error } = useFormSave(
    (body: PortUsageInput) => (usage?.id != null ? updatePortUsage(usage.id, body) : createPortUsage(body)),
    onClose,
  );

  function submit() {
    const body: PortUsageInput = {
      port: intOrUndefined(form.port),
      protocol: form.protocol,
      service_name: form.service_name.trim(),
      description: form.description.trim(),
      owner: form.owner.trim(),
    };
    // The host of a stored usage cannot change; only a new one names it.
    mutation.mutate(isEdit ? body : { host_id: intOrUndefined(form.host_id), ...body });
  }

  return (
    <FormShell
      title={isEdit ? t('network.usages.edit') : t('network.usages.add')}
      error={error}
      saving={mutation.isPending}
      isEdit={isEdit}
      onClose={onClose}
      onSubmit={submit}
    >
      <div className="form-group form-group--full">
        <label htmlFor="usage-host">{t('network.usages.host')}</label>
        <select
          id="usage-host"
          value={form.host_id}
          disabled={isEdit}
          onChange={(e) => setForm({ ...form, host_id: e.target.value })}
          required
        >
          <option value="">{t('network.selectHost')}</option>
          {hosts.map((h) => <option key={h.id} value={h.id}>{hostOptionLabel(h)}</option>)}
        </select>
      </div>
      <div className="form-group">
        <label htmlFor="usage-port">{t('network.usages.port')}</label>
        <input
          id="usage-port"
          type="number"
          min={1}
          max={65535}
          value={form.port}
          onChange={(e) => setForm({ ...form, port: e.target.value })}
          required
        />
      </div>
      <div className="form-group">
        <label htmlFor="usage-protocol">{t('network.protocol')}</label>
        <select
          id="usage-protocol"
          value={form.protocol}
          onChange={(e) => setForm({ ...form, protocol: e.target.value as NetworkProtocol })}
        >
          <option value="tcp">tcp</option>
          <option value="udp">udp</option>
        </select>
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="usage-service">{t('network.usages.service')}</label>
        <input
          id="usage-service"
          value={form.service_name}
          onChange={(e) => setForm({ ...form, service_name: e.target.value })}
          placeholder="postgres"
          required
        />
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="usage-description">{t('network.description')}</label>
        <input id="usage-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="usage-owner">{t('network.owner')}</label>
        <input id="usage-owner" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} />
      </div>
    </FormShell>
  );
}

/* ── Host ── */

interface HostFormModalProps {
  host?: NetworkHost | null;
  onClose: () => void;
}

export function HostFormModal({ host, onClose }: HostFormModalProps) {
  const { t } = useTranslation();
  const isEdit = host != null;
  const regionsQuery = useRegions();
  const regions = regionsQuery.data ?? [];
  // Monitored servers are listed by another feature's permission; without it
  // the current link is shown read-only and sent back unchanged.
  const canSeeServers = usePermissions().permissions.includes('servers.read');
  const serversQuery = useQuery({ queryKey: ['servers'], queryFn: getServers, enabled: canSeeServers });

  const [form, setForm] = useState({
    name: host?.name ?? '',
    ip: host?.ip ?? '',
    region_id: host?.region_id != null ? String(host.region_id) : '',
    description: host?.description ?? '',
    owner: host?.owner ?? '',
    kind: (host?.kind ?? 'managed') as NetworkHostKind,
    monitored_host_id: host?.monitored_host_id != null ? String(host.monitored_host_id) : '',
  });
  const external = form.kind === 'external';

  const { mutation, error } = useFormSave(
    (body: NetworkHostInput) => (host ? updateNetworkHost(host.id, body) : createNetworkHost(body)),
    onClose,
  );

  function submit() {
    // An external host has no region and no monitored link.
    const regionId = external ? undefined : intOrUndefined(form.region_id);
    const body: NetworkHostInput = {
      name: form.name.trim(),
      ip: form.ip.trim(),
      description: form.description.trim(),
      owner: form.owner.trim(),
      kind: form.kind,
      monitored_host_id: external ? null : intOrUndefined(form.monitored_host_id) ?? null,
    };
    // New host without a region: the backend places it by the regions' CIDRs.
    // Editing to "none" sends an explicit null, which unassigns it.
    if (regionId !== undefined) body.region_id = regionId;
    else if (isEdit) body.region_id = null;
    mutation.mutate(body);
  }

  return (
    <FormShell
      title={isEdit ? t('network.hosts.edit') : t('network.hosts.add')}
      error={error}
      saving={mutation.isPending}
      isEdit={isEdit}
      onClose={onClose}
      onSubmit={submit}
    >
      <div className="form-group">
        <label htmlFor="host-name">{t('network.hosts.name')}</label>
        <input id="host-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
      </div>
      <div className="form-group">
        <label htmlFor="host-ip">{t('network.hosts.ip')}</label>
        <input id="host-ip" value={form.ip} onChange={(e) => setForm({ ...form, ip: e.target.value })} placeholder="10.0.0.5" required />
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="host-kind">{t('network.kind.label')}</label>
        <select
          id="host-kind"
          value={form.kind}
          onChange={(e) => setForm({ ...form, kind: e.target.value as NetworkHostKind })}
        >
          <option value="managed">{t('network.kind.managed')}</option>
          <option value="external">{t('network.kind.external')}</option>
        </select>
        {external && <div className="network-sub">{t('network.kind.externalHint')}</div>}
        {external && form.monitored_host_id !== '' && (
          <div className="network-note" role="note">{t('network.kind.unlinkNote')}</div>
        )}
      </div>
      {!external && (
        <>
          <div className="form-group form-group--full">
            <label htmlFor="host-region">{t('network.region')}</label>
            <select id="host-region" value={form.region_id} onChange={(e) => setForm({ ...form, region_id: e.target.value })}>
              <option value="">{isEdit ? t('network.hosts.regionNone') : t('network.hosts.regionAuto')}</option>
              {regions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="host-monitored">{t('network.hosts.monitored')}</label>
            {canSeeServers ? (
              <select
                id="host-monitored"
                value={form.monitored_host_id}
                onChange={(e) => setForm({ ...form, monitored_host_id: e.target.value })}
              >
                <option value="">{t('network.hosts.monitoredNone')}</option>
                {(serversQuery.data ?? []).map((s) => <option key={s.id} value={s.id}>{`${s.name} (${s.address})`}</option>)}
              </select>
            ) : (
              <input id="host-monitored" value={host?.monitored_host_name ?? ''} disabled />
            )}
          </div>
        </>
      )}
      <div className="form-group form-group--full">
        <label htmlFor="host-description">{t('network.description')}</label>
        <input id="host-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </div>
      <div className="form-group form-group--full">
        <label htmlFor="host-owner">{t('network.owner')}</label>
        <input id="host-owner" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} />
      </div>
    </FormShell>
  );
}
