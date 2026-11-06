/** A host's or service's scrape status ("up" | "down" | "unknown" | "disabled"), translated. */
export function scrapeStatusLabel(t: (key: string) => string, status: string | null | undefined): string {
  switch (status) {
    case 'up':
      return t('servers.status_up');
    case 'down':
      return t('servers.status_down');
    case 'disabled':
      return t('servers.disabled');
    default:
      return t('servers.status_unknown');
  }
}

/**
 * Toast colour for a Test result: green only when everything probed is up. A
 * host whose node_exporter answers while its GPU exporter does not is a failure.
 */
export function testToastType(status: string, gpuStatus?: string | null): 'success' | 'error' {
  return status === 'up' && (gpuStatus == null || gpuStatus === 'up') ? 'success' : 'error';
}
