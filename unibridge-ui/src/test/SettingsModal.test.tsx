vi.mock('../keycloak', () => ({
  default: {
    createAccountUrl: () => 'https://auth.example.com/account',
  },
}));

import { screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SettingsModal from '../components/SettingsModal';
import { renderWithProviders } from './helpers';

describe('SettingsModal', () => {
  it('renders settings as an accessible dialog', () => {
    renderWithProviders(<SettingsModal onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog', { name: 'Settings' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });
});
