vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getS3Connections: vi.fn(),
  createS3Connection: vi.fn(),
  updateS3Connection: vi.fn(),
  deleteS3Connection: vi.fn(),
  testS3Connection: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getS3Connections } from '../api/client';
import S3Connections from '../pages/S3Connections';
import { renderWithProviders, makeS3Connection } from './helpers';

const mockedGetS3Connections = vi.mocked(getS3Connections);

describe('S3Connections', () => {
  beforeEach(() => {
    mockedGetS3Connections.mockResolvedValue([]);
  });

  it('hides write actions for users with read-only S3 connection permission', async () => {
    mockedGetS3Connections.mockResolvedValue([makeS3Connection()]);

    renderWithProviders(<S3Connections />, {
      permissions: ['s3.connections.read', 's3.browse'],
    });

    await waitFor(() => {
      expect(screen.getByText('s3-main')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add S3 Connection' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Test' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'cURL' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Browse' })).toBeInTheDocument();
  });
});
