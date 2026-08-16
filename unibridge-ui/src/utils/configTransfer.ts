import type { ConfigExportDocument } from '../api/client';

/** The only export-document version this UI knows how to import. */
export const CONFIG_EXPORT_VERSION = 1;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export type ParseFailure = 'malformedJson' | 'unsupportedVersion' | 'missingSections';

export type ParsedExport =
  | { ok: true; doc: ConfigExportDocument }
  | { ok: false; reason: ParseFailure };

/**
 * Validate an uploaded file before it is ever sent to the import endpoint.
 * A document from a different exporter version can describe the same section
 * names with different item shapes, so version mismatch is rejected here
 * rather than left for the backend to half-apply.
 */
export function parseExportDocument(text: string): ParsedExport {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { ok: false, reason: 'malformedJson' };
  }
  if (!isRecord(raw)) return { ok: false, reason: 'malformedJson' };
  if (raw.unibridge_export_version !== CONFIG_EXPORT_VERSION) {
    return { ok: false, reason: 'unsupportedVersion' };
  }
  if (!isRecord(raw.sections)) return { ok: false, reason: 'missingSections' };
  return { ok: true, doc: raw as unknown as ConfigExportDocument };
}

/**
 * Entries a section carries: list sections count their items, settings
 * sections (`alert_settings`, `system_settings`) count their keys.
 */
export function sectionCount(value: unknown): number {
  if (Array.isArray(value)) return value.length;
  if (isRecord(value)) return Object.keys(value).length;
  return 0;
}

export interface SectionSummary {
  name: string;
  count: number;
}

/** Sections present in the uploaded document, in file order. */
export function documentSections(doc: ConfigExportDocument): SectionSummary[] {
  if (!isRecord(doc?.sections)) return [];
  return Object.entries(doc.sections).map(([name, value]) => ({
    name,
    count: sectionCount(value),
  }));
}

/**
 * Identity of a dry-run. Apply stays unlocked only while the loaded file and
 * the chosen sections still match what the dry-run previewed — otherwise the
 * results on screen describe a different operation than the one about to run.
 */
export function dryRunKey(fileToken: string, sections: string[]): string {
  return `${fileToken}|${[...sections].sort().join(',')}`;
}

/** `unibridge-config-YYYYMMDD.json`, dated in the browser's local zone. */
export function exportFileName(date: Date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `unibridge-config-${y}${m}${d}.json`;
}

const PREVIEW_LIMIT = 4000;

/** Pretty-printed section contents, capped so a huge section can't freeze the page. */
export function previewJson(value: unknown): string {
  const text = JSON.stringify(value, null, 2);
  if (text === undefined) return '';
  return text.length > PREVIEW_LIMIT ? `${text.slice(0, PREVIEW_LIMIT)}\n…` : text;
}
