/**
 * Bounded exponential backoff for the WebSocket reconnect loop.
 *
 * The client used to retry on a flat 3 seconds forever. That is both too eager
 * — a backend that is restarting is hammered every three seconds by every open
 * tab — and too slow for a transient blip, where the first retry should be
 * quick. The delay grows per consecutive failure and is capped, then reset by a
 * successful open (see `socket.onopen`), so one bad afternoon cannot leave the
 * session waiting fifteen seconds after the link is healthy again.
 *
 * Pure and exported so the curve is testable without a socket.
 *
 * @param {number} attempt  0 for the first retry after a drop
 * @param {{baseMs?: number, maxMs?: number, jitter?: () => number}} [options]
 * @returns {number} milliseconds to wait
 */
export function reconnectDelay(attempt, options = {}) {
  const baseMs = options.baseMs ?? 500;
  const maxMs = options.maxMs ?? 15000;
  const jitter = options.jitter ?? Math.random;
  const safeAttempt = Math.max(0, Math.floor(Number(attempt) || 0));
  // `2 ** 30` is still a finite number, but the cap makes the growth stop long
  // before that; clamping the exponent also keeps a corrupt counter from
  // producing Infinity.
  const exponent = Math.min(safeAttempt, 20);
  const backoff = Math.min(maxMs, baseMs * 2 ** exponent);
  // Jitter keeps a room full of clients from reconnecting in lockstep.
  return Math.round(backoff * (0.85 + 0.3 * jitter()));
}

export default reconnectDelay;
