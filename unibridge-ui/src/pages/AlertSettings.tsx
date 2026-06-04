import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import AlertRecipientsPanel from './alerts/AlertRecipientsPanel';
import AlertDeliveryPanel from './alerts/AlertDeliveryPanel';
import './AlertSettings.css';

type AlertSettingsTab = 'recipients' | 'delivery';

const tabs: Array<{ key: AlertSettingsTab; labelKey: string }> = [
  { key: 'recipients', labelKey: 'alerts.recipientsTab' },
  { key: 'delivery', labelKey: 'alerts.deliveryTab' },
];

function AlertSettings() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<AlertSettingsTab>('recipients');

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

      {activeTab === 'recipients' && <AlertRecipientsPanel />}
      {activeTab === 'delivery' && <AlertDeliveryPanel />}
    </div>
  );
}

export default AlertSettings;
