import { vi, describe, expect, it, beforeEach } from 'vitest';

vi.mock('../keycloak', () => ({
  default: {
    createAccountUrl: () => 'https://auth.example.com/account',
  },
}));

import { fireEvent, screen } from '@testing-library/react';
import SettingsModal from '../components/SettingsModal';
import { renderWithProviders } from './helpers';
import i18n from '../i18n';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  i18n.changeLanguage('en');
});

describe('SettingsModal interactions', () => {
  it('switches theme to light when light theme button is clicked', () => {
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);
    const lightBtn = screen.getByRole('button', { name: /Light/i });
    fireEvent.click(lightBtn);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('switches theme to dark and removes data-theme attribute', () => {
    localStorage.setItem('theme', 'light');
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);
    const darkBtn = screen.getByRole('button', { name: /Dark/i });
    fireEvent.click(darkBtn);
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('switches language via radio inputs', () => {
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);
    const koRadio = screen.getByDisplayValue('ko');
    fireEvent.click(koRadio);
    expect(i18n.language).toBe('ko');

    const enRadio = screen.getByDisplayValue('en');
    fireEvent.click(enRadio);
    expect(i18n.language).toBe('en');
  });

  it('renders a change-password external link', () => {
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);
    const links = screen.getAllByRole('link');
    expect(links.some((l) => l.getAttribute('href')?.includes('account'))).toBe(true);
  });

  it('calls onClose when the close button is clicked', () => {
    const onClose = vi.fn();
    renderWithProviders(<SettingsModal onClose={onClose} />);
    const closeBtn = screen.getByRole('button', { name: /close/i });
    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalled();
  });

  it('marks currently selected theme button as active', () => {
    localStorage.setItem('theme', 'light');
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);
    const lightBtn = screen.getByRole('button', { name: /Light/i });
    expect(lightBtn.className).toMatch(/active/);
    expect(lightBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /Dark/i })).toHaveAttribute('aria-pressed', 'false');
  });
});
