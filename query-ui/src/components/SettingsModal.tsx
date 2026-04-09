import { useTranslation } from 'react-i18next';
import './SettingsModal.css';

interface SettingsModalProps {
  onClose: () => void;
}

function SettingsModal({ onClose }: SettingsModalProps) {
  const { t, i18n } = useTranslation();

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
