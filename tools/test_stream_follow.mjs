/* Transcript-follow rules (static/stream-follow.js).
 *
 * The state machine is small, and every rule in it exists because the alternative is worse in a way a reader
 * feels: suspend too little and the answer keeps yanking the text you are reading; suspend too eagerly and
 * the transcript freezes mid-answer and never comes back, which is a much more confusing bug than a jitter.
 * So the resume paths get claimed as hard as the suspend path.
 *
 * Run: node tools/test_stream_follow.mjs   (pytest wrapper: tests/test_stream_follow.py)
 */
import assert from 'node:assert/strict';
import {
  createStreamFollow, PILL_NORMAL_PX, PILL_WHILE_SUSPENDED_PX, RESUME_WITHIN_PX,
} from '../static/stream-follow.js';

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

function harness() {
  const events = [];
  const follow = createStreamFollow({
    onSuspend: () => events.push('suspend'),
    onResume: () => events.push('resume'),
  });
  return { follow, events };
}

await check('typing while an answer streams releases the follow', () => {
  const { follow, events } = harness();
  follow.typed({ running: true });
  assert.equal(follow.suspended, true);
  assert.equal(follow.shouldFollow, false);
  assert.deepEqual(events, ['suspend']);
});

await check('typing with nothing running does not release it', () => {
  const { follow, events } = harness();
  follow.typed({ running: false });
  assert.equal(follow.suspended, false, 'nothing is moving, and a stranded pill is worse than a jitter');
  assert.deepEqual(events, []);
});

await check('scrolling back to the bottom takes the follow again', () => {
  const { follow } = harness();
  follow.typed({ running: true });
  follow.scrolled({ distanceFromBottomPx: RESUME_WITHIN_PX });
  assert.equal(follow.shouldFollow, true, 'scrolling to the bottom is how the reader asks to be followed');
});

await check('scrolling partway back does not take it', () => {
  const { follow } = harness();
  follow.typed({ running: true });
  follow.scrolled({ distanceFromBottomPx: RESUME_WITHIN_PX + 40 });
  assert.equal(follow.shouldFollow, false, 'halfway back is still reading something');
});

await check('the run settling resumes it', () => {
  const { follow, events } = harness();
  follow.typed({ running: true });
  follow.settled();
  assert.equal(follow.shouldFollow, true);
  assert.deepEqual(events, ['suspend', 'resume']);
});

await check('sending a message resumes it', () => {
  const { follow } = harness();
  follow.typed({ running: true });
  follow.sent();
  assert.equal(follow.shouldFollow, true, 'a message of your own means you are at the bottom');
});

await check('the pill earns its place much sooner while the follow is released', () => {
  const { follow } = harness();
  const distance = PILL_WHILE_SUSPENDED_PX + 10;
  assert.equal(follow.pillShouldShow({ distanceFromBottomPx: distance, hasScrollableContent: true }), false,
    'before typing, that distance is not yet worth a control');
  follow.typed({ running: true });
  assert.equal(follow.pillShouldShow({ distanceFromBottomPx: distance, hasScrollableContent: true }), true,
    'it is the only way back to the answer, so it shows');
  follow.sent();
  assert.equal(follow.pillShouldShow({ distanceFromBottomPx: distance, hasScrollableContent: true }), false);
});

await check('no scrollable content means no pill, even while released', () => {
  const { follow } = harness();
  follow.typed({ running: true });
  assert.equal(follow.pillShouldShow({ distanceFromBottomPx: 4000, hasScrollableContent: false }), false,
    'the whole answer already fits; a pill pointing at nothing is a lie');
});

await check('typing fifty characters announces one suspension', () => {
  const { follow, events } = harness();
  for (let i = 0; i < 50; i += 1) follow.typed({ running: true });
  follow.scrolled({ distanceFromBottomPx: 900 });
  follow.scrolled({ distanceFromBottomPx: 800 });
  assert.deepEqual(events, ['suspend'], 'every keystroke firing an event is how listeners get churned');
});

await check('the normal threshold stays where the design put it', () => {
  const { follow } = harness();
  assert.equal(PILL_NORMAL_PX, 240, 'static/app.js hid the pill below this for a reason: reading, not flicker');
  assert.equal(PILL_WHILE_SUSPENDED_PX < RESUME_WITHIN_PX, true,
    'the pill has to appear before the resume point, or it never gets a chance to');
});

console.log(`${passed}/${total} claims passed`);
process.exit(passed === total ? 0 : 1);
