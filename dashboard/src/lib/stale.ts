export const STALE_AFTER_MS = 10_000;

export function isStale(lastSeenMs: number, now: number = Date.now()): boolean {
  return now - lastSeenMs > STALE_AFTER_MS;
}
