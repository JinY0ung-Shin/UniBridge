/*
 * The one place an API error becomes text. FastAPI sends `detail` in three
 * shapes, and only the first is a string:
 *   - a sentence (HTTPException),
 *   - a list of `{msg, loc}` (request validation, 422),
 *   - an object `{message, id}` (a duplicate network opening).
 * Rendering the list or the object as JSX throws "Objects are not valid as a
 * React child", so pages read errors through these helpers, never `detail`.
 */

/** A `detail` value as text, or undefined when it carries none. */
export function detailText(detail: unknown): string | undefined {
  if (typeof detail === 'string') return detail.trim() ? detail : undefined;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
      .filter(Boolean);
    return msgs.length ? msgs.join('; ') : undefined;
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const { message, id } = detail as { message: unknown; id?: unknown };
    return id != null ? `${String(message)} (#${String(id)})` : String(message);
  }
  return undefined;
}

/** True for an error that came back with an HTTP response (axios sets none on a network failure). */
function hasResponse(err: unknown): boolean {
  return Boolean(err && typeof err === 'object' && 'response' in err && (err as { response?: unknown }).response);
}

/** The server's reason for a failed request, or undefined when it gave none. */
export function apiErrorDetail(err: unknown): string | undefined {
  if (!hasResponse(err)) return undefined;
  const data = (err as { response: { data?: unknown } }).response.data;
  return data && typeof data === 'object' ? detailText((data as { detail?: unknown }).detail) : undefined;
}

/** The server's reason for a failed request, else `fallback`. */
export function apiErrorMessage(err: unknown, fallback: string): string {
  return apiErrorDetail(err) ?? fallback;
}

/**
 * Like `apiErrorMessage`, but an error that never got a response (one thrown in
 * the browser, or a network failure) shows its own message.
 */
export function errorMessage(err: unknown, fallback: string): string {
  if (hasResponse(err)) return apiErrorMessage(err, fallback);
  return err instanceof Error && err.message ? err.message : fallback;
}
