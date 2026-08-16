import { describe, it, expect } from 'vitest';
import en from '../locales/en.json';
import ko from '../locales/ko.json';

type Tree = Record<string, unknown>;

function flatten(tree: Tree, prefix = ''): string[] {
  return Object.entries(tree).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && typeof value === 'object' && !Array.isArray(value)
      ? flatten(value as Tree, path)
      : [path];
  });
}

const enKeys = flatten(en as Tree);
const koKeys = flatten(ko as Tree);

describe('locale files', () => {
  it('define exactly the same keys', () => {
    expect(koKeys.filter((k) => !enKeys.includes(k))).toEqual([]);
    expect(enKeys.filter((k) => !koKeys.includes(k))).toEqual([]);
  });

  it('have no empty translations', () => {
    const empty = [...Object.entries({ en, ko })].flatMap(([lang, tree]) =>
      flatten(tree as Tree)
        .filter((path) => {
          const value = path
            .split('.')
            .reduce<unknown>((node, key) => (node as Tree)?.[key], tree);
          return typeof value !== 'string' || value.trim() === '';
        })
        .map((path) => `${lang}:${path}`),
    );
    expect(empty).toEqual([]);
  });
});
