import { describe, expect, it } from 'vitest';
import nginxConfig from '../../nginx.conf?raw';

function getLocationBlock(path: string) {
  const escapedPath = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = nginxConfig.match(new RegExp(`location ${escapedPath} \\{[\\s\\S]*?\\n    \\}`, 'm'));
  return match?.[0] ?? '';
}

describe('nginx /api proxy config', () => {
  it('disables proxy buffering so upstream streaming is flushed progressively', () => {
    const apiLocation = getLocationBlock('/api/');

    expect(apiLocation).toContain('proxy_pass http://$apisix_upstream:9080$request_uri;');
    expect(apiLocation).toContain('proxy_buffering off;');
  });
});

describe('nginx gzip config', () => {
  it('compresses JSON and leaves event streams alone', () => {
    const gzipTypes = nginxConfig.match(/^\s*gzip_types ([^;]+);/m)?.[1].split(/\s+/) ?? [];

    expect(nginxConfig).toMatch(/^\s*gzip on;/m);
    expect(gzipTypes).toContain('application/json');
    expect(gzipTypes).not.toContain('text/event-stream');
  });
});
