import { useTranslation } from 'react-i18next';
import { useTheme } from './useTheme';
import keycloak from '../keycloak';
import './SettingsModal.css';

interface SettingsModalProps {
  onClose: () => void;
}

const THEME_OPTIONS = [
  { value: 'dark' as const, labelKey: 'settings.themeDark', icon: '🌙' },
  { value: 'light' as const, labelKey: 'settings.themeLight', icon: '☀️' },
  { value: 'system' as const, labelKey: 'settings.themeSystem', icon: '💻' },
];

function SettingsModal({ onClose }: SettingsModalProps) {
  const { t, i18n } = useTranslation();
  const { theme, setTheme } = useTheme();

  function handleLanguageChange(lng: string) {
    i18n.changeLanguage(lng);
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t('settings.title')}</h2>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="settings-body">
          {/* Theme */}
          <div className="settings-section">
            <div className="settings-label">{t('settings.theme')}</div>
            <p className="settings-desc">{t('settings.themeDesc')}</p>
            <div className="theme-options">
              {THEME_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  className={`theme-option ${theme === opt.value ? 'theme-option--active' : ''}`}
                  onClick={() => setTheme(opt.value)}
                >
                  <span className="theme-option-icon">{opt.icon}</span>
                  <span className="theme-option-label">{t(opt.labelKey)}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Language */}
          <div className="settings-section">
            <div className="settings-label">{t('settings.language')}</div>
            <p className="settings-desc">{t('settings.languageDesc')}</p>
            <div className="language-options">
              <label className="language-option">
                <input
                  type="radio"
                  name="language"
                  value="ko"
                  checked={i18n.language === 'ko'}
                  onChange={() => handleLanguageChange('ko')}
                />
                <span className="language-option-text">{t('settings.korean')}</span>
              </label>
              <label className="language-option">
                <input
                  type="radio"
                  name="language"
                  value="en"
                  checked={i18n.language === 'en'}
                  onChange={() => handleLanguageChange('en')}
                />
                <span className="language-option-text">{t('settings.english')}</span>
              </label>
            </div>
          </div>

          {/* Security */}
          <div className="settings-section">
            <div className="settings-label">{t('settings.security')}</div>
            <p className="settings-desc">{t('settings.securityDesc')}</p>
            <a
              className="settings-change-password-link"
              href={`${keycloak.createAccountUrl({ redirectUri: window.location.href })}#/security/signingin`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <rect x="3" y="7" width="10" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
                <path d="M5 7V5a3 3 0 016 0v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
              <span>{t('settings.changePassword')}</span>
              <svg className="settings-external-icon" width="12" height="12" viewBox="0 0 12 12" fill="none">
                <path d="M5 1H2.5A1.5 1.5 0 001 2.5v7A1.5 1.5 0 002.5 11h7A1.5 1.5 0 0011 9.5V7M7 1h4v4M11 1L5.5 6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </a>
            <p className="settings-desc settings-change-password-desc">
              {t('settings.changePasswordDesc')}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default SettingsModal;
