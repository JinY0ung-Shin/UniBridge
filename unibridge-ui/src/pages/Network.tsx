import type { ComponentType, KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useCanWrite } from '../components/useCanWrite';
import OverviewTab from './network/OverviewTab';
import RegionsTab from './network/RegionsTab';
import HostsTab from './network/HostsTab';
import OpeningsTab from './network/OpeningsTab';
import UsagesTab from './network/UsagesTab';
import ProbesTab from './network/ProbesTab';
import AccessCheckTab from './network/AccessCheckTab';
import PortFinderTab from './network/PortFinderTab';
import SettingsTab from './network/SettingsTab';
import './Connections.css';
import './Network.css';

interface Section {
  /** The `?tab=` value; links across the app point at these, so they stay stable. */
  key: string;
  Component: ComponentType;
}

interface Group {
  key: 'map' | 'tools' | 'records' | 'settings';
  sections: Section[];
  writeOnly?: boolean;
}

// Four top-level tabs; a group with several sections shows them as a second row.
const GROUPS: Group[] = [
  { key: 'map', sections: [{ key: 'overview', Component: OverviewTab }] },
  {
    key: 'tools',
    sections: [
      { key: 'finder', Component: PortFinderTab },
      { key: 'check', Component: AccessCheckTab },
    ],
  },
  {
    key: 'records',
    sections: [
      { key: 'hosts', Component: HostsTab },
      { key: 'regions', Component: RegionsTab },
      { key: 'openings', Component: OpeningsTab },
      { key: 'usages', Component: UsagesTab },
      { key: 'probes', Component: ProbesTab },
    ],
  },
  // People with network.write only: the platform host drives reachability flags.
  { key: 'settings', sections: [{ key: 'settings', Component: SettingsTab }], writeOnly: true },
];

/** Arrow/Home/End movement inside a tablist; null when the key is not one of them. */
function nextIndex(event: KeyboardEvent, index: number, length: number): number | null {
  if (event.key === 'ArrowRight') return (index + 1) % length;
  if (event.key === 'ArrowLeft') return (index - 1 + length) % length;
  if (event.key === 'Home') return 0;
  if (event.key === 'End') return length - 1;
  return null;
}

function Network() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('network.write');
  const [searchParams, setSearchParams] = useSearchParams();
  const groups = GROUPS.filter((group) => canWrite || !group.writeOnly);
  const requested = searchParams.get('tab');
  const group = groups.find((g) => g.sections.some((s) => s.key === requested)) ?? groups[0];
  const section = group.sections.find((s) => s.key === requested) ?? group.sections[0];

  function select(key: string, focusId?: string) {
    setSearchParams(key === 'overview' ? {} : { tab: key }, { replace: true });
    if (focusId) window.requestAnimationFrame(() => document.getElementById(focusId)?.focus());
  }

  function handleGroupKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const next = nextIndex(event, groups.indexOf(group), groups.length);
    if (next === null) return;
    event.preventDefault();
    select(groups[next].sections[0].key, `network-tab-${groups[next].key}`);
  }

  function handleSectionKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const next = nextIndex(event, group.sections.indexOf(section), group.sections.length);
    if (next === null) return;
    event.preventDefault();
    select(group.sections[next].key, `network-subtab-${group.sections[next].key}`);
  }

  const ActiveSection = section.Component;
  const hasSections = group.sections.length > 1;
  // One panel: the section row, when there is one, sits between the tabs and it.
  const panelId = `network-panel-${section.key}`;

  return (
    <div className="connections network-page">
      <div className="page-header">
        <div>
          <h1>{t('network.title')}</h1>
          <p className="page-subtitle">{t('network.subtitle')}</p>
        </div>
      </div>

      <div className="network-tabs" role="tablist" aria-label={t('network.title')}>
        {groups.map((g) => (
          <button
            key={g.key}
            id={`network-tab-${g.key}`}
            type="button"
            role="tab"
            aria-selected={group.key === g.key}
            aria-controls={group.key === g.key ? panelId : undefined}
            tabIndex={group.key === g.key ? 0 : -1}
            className={`network-tab${group.key === g.key ? ' network-tab--active' : ''}`}
            onClick={() => select(g.sections[0].key)}
            onKeyDown={handleGroupKeyDown}
          >
            {t(`network.groups.${g.key}`)}
          </button>
        ))}
      </div>

      {hasSections && (
        <div className="network-subtabs" role="tablist" aria-label={t(`network.groups.${group.key}`)}>
          {group.sections.map((s) => (
            <button
              key={s.key}
              id={`network-subtab-${s.key}`}
              type="button"
              role="tab"
              aria-selected={section.key === s.key}
              aria-controls={panelId}
              tabIndex={section.key === s.key ? 0 : -1}
              className={`network-subtab${section.key === s.key ? ' network-subtab--active' : ''}`}
              onClick={() => select(s.key)}
              onKeyDown={handleSectionKeyDown}
            >
              {t(`network.tabs.${s.key}`)}
            </button>
          ))}
        </div>
      )}

      <div
        id={panelId}
        role="tabpanel"
        aria-labelledby={hasSections ? `network-subtab-${section.key}` : `network-tab-${group.key}`}
      >
        <ActiveSection />
      </div>
    </div>
  );
}

export default Network;
