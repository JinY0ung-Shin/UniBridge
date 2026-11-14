/** Delay before the object URL is revoked: revoking right after the click can cancel the download in some browsers. */
export const REVOKE_DELAY_MS = 1000;

/** Save a blob the browser already holds to disk under `filename`. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    setTimeout(() => window.URL.revokeObjectURL(url), REVOKE_DELAY_MS);
  }
}

/** Save `value` as pretty-printed JSON. */
export function saveJson(value: unknown, filename: string): void {
  saveBlob(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }), filename);
}
