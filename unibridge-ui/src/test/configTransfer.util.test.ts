import { describe, it, expect } from 'vitest';
import {
  documentSections,
  dryRunKey,
  exportFileName,
  parseExportDocument,
  previewJson,
  sectionCount,
} from '../utils/configTransfer';

const VALID = {
  unibridge_export_version: 1,
  exported_at: '2026-08-15T01:00:00Z',
  sections: {
    routes: [{ id: 'r1' }, { id: 'r2' }],
    alert_settings: { check_interval_seconds: 60, admin_emails: [] },
  },
};

describe('parseExportDocument', () => {
  it('accepts a version 1 document with sections', () => {
    const parsed = parseExportDocument(JSON.stringify(VALID));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.doc.exported_at).toBe('2026-08-15T01:00:00Z');
  });

  it('rejects malformed JSON and non-object roots', () => {
    expect(parseExportDocument('{not json')).toEqual({ ok: false, reason: 'malformedJson' });
    expect(parseExportDocument('[]')).toEqual({ ok: false, reason: 'malformedJson' });
    expect(parseExportDocument('"a string"')).toEqual({ ok: false, reason: 'malformedJson' });
    expect(parseExportDocument('null')).toEqual({ ok: false, reason: 'malformedJson' });
  });

  it('rejects any version other than 1', () => {
    for (const version of [2, 0, '1', null, undefined]) {
      const parsed = parseExportDocument(JSON.stringify({ ...VALID, unibridge_export_version: version }));
      expect(parsed).toEqual({ ok: false, reason: 'unsupportedVersion' });
    }
  });

  it('rejects a document whose sections are missing or not an object', () => {
    expect(parseExportDocument(JSON.stringify({ unibridge_export_version: 1 })))
      .toEqual({ ok: false, reason: 'missingSections' });
    expect(parseExportDocument(JSON.stringify({ ...VALID, sections: [] })))
      .toEqual({ ok: false, reason: 'missingSections' });
    expect(parseExportDocument(JSON.stringify({ ...VALID, sections: null })))
      .toEqual({ ok: false, reason: 'missingSections' });
  });
});

describe('sectionCount / documentSections', () => {
  it('counts list items and settings keys', () => {
    expect(sectionCount([1, 2, 3])).toBe(3);
    expect(sectionCount([])).toBe(0);
    expect(sectionCount({ a: 1, b: 2 })).toBe(2);
    expect(sectionCount(null)).toBe(0);
    expect(sectionCount('text')).toBe(0);
  });

  it('lists sections in file order with their counts', () => {
    const parsed = parseExportDocument(JSON.stringify(VALID));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(documentSections(parsed.doc)).toEqual([
      { name: 'routes', count: 2 },
      { name: 'alert_settings', count: 2 },
    ]);
  });

  it('returns nothing for a document without usable sections', () => {
    expect(documentSections({ unibridge_export_version: 1, exported_at: '', sections: {} })).toEqual([]);
  });
});

describe('dryRunKey', () => {
  it('ignores section order but not section membership', () => {
    expect(dryRunKey('file#1', ['routes', 'roles'])).toBe(dryRunKey('file#1', ['roles', 'routes']));
    expect(dryRunKey('file#1', ['routes'])).not.toBe(dryRunKey('file#1', ['routes', 'roles']));
  });

  it('changes when a different upload is loaded', () => {
    expect(dryRunKey('file#1', ['routes'])).not.toBe(dryRunKey('file#2', ['routes']));
  });
});

describe('exportFileName', () => {
  it('dates the download in the local zone', () => {
    expect(exportFileName(new Date(2026, 7, 5))).toBe('unibridge-config-20260805.json');
    expect(exportFileName(new Date(2026, 11, 31))).toBe('unibridge-config-20261231.json');
  });
});

describe('previewJson', () => {
  it('pretty-prints values and caps very large sections', () => {
    expect(previewJson({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(previewJson(undefined)).toBe('');
    const huge = previewJson(Array.from({ length: 2000 }, (_, i) => ({ name: `item-${i}` })));
    expect(huge.length).toBeLessThan(4100);
    expect(huge.endsWith('\n…')).toBe(true);
  });
});
