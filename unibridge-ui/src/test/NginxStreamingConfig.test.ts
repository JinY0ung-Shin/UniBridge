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

    expect(apiLocation).toContain('proxy_pass http://apisix:9080;');
    expect(apiLocation).toContain('proxy_buffering off;');
  });
});
