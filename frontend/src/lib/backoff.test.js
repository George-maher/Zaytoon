/**
 * `lib/backoff.js` — bounded exponential backoff tests.
 *
 * Current contract: `reconnectDelay(attempt, options?)` where
 *   - `attempt` is 0 for the first retry after a drop (not 1),
 *   - `options = { baseMs = 500, maxMs = 15000, jitter = Math.random }`,
 *   - the curve is `min(maxMs, baseMs * 2 ** attempt)`,
 *   - the result is `round(backoff * (0.85 + 0.3 * jitter()))`, i.e. jitter is a
 *     multiplicative spread of ±15%, not an additive offset.
 *
 * The jitter tests therefore inject a deterministic `jitter` function rather
 * than hoping `Math.random` lands somewhere convenient.
 */

import { describe, test, expect } from "vitest";

import { reconnectDelay } from "../lib/backoff";

const BASE = 500; // ms
const CAP = 15_000; // 15 s

/** A jitter source that always returns `value` (in [0, 1]). */
const fixedJitter = (value) => () => value;

describe("backoff — reconnectDelay", () => {
  test("attempt 0 returns base (first retry after a drop)", () => {
    expect(reconnectDelay(0, { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) })).toBe(BASE);
  });

  test("grows by a factor of two per attempt", () => {
    const opts = { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) };
    expect(reconnectDelay(1, opts)).toBe(BASE * 2);
    expect(reconnectDelay(2, opts)).toBe(BASE * 4);
    expect(reconnectDelay(3, opts)).toBe(BASE * 8);
    expect(reconnectDelay(4, opts)).toBe(BASE * 16);
  });

  test("caps at maxMs when the exponential exceeds it", () => {
    const opts = { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) };
    // 500 * 2^5 = 16000 > 15000 → capped at 15000
    expect(reconnectDelay(5, opts)).toBe(CAP);
  });

  test("caps even for absurd attempts (no Infinity)", () => {
    const opts = { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) };
    expect(reconnectDelay(100, opts)).toBe(CAP);
    expect(Number.isFinite(reconnectDelay(1e9, opts))).toBe(true);
  });

  test("a negative or non-numeric attempt is treated as 0", () => {
    const opts = { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) };
    expect(reconnectDelay(-5, opts)).toBe(BASE);
    expect(reconnectDelay("nonsense", opts)).toBe(BASE);
    expect(reconnectDelay(undefined, opts)).toBe(BASE);
  });

  test("jitter is multiplicative and bounded to 0.85–1.15×", () => {
    const low = reconnectDelay(1, { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0) });
    const mid = reconnectDelay(1, { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(0.5) });
    const high = reconnectDelay(1, { baseMs: BASE, maxMs: CAP, jitter: fixedJitter(1) });
    expect(low).toBe(Math.round(BASE * 2 * 0.85));
    expect(mid).toBe(BASE * 2);
    expect(high).toBe(Math.round(BASE * 2 * 1.15));
    expect(low).toBeLessThan(mid);
    expect(mid).toBeLessThan(high);
  });

  test("random jitter stays inside the documented spread", () => {
    const backoff = BASE * 2;
    for (let i = 0; i < 100; i++) {
      const delay = reconnectDelay(1, { baseMs: BASE, maxMs: CAP });
      expect(delay).toBeGreaterThanOrEqual(Math.round(backoff * 0.85));
      expect(delay).toBeLessThanOrEqual(Math.round(backoff * 1.15));
    }
  });

  test("defaults are 500 ms base and 15 s cap", () => {
    expect(reconnectDelay(0, { jitter: fixedJitter(0.5) })).toBe(500);
    expect(reconnectDelay(20, { jitter: fixedJitter(0.5) })).toBe(15_000);
  });
});
