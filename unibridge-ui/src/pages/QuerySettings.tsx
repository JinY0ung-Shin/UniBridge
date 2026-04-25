import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getQuerySettings, updateQuerySettings, type QuerySettings } from '../api/client';
import './QuerySettings.css';

function settingsKey(settings: QuerySettings): string {
  return [
    settings.rate_limit_per_minute,
    settings.max_concurrent_queries,
    settings.blocked_sql_keywords.join('\0'),
  ].join(':');
}

function QuerySettingsForm({ settings }: { settings: QuerySettings }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [rateLimit, setRateLimit] = useState(settings.rate_limit_per_minute);
  const [maxConcurrent, setMaxConcurrent] = useState(settings.max_concurrent_queries);
  const [blockedKeywords, setBlockedKeywords] = useState(settings.blocked_sql_keywords.join(', '));

  const updateMut = useMutation({
    mutationFn: updateQuerySettings,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['query-settings'] });
    },
  });

  function handleSave() {
    const keywords = blockedKeywords
      .split(',')
      .map((k) => k.trim().toUpperCase())
      .filter((k) => k.length > 0);

    updateMut.mutate({
      rate_limit_per_minute: rateLimit,
      max_concurrent_queries: maxConcurrent,
      blocked_sql_keywords: keywords,
    });
  }

  return (
    <div className="settings-form">
      <div className="settings-card">
        <h3>{t('querySettings.rateLimiting')}</h3>
        <div className="form-group">
          <label>{t('querySettings.rateLimit')}</label>
          <input
            type="number"
            min={1}
            max={1000}
            value={rateLimit}
            onChange={(e) => setRateLimit(Number(e.target.value))}
          />
          <span className="form-hint">{t('querySettings.rateLimitHint')}</span>
        </div>
        <div className="form-group">
          <label>{t('querySettings.maxConcurrent')}</label>
          <input
            type="number"
            min={1}
            max={100}
            value={maxConcurrent}
            onChange={(e) => setMaxConcurrent(Number(e.target.value))}
          />
          <span className="form-hint">{t('querySettings.maxConcurrentHint')}</span>
        </div>
      </div>

      <div className="settings-card">
        <h3>{t('querySettings.sqlBlacklist')}</h3>
        <div className="form-group">
          <label>{t('querySettings.blockedKeywords')}</label>
          <input
            type="text"
            value={blockedKeywords}
            onChange={(e) => setBlockedKeywords(e.target.value)}
            placeholder="VACUUM, ANALYZE, ..."
          />
          <span className="form-hint">{t('querySettings.blockedKeywordsHint')}</span>
        </div>
      </div>

      <button
        className="btn btn-primary"
        onClick={handleSave}
        disabled={updateMut.isPending}
      >
        {updateMut.isPending ? t('common.saving') : t('common.save')}
      </button>

      {updateMut.isSuccess && (
        <span className="save-success">{t('querySettings.saved')}</span>
      )}
      {updateMut.isError && (
        <span className="save-error">{t('querySettings.saveFailed')}</span>
      )}
    </div>
  );
}

function QuerySettingsPage() {
  const { t } = useTranslation();

  const settingsQuery = useQuery({
    queryKey: ['query-settings'],
    queryFn: getQuerySettings,
  });

  return (
    <div className="query-settings">
      <div className="page-header">
        <h1>{t('querySettings.title')}</h1>
        <p className="page-subtitle">{t('querySettings.subtitle')}</p>
      </div>

      {settingsQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}

      {settingsQuery.isError && (
        <div className="error-banner">{t('querySettings.loadFailed')}</div>
      )}

      {settingsQuery.data && (
        <QuerySettingsForm key={settingsKey(settingsQuery.data)} settings={settingsQuery.data} />
      )}
    </div>
  );
}

export default QuerySettingsPage;
