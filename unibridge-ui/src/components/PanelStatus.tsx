import { useTranslation } from 'react-i18next';

interface PanelStatusProps {
  loading?: boolean;
  error?: boolean;
  emptyText: string;
}

/**
 * Placeholder body for a chart panel without data, distinguishing the three
 * states that previously all rendered as "no data": still loading, the
 * panel's query failed, and a genuinely empty result.
 */
function PanelStatus({ loading, error, emptyText }: PanelStatusProps) {
  const { t } = useTranslation();
  if (loading) {
    return <div className="no-data" role="status">{t('monitoring.panelLoading')}</div>;
  }
  if (error) {
    return <div className="no-data no-data--error" role="alert">{t('monitoring.panelLoadFailed')}</div>;
  }
  return <div className="no-data">{emptyText}</div>;
}

export default PanelStatus;
