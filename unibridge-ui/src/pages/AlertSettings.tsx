import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import AlertMailChannelPanel from './alerts/AlertMailChannelPanel';
import AlertOwnerGroupsPanel from './alerts/AlertOwnerGroupsPanel';
import AlertResourceOwnersPanel from './alerts/AlertResourceOwnersPanel';
import AlertRulesPanel from './alerts/AlertRulesPanel';
import './AlertSettings.css';

type AlertSettingsTab = 'mail' | 'owner-groups' | 'resource-owners' | 'rules';

const tabs: Array<{ key: AlertSettingsTab; labelKey: string }> = [
  { key: 'mail', labelKey: 'alerts.mailChannelTab' },
  { key: 'owner-groups', labelKey: 'alerts.ownerGroupsTab' },
  { key: 'resource-owners', labelKey: 'alerts.resourceOwnersTab' },
  { key: 'rules', labelKey: 'alerts.rulesTab' },
];

function AlertSettings() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<AlertSettingsTab>('mail');

  return (
    <div className="alert-settings">
      <div className="page-header">
        <div>
          <h1>{t('alerts.settingsTitle')}</h1>
          <p className="page-subtitle">{t('alerts.settingsSubtitle')}</p>
        </div>
      </div>

      <div className="alert-tabs" aria-label={t('alerts.settingsTitle')}>
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={`alert-tab${activeTab === tab.key ? ' alert-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      {activeTab === 'mail' && <AlertMailChannelPanel />}
      {activeTab === 'owner-groups' && <AlertOwnerGroupsPanel />}
      {activeTab === 'resource-owners' && <AlertResourceOwnersPanel />}
      {activeTab === 'rules' && <AlertRulesPanel />}
    </div>
  );
}

export default AlertSettings;
