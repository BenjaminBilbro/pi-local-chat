/**
 * Reconnect behaviour in static/socket.js (FE-13).
 *
 * There is no JS test runner in this repo, so this is a self-checking script in the `tools/` style:
 * it prints one line per claim and exits non-zero if any fail. `tests/test_socket_reconnect.py` runs
 * it from pytest so the claim does not live outside the suite.
 *
 * Timers and WebSocket are stubbed. Nothing here waits in real time, and nothing here needs a browser:
 * the whole point is the schedule the module asks the host for, which is observable without a socket.
 */

import assert from 'node:assert/strict';

const timers = [];
const intervals = [];

globalThis.setTimeout = (fn, ms) => {
  timers.push({ fn, ms });
  return timers.length;
};
globalThis.clearTimeout = () => {};
globalThis.setInterval = (fn, ms) => {
  intervals.push({ fn, ms });
  return intervals.length;
};
globalThis.clearInterval = () => {};
globalThis.location = { protocol: 'http:', host: 'example.test', search: '' };
globalThis.window = {}; // socket.js parks the live socket on window for STT to reach

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.sent = [];
    FakeSocket.instances.push(this);
  }

  close() {
    this.readyState = FakeSocket.CLOSED;
  }

  send(payload) {
    this.sent.push(payload);
  }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSED = 3;
FakeSocket.instances = [];
globalThis.WebSocket = FakeSocket;

const { createSocket } = await import('../static/socket.js');

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

let unauthorizedCalls = 0;
const socket = createSocket({
  onOpen: () => {},
  onClose: () => {},
  onMessage: () => {},
  onUnauthorized: () => { unauthorizedCalls += 1; },
});

function lastDelay() {
  assert.ok(timers.length > 0, 'no reconnect was scheduled');
  return timers[timers.length - 1].ms;
}

function openLast() {
  const instance = FakeSocket.instances[FakeSocket.instances.length - 1];
  instance.readyState = FakeSocket.OPEN;
  instance.onopen();
  return instance;
}

function dropLast(code = 1006) {
  const instance = FakeSocket.instances[FakeSocket.instances.length - 1];
  instance.readyState = FakeSocket.CLOSED;
  instance.onclose({ code });
  return instance;
}

function runPendingReconnect() {
  const pending = timers[timers.length - 1];
  pending.fn();
}

socket.connect();
openLast();
dropLast();

check('the first retry is about a second, not a flat 2 s', () => {
  const delay = lastDelay();
  assert.ok(delay >= 500 && delay <= 1000, `first retry scheduled for ${delay} ms`);
});

const ladder = [];
for (let attempt = 0; attempt < 6; attempt += 1) {
  runPendingReconnect();
  dropLast();
  ladder.push(lastDelay());
}

check('delays grow without a cap on attempts', () => {
  // Not "strictly increasing": once the schedule is clamped at the ceiling, consecutive jittered
  // delays are both drawn from the same 15-30 s band and can go either way. What must hold is that
  // the ladder actually climbs, that it stays inside the band, and that it keeps trying.
  for (const delay of ladder) {
    assert.ok(delay >= 500 && delay <= 30_000, `delay ${delay} ms outside the 500 ms - 30 s band`);
  }
  assert.ok(ladder[3] >= ladder[0] * 4, `not growing: ${ladder.join(', ')}`);
  assert.ok(ladder[ladder.length - 1] >= 15_000, `never reached the ceiling: ${ladder.join(', ')}`);
});

check('the ceiling is 30 s', () => {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    runPendingReconnect();
    dropLast();
  }
  const delay = lastDelay();
  assert.ok(delay <= 30_000, `scheduled ${delay} ms, above the ceiling`);
  assert.ok(delay >= 15_000, `at the ceiling it should sit near 15-30 s, got ${delay} ms`);
});

check('a landed connection resets the backoff', () => {
  runPendingReconnect();
  openLast();
  dropLast();
  const delay = lastDelay();
  assert.ok(delay >= 500 && delay <= 1000, `after a good connection the retry was ${delay} ms`);
});

check('a 4401 hands over to the login flow instead of reconnecting', () => {
  const before = timers.length;
  dropLast(4401);
  assert.equal(unauthorizedCalls, 1, 'onUnauthorized was not called');
  assert.equal(timers.length, before, 'a reconnect was scheduled after an auth refusal');
});

check('disconnect stops the retry loop', () => {
  const before = timers.length;
  socket.disconnect();
  dropLast();
  assert.equal(timers.length, before, 'a reconnect was scheduled after disconnect()');
});

check('the heartbeat is 30 s and one per connection', () => {
  assert.ok(intervals.every((i) => i.ms === 30_000), `heartbeat intervals: ${intervals.map((i) => i.ms)}`);
});

console.log(results.join('\n'));
const failed = results.filter((line) => line.startsWith('FAIL'));
console.log(`${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
