import { useTranslation } from 'react-i18next';
import { useTheme } from './ThemeContext';
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
        </div>
      </div>
    </div>
  );
}

export default SettingsModal;
