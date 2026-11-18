/* Geometry sampler claims (static/geometry-watch.js).
 *
 * The module exists so a report like "the text jumps when my message wraps" becomes numbers in the audit
 * log. What has to be true: it costs nothing when nothing is moving, it does not move the layout it is
 * measuring, and it says nothing to a production server.
 *
 * Run: node tools/test_geometry_watch.mjs   (pytest wrapper: tests/test_geometry_watch.py)
 */
import assert from 'node:assert/strict';

let passed = 0;
let total = 0;

async function check(name, fn) {
  total += 1;
  try {
    await fn();
    console.log(`PASS ${passed + 1}: ${name}`);
    passed += 1;
  } catch (error) {
    console.log(`FAIL: ${name}\n  ${error.message.split('\n').slice(0, 6).join('\n  ')}`);
  }
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = '';
    this.id = '';
    this.hidden = false;
    this.parentNode = null;
    this.rect = { width: 100, height: 40, top: 500 };
    this.metrics = { clientHeight: 400, scrollHeight: 1200, scrollTop: 800 };
  }

  appendChild(node) {
    node.parentNode = this;
    this.children.push(node);
    return node;
  }

  getBoundingClientRect() {
    return { ...this.rect };
  }

  get clientHeight() { return this.metrics.clientHeight; }
  get scrollHeight() { return this.metrics.scrollHeight; }
  get scrollTop() { return this.metrics.scrollTop; }
  set scrollTop(value) { this.metrics.scrollTop = value; }

  querySelector(selector) {
    if (selector.endsWith(':last-of-type') || selector.endsWith(':last-child')) {
      const messages = this.children.filter((child) => child.className.includes('message'));
      return messages.at(-1) || null;
    }
    return null;
  }
}

function harness({ dev = true, versionStatus = 200 } = {}) {
  const body = new El('body');
  const scroller = new El('div');
  scroller.className = 'messages';
  const field = new El('textarea');
  field.id = 'user-input';
  const pill = new El('button');
  pill.id = 'steer-btn';
  pill.hidden = true;
  const tail = new El('div');
  tail.className = 'message assistant';
  scroller.appendChild(tail);
  body.appendChild(scroller);

  const nodes = { '#user-input': field, '.messages': scroller, '#steer-btn': pill };
  const listeners = {};
  const frames = [];
  let clock = 1000;

  const doc = {
    body,
    querySelector: (selector) => {
      if (nodes[selector]) return nodes[selector];
      // The sampler looks for the last message inside the scroller, which only the scroller can answer.
      if (selector.startsWith('.messages')) return scroller.querySelector(selector);
      return null;
    },
    addEventListener: (type, handler) => { (listeners[type] ||= []).push(handler); },
  };
  const win = {
    innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} },
    addEventListener: () => {},
  };

  const events = [];
  const watch = module_.createGeometryWatch({
    sendEvent: (kind, data) => events.push({ kind, data }),
    fetchVersion: async () => (versionStatus === 200
      ? { ok: true, json: async () => ({ dev }) }
      : { ok: false, status: versionStatus }),
    doc,
    win,
    now: () => clock,
    requestFrame: (callback) => { frames.push(callback); return frames.length; },
  });

  const runFrames = (count, advance = 16) => {
    for (let i = 0; i < count; i += 1) {
      clock += advance;
      const callback = frames.shift();
      callback?.();
      if (!watch.watching) break;
    }
  };

  const fire = (type, event) => (listeners[type] || []).forEach((handler) => handler(event));

  return { watch, events, field, pill, scroller, tail, doc, win, fire, runFrames, advance: (ms) => { clock += ms; } };
}

const module_ = await import('../static/geometry-watch.js');

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(3);

  await check('a still page reports nothing', () => {
    assert.deepEqual(h.events, [], 'the first sample is a baseline, not an event');
  });

  await check('a page that never moves stops sampling', () => {
    h.runFrames(120);
    assert.equal(h.watch.watching, false, 'the loop is still running after 1.5s of stillness');
    assert.deepEqual(h.events, []);
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(1);
  h.tail.rect = { width: 700, height: 120, top: 480 };
  h.runFrames(2);

  await check('a moved block is reported with the composer and the scroller', () => {
    assert.equal(h.events.length, 1);
    const { kind, data } = h.events[0];
    assert.equal(kind, 'geometry');
    assert.equal(data.tail.t, 480);
    assert.deepEqual(data.field, { w: 100, h: 40, t: 500 });
    assert.deepEqual(data.view, { h: 400, content: 1200, top: 800, gap: 0 });
    assert.equal(data.viewport, 900);
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(1);
  for (let i = 0; i < 20; i += 1) {
    h.tail.rect = { width: 700, height: 120, top: 500 - i };
    h.runFrames(1);
  }

  await check('a moving edge is rate limited, not one line per frame', () => {
    assert.ok(h.events.length > 0, '20 frames of movement produced nothing');
    assert.ok(h.events.length <= 4, `emitted ${h.events.length} events in 20 frames of 16ms`);
    assert.deepEqual(h.events.map((event) => event.data.seq), [1, 2, 3].slice(0, h.events.length));
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(1);
  h.pill.hidden = false;
  h.runFrames(1);

  await check('the Send now pill is reported even though it changes no pixels of its own', () => {
    assert.equal(h.events.at(-1)?.data.pill, true);
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(1);
  h.scroller.metrics.scrollTop = 770;
  h.runFrames(1);

  await check('scroll position is part of the sample', () => {
    assert.equal(h.events.at(-1)?.data.view.top, 770);
    assert.equal(h.events.at(-1)?.data.view.gap, 30);
  });
}

{
  const h = harness({ dev: false });
  await h.watch.ready;
  h.runFrames(2);
  h.tail.rect = { width: 700, height: 120, top: 10 };
  h.runFrames(3);

  await check('a non-dev server hears nothing', () => {
    assert.deepEqual(h.events, []);
    assert.equal(h.watch.watching, false);
  });
}

{
  const h = harness({ versionStatus: 404 });
  await h.watch.ready;
  h.runFrames(2);

  await check('a server without the endpoint is silent, not an exception', async () => {
    assert.deepEqual(h.events, []);
    await Promise.resolve(h.watch.ready);
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.runFrames(120);
  assert.equal(h.watch.watching, false, 'the loop should have idled out after 1.5s of stillness');
  h.fire('input', { target: { id: 'user-input' } });

  await check('typing re-arms a sampler that idled out', () => {
    assert.equal(h.watch.watching, true);
  });
}

{
  const h = harness();
  await h.watch.ready;
  h.field.rect = { width: 100, height: 40, top: 500 };
  const before = h.doc.body.children.length;
  h.watch.sample();

  await check('measuring writes nothing to the layout it is measuring', () => {
    assert.equal(h.doc.body.children.length, before);
    assert.equal(h.scroller.scrollTop, 800, 'reading the scroller must not move it');
  });
}

console.log(`${passed}/${total} claims passed`);
process.exit(passed === total ? 0 : 1);
