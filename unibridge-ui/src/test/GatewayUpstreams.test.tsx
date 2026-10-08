vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayUpstreams: vi.fn(),
  saveGatewayUpstream: vi.fn(),
  deleteGatewayUpstream: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayUpstreams, saveGatewayUpstream, deleteGatewayUpstream } from '../api/client';
import GatewayUpstreams from '../pages/GatewayUpstreams';
import { renderWithProviders, makeGatewayUpstream } from './helpers';

const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);
const mockedSaveGatewayUpstream = vi.mocked(saveGatewayUpstream);
const mockedDeleteGatewayUpstream = vi.mocked(deleteGatewayUpstream);

describe('GatewayUpstreams', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [], total: 0 });
  });

  it('renders loading state', () => {
    mockedGetGatewayUpstreams.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<GatewayUpstreams />);

    expect(screen.getByText('Loading upstreams...')).toBeInTheDocument();
  });

  it('renders upstreams table', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    expect(screen.getByText('roundrobin')).toBeInTheDocument();
    expect(screen.getByText('HTTP')).toBeInTheDocument();
    expect(screen.getByText('localhost:3000 (w:1)')).toBeInTheDocument();
  });

  it('filters upstreams by search text', async () => {
    mockedGetGatewayUpstreams.mockResolvedValue({
      items: [
        makeGatewayUpstream({ id: 'upstream-1', name: 'orders-api', nodes: { 'orders.internal:3000': 1 } }),
        makeGatewayUpstream({ id: 'upstream-2', name: 'billing-api', scheme: 'https', nodes: { 'billing.internal:443': 1 } }),
      ],
      total: 2,
    });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('orders-api')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search upstreams...' }), 'billing');

    expect(screen.queryByText('orders-api')).not.toBeInTheDocument();
    expect(screen.getByText('billing-api')).toBeInTheDocument();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Search upstreams...' }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search upstreams...' }), 'missing');

    expect(screen.getByText('No matching upstreams')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('orders-api')).toBeInTheDocument();
    expect(screen.getByText('billing-api')).toBeInTheDocument();
  });

  it('submits https upstreams with the selected scheme and default port', async () => {
    const user = userEvent.setup();
    mockedSaveGatewayUpstream.mockResolvedValue(makeGatewayUpstream({ scheme: 'https', nodes: { 'secure.example.com:443': 1 } }));

    renderWithProviders(<GatewayUpstreams />);

    await user.click(screen.getByRole('button', { name: '+ Add Upstream' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'secure-api');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Scheme' }), 'https');
    await user.type(screen.getByRole('textbox', { name: 'Node 1 host or IP' }), 'secure.example.com');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedSaveGatewayUpstream).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          name: 'secure-api',
          scheme: 'https',
          pass_host: 'node',
          nodes: { 'secure.example.com:443': 1 },
        }),
      );
    });
  });

  it('hides write actions for users with read-only upstream permission', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />, {
      permissions: ['gateway.upstreams.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add Upstream' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('renders empty state when no upstreams', async () => {
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [], total: 0 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('No upstreams')).toBeInTheDocument();
    });
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<GatewayUpstreams />);

    await userEvent.click(screen.getByRole('button', { name: '+ Add Upstream' }));

    const dialog = screen.getByRole('dialog', { name: 'Add Upstream' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-name-hint',
    );
    expect(document.getElementById('gateway-upstream-name-hint')).toHaveTextContent(
      'Identifier name',
    );
    expect(screen.getByRole('combobox', { name: 'Scheme' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-scheme-hint',
    );
    expect(screen.getByRole('combobox', { name: 'Host Header' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-host-header-hint',
    );
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Host Header' }), 'rewrite');
    expect(screen.getByRole('textbox', { name: 'Custom Host' })).toHaveAttribute(
      'id',
      'gateway-upstream-rewrite-host',
    );
    expect(screen.getByRole('combobox', { name: 'Type' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Type' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-type-hint',
    );
    expect(screen.getByRole('group', { name: 'Nodes' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-nodes-hint',
    );
    expect(screen.getByRole('textbox', { name: 'Node 1 host or IP' })).toHaveAttribute(
      'aria-describedby',
      'gateway-upstream-nodes-hint',
    );
    for (const option of ['Round Robin', 'Consistent Hash', 'EWMA', 'Least Connections']) {
      expect(screen.getByRole('option', { name: option })).toBeInTheDocument();
    }
  });

  it('opens edit modal on edit button click', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit upstream test-upstream' }));

    expect(screen.getByText('Edit Upstream')).toBeInTheDocument();
  });

  it('calls deleteGatewayUpstream after confirmation', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });
    mockedDeleteGatewayUpstream.mockResolvedValue(undefined);

    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete upstream test-upstream' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteGatewayUpstream).toHaveBeenCalledWith('upstream-1');
    });

    vi.restoreAllMocks();
  });

  describe('node path prefix', () => {
    const listFormNodes = [
      { host: '10.0.0.1', port: 8080, weight: 1 },
      { host: '10.0.0.2', port: 8080, weight: 2, metadata: { path_prefix: '/api' } },
    ];

    async function openEditFor(nodes: unknown) {
      const user = userEvent.setup();
      mockedGetGatewayUpstreams.mockResolvedValue({
        items: [makeGatewayUpstream({ nodes })],
        total: 1,
      });
      mockedSaveGatewayUpstream.mockResolvedValue(makeGatewayUpstream());
      renderWithProviders(<GatewayUpstreams />);
      await user.click(await screen.findByRole('button', { name: 'Edit upstream test-upstream' }));
      return user;
    }

    async function openCreateWithHost(host: string) {
      const user = userEvent.setup();
      mockedSaveGatewayUpstream.mockResolvedValue(makeGatewayUpstream());
      renderWithProviders(<GatewayUpstreams />);
      await user.click(screen.getByRole('button', { name: '+ Add Upstream' }));
      await user.type(screen.getByRole('textbox', { name: 'Node 1 host or IP' }), host);
      return user;
    }

    function expectSavedNodes(nodes: unknown) {
      return waitFor(() => {
        expect(mockedSaveGatewayUpstream).toHaveBeenCalledWith(
          expect.any(String),
          expect.objectContaining({ nodes }),
        );
      });
    }

    it('saves dict-form nodes back as a dict, keeping a zero weight', async () => {
      const user = await openEditFor({ 'localhost:3000': 1, 'standby.internal:3000': 0 });

      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes({ 'localhost:3000': 1, 'standby.internal:3000': 0 });
    });

    it('shows list-form prefixes in the table and the edit form', async () => {
      mockedGetGatewayUpstreams.mockResolvedValue({
        items: [makeGatewayUpstream({ nodes: listFormNodes })],
        total: 1,
      });

      renderWithProviders(<GatewayUpstreams />);

      expect(
        await screen.findByText('10.0.0.1:8080 (w:1), 10.0.0.2:8080/api (w:2)'),
      ).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: 'Edit upstream test-upstream' }));

      expect(screen.getByRole('textbox', { name: 'Node 1 host or IP' })).toHaveValue('10.0.0.1');
      expect(screen.getByRole('textbox', { name: 'Node 1 path prefix' })).toHaveValue('');
      const prefixInput = screen.getByRole('textbox', { name: 'Node 2 path prefix' });
      expect(prefixInput).toHaveValue('/api');
      expect(prefixInput).toHaveAttribute('aria-describedby', 'gateway-upstream-path-prefix-hint');
      expect(prefixInput).not.toHaveAttribute('aria-invalid');
      expect(document.getElementById('gateway-upstream-path-prefix-hint')).toHaveTextContent(
        'can only be retried on a node with the same prefix',
      );
    });

    it('sends list-form nodes with a normalized prefix', async () => {
      const user = await openCreateWithHost('10.0.0.1');
      await user.click(screen.getByRole('button', { name: '+ Add Node' }));
      await user.type(screen.getByRole('textbox', { name: 'Node 2 host or IP' }), '10.0.0.2');
      await user.type(screen.getByRole('textbox', { name: 'Node 2 path prefix' }), ' /api/v1// ');
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes([
        { host: '10.0.0.1', port: 80, weight: 1 },
        { host: '10.0.0.2', port: 80, weight: 1, metadata: { path_prefix: '/api/v1' } },
      ]);
    });

    it('treats a lone slash as no prefix and keeps the dict form', async () => {
      const user = await openCreateWithHost('10.0.0.2');
      await user.type(screen.getByRole('textbox', { name: 'Node 1 path prefix' }), '/');
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes({ '10.0.0.2:80': 1 });
    });

    it('accepts a prefix of exactly 256 characters with escapes and sub-delimiters', async () => {
      const prefix = `/a%2Fb/v1;x=1,y/${'c'.repeat(256 - '/a%2Fb/v1;x=1,y/'.length)}`;
      expect(prefix).toHaveLength(256);
      const user = await openCreateWithHost('10.0.0.2');
      await user.click(screen.getByRole('textbox', { name: 'Node 1 path prefix' }));
      await user.paste(prefix);
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes([
        { host: '10.0.0.2', port: 80, weight: 1, metadata: { path_prefix: prefix } },
      ]);
    });

    it.each([
      ['without a leading slash', 'api'],
      ['with a space', '/a b'],
      ['with a query string', '/api?x=1'],
      ['with a fragment', '/api#top'],
      ['with an empty segment', '/a//b'],
      ['with a dot segment', '/a/./b'],
      ['with a dot-dot segment', '/a/../b'],
      ['with an escaped dot-dot segment', '/a/%2e%2E/b'],
      ['with a broken escape', '/a%2'],
      ['longer than 256 characters', `/${'a'.repeat(256)}`],
    ])('blocks saving a prefix %s', async (_label, prefix) => {
      const user = await openCreateWithHost('10.0.0.2');
      const prefixInput = screen.getByRole('textbox', { name: 'Node 1 path prefix' });
      await user.click(prefixInput);
      await user.paste(prefix);
      await user.click(screen.getByRole('button', { name: 'Create' }));

      const error = screen.getByRole('alert');
      expect(error).toHaveTextContent('Node 1: the path prefix must start with /');
      expect(prefixInput).toHaveAttribute('aria-invalid', 'true');
      expect(prefixInput).toHaveAttribute(
        'aria-describedby',
        `${error.id} gateway-upstream-path-prefix-hint`,
      );
      expect(prefixInput).toHaveFocus();
      expect(mockedSaveGatewayUpstream).not.toHaveBeenCalled();
    });

    it('drops the error once the prefix is fixed', async () => {
      const user = await openCreateWithHost('10.0.0.2');
      const prefixInput = screen.getByRole('textbox', { name: 'Node 1 path prefix' });
      await user.type(prefixInput, 'api');
      expect(screen.getByRole('alert')).toBeInTheDocument();

      await user.clear(prefixInput);
      await user.type(prefixInput, '/api');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes([
        { host: '10.0.0.2', port: 80, weight: 1, metadata: { path_prefix: '/api' } },
      ]);
    });

    it('sends list-form nodes back unchanged, priority and unknown metadata included', async () => {
      const nodes = [
        { host: '10.0.0.1', port: 8080, weight: 1, priority: 1, metadata: { zone: 'a' } },
        { host: '10.0.0.2', port: 8080, weight: 0, priority: -1, metadata: { path_prefix: '/api', zone: 'b' } },
      ];
      const user = await openEditFor(nodes);

      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes(nodes);
    });

    it('keeps the list form for a priority without metadata', async () => {
      // Priority tiers (primary/backup) only exist in the list form; a dict
      // save would silently drop them.
      const nodes = [
        { host: '10.0.0.1', port: 80, weight: 1, priority: 1 },
        { host: '10.0.0.2', port: 80, weight: 1 },
      ];
      const user = await openEditFor(nodes);

      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes(nodes);
    });

    it('flags a stored prefix that is not a string instead of rewriting it', async () => {
      const user = await openEditFor([
        { host: '10.0.0.2', port: 8080, weight: 1, metadata: { path_prefix: ['/api'] } },
      ]);

      expect(screen.getByRole('textbox', { name: 'Node 1 path prefix' })).toHaveValue('["/api"]');
      expect(screen.getByRole('alert')).toHaveTextContent('Node 1: the path prefix must start with /');
      await user.click(screen.getByRole('button', { name: 'Update' }));

      expect(mockedSaveGatewayUpstream).not.toHaveBeenCalled();
    });

    it('keeps other metadata in the list form when a prefix is cleared', async () => {
      const user = await openEditFor([
        { host: '10.0.0.2', port: 8080, weight: 1, metadata: { path_prefix: '/api', zone: 'b' } },
      ]);

      await user.clear(screen.getByRole('textbox', { name: 'Node 1 path prefix' }));
      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes([{ host: '10.0.0.2', port: 8080, weight: 1, metadata: { zone: 'b' } }]);
    });

    it('goes back to the dict form once the only prefix is cleared', async () => {
      const user = await openEditFor(listFormNodes);

      await user.clear(screen.getByRole('textbox', { name: 'Node 2 path prefix' }));
      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes({ '10.0.0.1:8080': 1, '10.0.0.2:8080': 2 });
    });

    it('parses a bracketed IPv6 dict-form address', async () => {
      const user = await openEditFor({ '[::1]:8080': 1 });

      expect(screen.getByRole('textbox', { name: 'Node 1 host or IP' })).toHaveValue('[::1]');
      expect(screen.getByRole('spinbutton', { name: 'Node 1 port' })).toHaveValue(8080);
      await user.click(screen.getByRole('button', { name: 'Update' }));

      await expectSavedNodes({ '[::1]:8080': 1 });
    });

    it('brackets a bare IPv6 host in the dict form', async () => {
      const user = await openCreateWithHost('fe80::1');
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes({ '[fe80::1]:80': 1 });
    });

    it('brackets a bare IPv6 host in the list form, which APISIX requires', async () => {
      const user = await openCreateWithHost('fe80::1');
      await user.type(screen.getByRole('textbox', { name: 'Node 1 path prefix' }), '/api');
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await expectSavedNodes([
        { host: '[fe80::1]', port: 80, weight: 1, metadata: { path_prefix: '/api' } },
      ]);
    });
  });
});
