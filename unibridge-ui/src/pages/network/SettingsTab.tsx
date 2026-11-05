import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getNetworkSettings, updateNetworkSettings, type NetworkSettings } from '../../api/network';
import { useToast } from '../../components/useToast';
import { NETWORK_KEY, intOrUndefined } from './networkUtils';
import { useAllNetworkHosts, useNetworkMutation } from './useNetworkActions';

function SettingsForm({ settings }: { settings: NetworkSettings }) {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const hosts = useAllNetworkHosts().data ?? [];
  const [platformHost, setPlatformHost] = useState(settings.platform_host_id != null ? String(settings.platform_host_id) : '');
  const save = useNetworkMutation(updateNetworkSettings, 'network.saveFailed', () =>
    addToast({ type: 'success', title: t('network.settings.saved') }),
  );

  return (
    <form
      className="network-settings"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate({ platform_host_id: intOrUndefined(platformHost) ?? null });
      }}
    >
      <div className="form-group">
        <label htmlFor="network-platform-host">
          {t('network.settings.platformHost')}{' '}
          <span id="network-platform-host-hint" className="hint">{t('network.settings.platformHostHint')}</span>
        </label>
        <select
          id="network-platform-host"
          value={platformHost}
          onChange={(e) => setPlatformHost(e.target.value)}
          aria-describedby="network-platform-host-hint"
        >
          <option value="">{t('network.settings.platformHostNone')}</option>
          {hosts.map((h) => <option key={h.id} value={h.id}>{`${h.name} (${h.ip})`}</option>)}
        </select>
      </div>
      <div className="modal-actions">
        <button type="submit" className="btn btn-primary" disabled={save.isPending} aria-busy={save.isPending}>
          {save.isPending ? t('common.saving') : t('common.save')}
        </button>
      </div>
    </form>
  );
}

function SettingsTab() {
  const { t } = useTranslation();
  const settingsQuery = useQuery({ queryKey: [...NETWORK_KEY, 'settings'], queryFn: getNetworkSettings });

  if (settingsQuery.isLoading) return <div className="loading-message" role="status">{t('common.loading')}</div>;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>;
  }
  // Keyed so a refetched value resets the form instead of syncing state in an effect.
  return <SettingsForm key={String(settingsQuery.data.platform_host_id)} settings={settingsQuery.data} />;
}

export default SettingsTab;
