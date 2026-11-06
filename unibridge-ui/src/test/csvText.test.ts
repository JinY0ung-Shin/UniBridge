import { describe, expect, it } from 'vitest';
import { decodeCsvBytes, sampleHeader, stripBom } from '../pages/network/csvText';
import { formatRelativeTime } from '../utils/time';

describe('decodeCsvBytes', () => {
  it('decodes UTF-8 and strips its BOM', () => {
    const bytes = new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode('이름,ip\n')]);
    expect(decodeCsvBytes(bytes)).toEqual({ text: '이름,ip\n', encoding: 'utf-8' });
  });

  it('falls back to EUC-KR (CP949) when the bytes are not valid UTF-8', () => {
    // "이름,ip\n서버1" saved by Excel on Korean Windows.
    const bytes = new Uint8Array([192, 204, 184, 167, 44, 105, 112, 10, 188, 173, 185, 246, 49]);
    expect(decodeCsvBytes(bytes.buffer)).toEqual({ text: '이름,ip\n서버1', encoding: 'euc-kr' });
  });

  it('reads plain ASCII as UTF-8', () => {
    expect(decodeCsvBytes(new TextEncoder().encode('name,ip'))).toEqual({ text: 'name,ip', encoding: 'utf-8' });
  });

  it('decodes UTF-16LE with a BOM ("Unicode text")', () => {
    const text = '호스트,ip';
    const bytes = new Uint8Array(2 + text.length * 2);
    bytes.set([0xff, 0xfe]);
    for (let i = 0; i < text.length; i += 1) {
      const code = text.charCodeAt(i);
      bytes[2 + i * 2] = code & 0xff;
      bytes[3 + i * 2] = code >> 8;
    }
    expect(decodeCsvBytes(bytes)).toEqual({ text, encoding: 'utf-16le' });
  });

  it('strips a BOM from pasted text', () => {
    expect(stripBom('﻿name')).toBe('name');
    expect(stripBom('name')).toBe('name');
  });

  it('builds a sample header in either language', () => {
    expect(sampleHeader('openings', 'en')).toBe('src,dst,ports,protocol,status,ticket no,requester,purpose');
    expect(sampleHeader('port_usages', 'ko')).toBe('호스트,포트,서비스,프로토콜,담당자,설명');
  });
});

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-11-06T12:00:00Z');
  it('picks the largest fitting unit', () => {
    expect(formatRelativeTime('2026-11-06T11:59:30Z', 'en', now)).toBe('30 seconds ago');
    expect(formatRelativeTime('2026-11-06T11:55:00Z', 'en', now)).toBe('5 minutes ago');
    expect(formatRelativeTime('2026-11-06T09:00:00Z', 'ko', now)).toBe('3시간 전');
    expect(formatRelativeTime('2026-11-05T12:00:00Z', 'en', now)).toBe('yesterday');
  });
  it('returns a dash for missing or bad input', () => {
    expect(formatRelativeTime(null, 'en', now)).toBe('—');
    expect(formatRelativeTime('nope', 'en', now)).toBe('—');
  });
});
