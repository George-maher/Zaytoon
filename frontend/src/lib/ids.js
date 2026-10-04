/**
 * Client-generated ids.
 *
 * `client_msg_id` goes on every outbound `message` frame so the backend can
 * drop a duplicate (a retried send, a double Enter, a reconnect that replays
 * the composer) instead of running the turn twice. It is *optional* on the
 * wire — the server treats its absence as "nothing to dedupe" — so this must
 * never throw and never block a send.
 *
 * `crypto.randomUUID()` is the primary source; the manual v4 fallback covers
 * older browsers and any test environment that exposes `crypto` without it.
 */

function manualUuid() {
  const hex = "0123456789abcdef";
  let out = "";
  for (let i = 0; i < 36; i++) {
    if (i === 8 || i === 13 || i === 18 || i === 23) {
      out += "-";
    } else if (i === 14) {
      out += "4"; // version 4
    } else if (i === 19) {
      out += hex[(Math.floor(Math.random() * 16) & 0x3) | 0x8]; // variant
    } else {
      out += hex[Math.floor(Math.random() * 16)];
    }
  }
  return out;
}

export function clientMsgId() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch (e) {
    // A hardened browser can expose `crypto` and still refuse randomUUID.
  }
  return manualUuid();
}

export default clientMsgId;
