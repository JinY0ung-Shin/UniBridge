import { useState, type KeyboardEvent } from 'react';
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

  function focusTab(tab: AlertSettingsTab) {
    window.requestAnimationFrame(() => {
      document.getElementById(`alert-settings-tab-${tab}`)?.focus();
    });
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, tab: AlertSettingsTab) {
    const currentIndex = tabs.findIndex((item) => item.key === tab);
    const nextIndex = (() => {
      if (event.key === 'ArrowRight') return (currentIndex + 1) % tabs.length;
      if (event.key === 'ArrowLeft') return (currentIndex - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') return 0;
      if (event.key === 'End') return tabs.length - 1;
      return null;
    })();

    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = tabs[nextIndex].key;
    setActiveTab(nextTab);
    focusTab(nextTab);
  }

  return (
    <div className="alert-settings">
      <div className="page-header">
        <div>
          <h1>{t('alerts.settingsTitle')}</h1>
          <p className="page-subtitle">{t('alerts.settingsSubtitle')}</p>
        </div>
      </div>

      <div className="alert-tabs" role="tablist" aria-label={t('alerts.settingsTitle')}>
        {tabs.map((tab) => (
          <button
            key={tab.key}
            id={`alert-settings-tab-${tab.key}`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            aria-controls={`alert-settings-panel-${tab.key}`}
            tabIndex={activeTab === tab.key ? 0 : -1}
            className={`alert-tab${activeTab === tab.key ? ' alert-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
            onKeyDown={(event) => handleTabKeyDown(event, tab.key)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      {activeTab === 'recipients' && (
        <div
          id="alert-settings-panel-recipients"
          role="tabpanel"
          aria-labelledby="alert-settings-tab-recipients"
        >
          <AlertRecipientsPanel />
        </div>
      )}
      {activeTab === 'delivery' && (
        <div
          id="alert-settings-panel-delivery"
          role="tabpanel"
          aria-labelledby="alert-settings-tab-delivery"
        >
          <AlertDeliveryPanel />
        </div>
      )}
    </div>
  );
}

export default AlertSettings;
