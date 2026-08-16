vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  exportConfig: vi.fn(),
  importConfig: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  exportConfig,
  importConfig,
  type ConfigExportDocument,
  type ConfigImportResult,
} from '../api/client';
import ConfigTransfer from '../pages/ConfigTransfer';
import { navItems, isNavItemVisible } from '../components/navItems';
import { renderWithProviders } from './helpers';

const mockedExportConfig = vi.mocked(exportConfig);
const mockedImportConfig = vi.mocked(importConfig);

const FULL_PERMISSIONS = ['admin.config.read', 'admin.config.write'];
const READ_PERMISSIONS = ['admin.config.read'];

/** Every wait here is on a mocked promise plus a React commit; the default 1s
 *  is tight when the whole suite runs in parallel on a loaded machine. */
const WAIT = { timeout: 5000 };

const originalCreateObjectUrl = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

function restoreProperty(target: object, key: PropertyKey, descriptor: PropertyDescriptor | undefined) {
  if (descriptor) {
    Object.defineProperty(target, key, descriptor);
  } else {
    Reflect.deleteProperty(target, key);
  }
}

const SAMPLE_DOC: ConfigExportDocument = {
  unibridge_export_version: 1,
  exported_at: '2026-08-15T01:00:00Z',
  sections: {
    routes: [{ id: 'r1', name: 'route-one' }, { id: 'r2', name: 'route-two' }],
    db_connections: [{ alias: 'orders' }],
    alert_settings: { check_interval_seconds: 60, admin_emails: [] },
  },
  excluded: { builtin_routes: ['query-api'], notes: [] },
};

const DRY_RUN_RESULT: ConfigImportResult = {
  dry_run: true,
  results: [
    { section: 'routes', name: 'route-one', action: 'create', reason: null },
    { section: 'routes', name: 'route-two', action: 'update', reason: 'differs from existing' },
    { section: 'db_connections', name: 'orders', action: 'skip', reason: 'already identical' },
    { section: 'alert_settings', name: 'alert_settings', action: 'error', reason: 'channel missing' },
  ],
  summary: { create: 1, update: 1, skip: 1, error: 1 },
};

function makeFile(content: unknown, name = 'config.json'): File {
  const text = typeof content === 'string' ? content : JSON.stringify(content);
  return new File([text], name, { type: 'application/json' });
}

/** Upload a document and wait for its section list to render. */
async function uploadDocument(user: ReturnType<typeof userEvent.setup>, file: File) {
  await user.upload(screen.getByLabelText('Config file'), file);
  await screen.findByRole('checkbox', { name: 'Gateway routes' }, WAIT);
}

function applyButton() {
  return screen.getByRole('button', { name: 'Apply' });
}

function dryRunButton() {
  return screen.getByRole('button', { name: 'Preview (dry-run)' });
}

/** Item count rendered next to one section's checkbox. */
function countFor(sectionLabel: string): string | null {
  const row = screen.getByRole('checkbox', { name: sectionLabel }).closest('.config-section__row');
  return within(row as HTMLElement).getByText(/items$/).textContent;
}

/**
 * Body of importConfig's `callNumber`-th call (1-based), waiting until that
 * call has actually been made.
 *
 * Keyed on the call count rather than "whatever the latest call is" so a slow
 * worker cannot make the assertion read a neighbouring call, and so it never
 * runs before the interaction it describes has reached the mock. TanStack
 * Query appends its own context argument, hence the `[0]`.
 */
async function importBody(callNumber: number) {
  await waitFor(() => expect(mockedImportConfig).toHaveBeenCalledTimes(callNumber), WAIT);
  return mockedImportConfig.mock.calls[callNumber - 1][0];
}

async function waitForApplyEnabled() {
  await waitFor(() => expect(applyButton()).toBeEnabled(), WAIT);
}

async function waitForApplyDisabled() {
  await waitFor(() => expect(applyButton()).toBeDisabled(), WAIT);
}

describe('ConfigTransfer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedExportConfig.mockResolvedValue(SAMPLE_DOC);
    mockedImportConfig.mockResolvedValue(DRY_RUN_RESULT);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    restoreProperty(URL, 'createObjectURL', originalCreateObjectUrl);
    restoreProperty(URL, 'revokeObjectURL', originalRevokeObjectUrl);
  });

  /* ── Navigation ── */

  it('registers an admin nav entry gated on admin.config.read', () => {
    const item = navItems.find((i) => i.to === '/config-transfer');
    expect(item).toMatchObject({
      labelKey: 'nav.configTransfer',
      section: 'admin',
      permission: 'admin.config.read',
    });
    expect(isNavItemVisible(item!, ['admin.config.read'])).toBe(true);
    expect(isNavItemVisible(item!, ['admin.audit.read'])).toBe(false);
  });

  /* ── Export ── */

  it('downloads the export document as a dated JSON file', async () => {
    const createObjectURL = vi.fn(() => 'blob:config');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });

    await user.click(screen.getByRole('button', { name: 'Download config' }));

    // Wait on the download itself, not on the request: the anchor is only built
    // once the export promise resolves.
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob)), WAIT);
    expect(mockedExportConfig).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:config');

    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.download).toMatch(/^unibridge-config-\d{8}\.json$/);
  });

  it('reports an export failure', async () => {
    mockedExportConfig.mockRejectedValueOnce(new Error('offline'));
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });

    await user.click(screen.getByRole('button', { name: 'Download config' }));

    expect(await screen.findByRole('alert', undefined, WAIT)).toHaveTextContent(
      'Failed to export configuration.',
    );
  });

  /* ── Permission gating ── */

  it('replaces the import card with a notice when the user cannot write', () => {
    renderWithProviders(<ConfigTransfer />, { permissions: READ_PERMISSIONS });

    expect(screen.getByRole('button', { name: 'Download config' })).toBeInTheDocument();
    expect(screen.getByText(/Importing requires the admin.config.write permission/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Config file')).not.toBeInTheDocument();
  });

  it('hides the export button when the user cannot read config', () => {
    renderWithProviders(<ConfigTransfer />, { permissions: ['admin.config.write'] });

    expect(screen.queryByRole('button', { name: 'Download config' })).not.toBeInTheDocument();
    expect(screen.getByText('You do not have permission to export configuration.')).toBeInTheDocument();
    expect(screen.getByLabelText('Config file')).toBeInTheDocument();
  });

  /* ── File validation ── */

  it('rejects files that are not version 1 export documents', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    const input = screen.getByLabelText('Config file');

    await user.upload(input, makeFile('{ not json', 'broken.json'));
    expect(await screen.findByRole('alert', undefined, WAIT)).toHaveTextContent(
      'This file is not valid JSON.',
    );

    await user.upload(input, makeFile({ ...SAMPLE_DOC, unibridge_export_version: 2 }));
    await waitFor(
      () => expect(screen.getByRole('alert')).toHaveTextContent('only imports version 1 documents'),
      WAIT,
    );

    await user.upload(input, makeFile({ unibridge_export_version: 1, exported_at: 'x' }));
    await waitFor(
      () => expect(screen.getByRole('alert')).toHaveTextContent('The document has no "sections" object.'),
      WAIT,
    );

    // A rejected file never reaches the import endpoint and offers no sections.
    expect(mockedImportConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();

    await uploadDocument(user, makeFile(SAMPLE_DOC));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  /* ── Section picker ── */

  it('derives section checkboxes with item counts, all selected by default', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });

    await uploadDocument(user, makeFile(SAMPLE_DOC));

    expect(screen.getByText(/config\.json/)).toHaveTextContent('exported');
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(3);
    for (const checkbox of checkboxes) expect(checkbox).toBeChecked();

    // Only the sections present in the file, and lists count their items while
    // settings objects count their keys.
    expect(countFor('Gateway routes')).toBe('2 items');
    expect(countFor('DB connections')).toBe('1 items');
    expect(countFor('Alert settings')).toBe('2 items');
    expect(screen.queryByRole('checkbox', { name: 'Roles' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).not.toBeChecked();
    expect(dryRunButton()).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Select all' }));
    for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeChecked();
  });

  it('previews a section as JSON', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));

    const toggle = screen.getByRole('button', { name: 'Preview: DB connections' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);

    const preview = document.getElementById('config-section-preview-db_connections');
    expect(preview).toHaveTextContent('"alias": "orders"');
    expect(screen.getByRole('button', { name: 'Hide preview: DB connections' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  /* ── Dry-run → apply ── */

  it('locks Apply until a dry-run covers the current file and selection', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));

    expect(applyButton()).toBeDisabled();
    expect(screen.getByText(/Run a dry-run for the current file/)).toBeInTheDocument();

    await user.click(dryRunButton());
    expect(await importBody(1)).toEqual({
      dry_run: true,
      sections: ['routes', 'db_connections', 'alert_settings'],
      data: SAMPLE_DOC,
    });
    await waitForApplyEnabled();

    // Changing the selection invalidates the preview on screen.
    await user.click(screen.getByRole('checkbox', { name: 'DB connections' }));
    await waitFor(
      () => expect(screen.getByRole('checkbox', { name: 'DB connections' })).not.toBeChecked(),
      WAIT,
    );
    await waitForApplyDisabled();

    await user.click(dryRunButton());
    expect(await importBody(2)).toEqual({
      dry_run: true,
      sections: ['routes', 'alert_settings'],
      data: SAMPLE_DOC,
    });
    await waitForApplyEnabled();

    // A different upload also invalidates it, even with the same contents.
    await uploadDocument(user, makeFile(SAMPLE_DOC, 'config.json'));
    await waitForApplyDisabled();
  });

  it('freezes the file and section pickers while a dry-run is in flight', async () => {
    let resolveDryRun!: (value: ConfigImportResult) => void;
    mockedImportConfig.mockReturnValueOnce(new Promise((resolve) => { resolveDryRun = resolve; }));
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));

    await user.click(dryRunButton());

    await waitFor(
      () => expect(screen.getByRole('button', { name: 'Previewing...' })).toBeDisabled(),
      WAIT,
    );
    expect(screen.getByLabelText('Config file')).toBeDisabled();
    for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeDisabled();
    expect(applyButton()).toBeDisabled();

    resolveDryRun(DRY_RUN_RESULT);

    await waitForApplyEnabled();
    expect(screen.getByLabelText('Config file')).toBeEnabled();
  });

  it('renders dry-run results with colour-coded actions and a summary', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));
    await user.click(dryRunButton());

    expect(
      await screen.findByRole('heading', { name: 'Dry-run preview' }, WAIT),
    ).toBeInTheDocument();
    expect(screen.getByText('Create 1 · Update 1 · Skip 1 · Error 1')).toBeInTheDocument();

    const table = within(screen.getByRole('table'));
    expect(table.getByText('route-one')).toBeInTheDocument();
    expect(table.getByText('differs from existing')).toBeInTheDocument();
    expect(table.getByText('already identical')).toBeInTheDocument();

    expect(screen.getByText('Create')).toHaveClass('badge', 'badge-import-create');
    expect(screen.getByText('Update')).toHaveClass('badge-import-update');
    expect(screen.getByText('Skip')).toHaveClass('badge-import-skip');
    expect(screen.getByText('Error')).toHaveClass('badge-import-error');

    // Error rows keep their reason; a missing reason renders as an em dash.
    const errorRow = screen.getByText('Error').closest('tr');
    expect(within(errorRow!).getByText('channel missing')).toBeInTheDocument();
    const createRow = screen.getByText('Create').closest('tr');
    expect(within(createRow!).getByText('—')).toBeInTheDocument();

    // Short result lists need no section filter.
    expect(screen.queryByLabelText('Section filter')).not.toBeInTheDocument();
  });

  it('filters long result lists by section', async () => {
    mockedImportConfig.mockResolvedValue({
      dry_run: true,
      results: [
        ...Array.from({ length: 11 }, (_, i) => ({
          section: 'routes',
          name: `route-${i}`,
          action: 'create' as const,
          reason: null,
        })),
        { section: 'db_connections', name: 'orders', action: 'skip' as const, reason: null },
      ],
      summary: { create: 11, update: 0, skip: 1, error: 0 },
    });
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));
    await user.click(dryRunButton());

    const filter = await screen.findByLabelText('Section filter', undefined, WAIT);
    expect(screen.getByText('orders')).toBeInTheDocument();

    await user.selectOptions(filter, 'routes');
    expect(screen.getByText('route-0')).toBeInTheDocument();
    expect(screen.queryByText('orders')).not.toBeInTheDocument();

    await user.selectOptions(filter, '');
    expect(screen.getByText('orders')).toBeInTheDocument();
  });

  it('applies after confirmation and re-locks Apply afterwards', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));
    await user.click(dryRunButton());
    await waitForApplyEnabled();

    mockedImportConfig.mockResolvedValueOnce({
      dry_run: false,
      results: [{ section: 'routes', name: 'route-one', action: 'create', reason: null }],
      summary: { create: 1, update: 0, skip: 0, error: 0 },
    });
    await user.click(applyButton());

    await waitFor(() => expect(confirmSpy).toHaveBeenCalledOnce(), WAIT);
    expect(await importBody(2)).toEqual({
      dry_run: false,
      sections: ['routes', 'db_connections', 'alert_settings'],
      data: SAMPLE_DOC,
    });
    expect(
      await screen.findByRole('heading', { name: 'Import result' }, WAIT),
    ).toBeInTheDocument();
    expect(await screen.findByText('Configuration imported', undefined, WAIT)).toBeInTheDocument();
    // Server state moved, so a second apply needs its own dry-run.
    await waitForApplyDisabled();
  });

  it('does not apply when the confirmation is dismissed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));
    await user.click(dryRunButton());
    await waitForApplyEnabled();

    await user.click(applyButton());

    // confirm() decides synchronously inside the click handler, so once it has
    // run the apply either fired already or never will.
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledOnce(), WAIT);
    expect(mockedImportConfig).toHaveBeenCalledOnce();
    expect((await importBody(1)).dry_run).toBe(true);
    expect(applyButton()).toBeEnabled();
  });

  it('reports a partly failed apply and a failed dry-run', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderWithProviders(<ConfigTransfer />, { permissions: FULL_PERMISSIONS });
    await uploadDocument(user, makeFile(SAMPLE_DOC));
    await user.click(dryRunButton());
    await waitForApplyEnabled();

    mockedImportConfig.mockResolvedValueOnce({
      dry_run: false,
      results: [{ section: 'routes', name: 'route-one', action: 'error', reason: 'upstream missing' }],
      summary: { create: 0, update: 0, skip: 0, error: 2 },
    });
    await user.click(applyButton());
    expect(
      await screen.findByText('Import finished with errors (2)', undefined, WAIT),
    ).toBeInTheDocument();

    mockedImportConfig.mockRejectedValueOnce(new Error('boom'));
    await user.click(dryRunButton());
    expect(await screen.findByText('Dry-run failed.', undefined, WAIT)).toBeInTheDocument();
  });
});
