/**
 * Background Keepalive Web Worker for Xiaomi Redmi Note 8 / MIUI.
 *
 * Browsers aggressively throttle window.setInterval when display is locked.
 * Dedicated Workers are less aggressively throttled, providing steady timing ticks.
 */

let timerId = null;
const TICK_INTERVAL_MS = 200;

self.onmessage = function (e) {
  const data = e.data || {};
  if (data.command === 'start') {
    if (timerId !== null) clearInterval(timerId);
    timerId = setInterval(() => {
      self.postMessage({ type: 'tick', timestamp: Date.now() });
    }, TICK_INTERVAL_MS);
  } else if (data.command === 'stop') {
    if (timerId !== null) {
      clearInterval(timerId);
      timerId = null;
    }
  }
};
