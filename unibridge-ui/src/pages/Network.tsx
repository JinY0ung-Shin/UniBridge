import type { KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useCanWrite } from '../components/useCanWrite';
import OverviewTab from './network/OverviewTab';
import RegionsTab from './network/RegionsTab';
import HostsTab from './network/HostsTab';
import OpeningsTab from './network/OpeningsTab';
import UsagesTab from './network/UsagesTab';
import AccessCheckTab from './network/AccessCheckTab';
import SettingsTab from './network/SettingsTab';
import './Connections.css';
import './Network.css';

const TABS = [
  { key: 'overview', Component: OverviewTab },
  { key: 'regions', Component: RegionsTab },
  { key: 'hosts', Component: HostsTab },
  { key: 'openings', Component: OpeningsTab },
  { key: 'usages', Component: UsagesTab },
  { key: 'check', Component: AccessCheckTab },
  // People with network.write only: the platform host drives reachability flags.
  { key: 'settings', Component: SettingsTab, writeOnly: true },
] as const;

type TabKey = (typeof TABS)[number]['key'];

function Network() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const [searchParams, setSearchParams] = useSearchParams();
  const tabs = TABS.filter((tab) => canWrite || !('writeOnly' in tab));
  const requested = searchParams.get('tab');
  const active = tabs.find((tab) => tab.key === requested) ?? tabs[0];

  function select(key: TabKey) {
    setSearchParams(key === 'overview' ? {} : { tab: key }, { replace: true });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const index = tabs.findIndex((tab) => tab.key === active.key);
    const next = (() => {
      if (event.key === 'ArrowRight') return (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') return (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') return 0;
      if (event.key === 'End') return tabs.length - 1;
      return null;
    })();
    if (next === null) return;
    event.preventDefault();
    const key = tabs[next].key;
    select(key);
    window.requestAnimationFrame(() => document.getElementById(`network-tab-${key}`)?.focus());
  }

  const ActiveTab = active.Component;

  return (
    <div className="connections network-page">
      <div className="page-header">
        <div>
          <h1>{t('network.title')}</h1>
          <p className="page-subtitle">{t('network.subtitle')}</p>
        </div>
      </div>

      <div className="network-tabs" role="tablist" aria-label={t('network.title')}>
        {tabs.map((tab) => (
          <button
            key={tab.key}
            id={`network-tab-${tab.key}`}
            type="button"
            role="tab"
            aria-selected={active.key === tab.key}
            aria-controls={`network-panel-${tab.key}`}
            tabIndex={active.key === tab.key ? 0 : -1}
            className={`network-tab${active.key === tab.key ? ' network-tab--active' : ''}`}
            onClick={() => select(tab.key)}
            onKeyDown={handleKeyDown}
          >
            {t(`network.tabs.${tab.key}`)}
          </button>
        ))}
      </div>

      <div id={`network-panel-${active.key}`} role="tabpanel" aria-labelledby={`network-tab-${active.key}`}>
        <ActiveTab />
      </div>
    </div>
  );
}

export default Network;
