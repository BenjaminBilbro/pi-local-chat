# Rendering parity contract

The rule the audit rates CRITICAL, kept in its own file because it is the one place where a correct-looking
change produces a wrong-looking UI with no error anywhere. Extracted from `AGENTS.md` on 2026-11-17; the
section itself is unchanged.

The short version, for anyone who is not going to read the whole page: a message rendered live from a stream
must end up byte-identical to the same message rendered from history after a reload. `tests/compare_render.py`
enforces that by running both renderers over the same fixtures, and it is the test that has caught the most
real regressions in this repository. Run it after changing any parser, event handler, timeline primitive or
sub-agent renderer.

## Rendering Parity Contract (CRITICAL)

Historic and live data arrive differently, but a completed run must produce identical assistant DOM. This is enforced by `npm test`.

### Rules

1. One user prompt and all following assistant/tool-result messages form one **run**. The run gets one `ASSISTANT` label and one connected timeline, even if pi emits multiple `agent_start`/`agent_end` cycles before `agent_settled` while retrying. The one exception is a message delivered mid-run: `message_start` with role `user` closes the open assistant block so the run continues in a fresh block below it, because that is where the session file holds it (see Mid-run Message Delivery).
2. Live deltas render progressively in `chat.js`.
3. `tool_execution_end.result.details.results[0]` is the authoritative final sub-agent snapshot. Live updates rebuild only the small inner sub-agent timeline from each full snapshot.
4. `agent_end.messages` is the authoritative completed top-level run. `chat.js` keeps only messages after the last user record and reconciles the live timeline through `history.js`. Retry segments are overlap-merged until `agent_settled`.
5. `timeline.js` creates thinking, tool, text, connector, and assistant-shell elements for both paths. Do not add a second historic-only or live-only DOM shape.
6. A live-only sub-agent status row is transient. Settled live and historic cards both contain the same message rows, one turns row, and one optional receipt summary. Reconciliation preserves a user's collapsed-card and keyboard-focus state.
7. A tool line's `data-tool-status` is its state and the only source of its mark: `pending` (arguments streamed, execution not announced), `running` (executing, no result), `ok` / `error` (result returned), `unknown` (settled with no result, i.e. aborted). CSS draws the ring or the mark from that attribute; nothing may render a completion mark before `tool_execution_end`, which is what made a long bash call look finished. `timeline.js` owns the attribute, `history.js` derives it from the `isError` that `enrichToolCalls` leaves undefined when no result came back.

### Sub-agent Result Field Mapping

| Display data | Historical/native source | Live RPC source |
|---|---|---|
| Messages | `toolResult.details.results[0].messages` | `tool_execution_end.result.details.results[0].messages` |
| Turns | `details.results[0].usage.turns` | Same path under the final result |
| Turn limit | `details.results[0].maxTurnsLimit` | Same path under the final result |
| Status/summary | `details.results[0].receipt` | Same path under the final result |
| Fallback receipt | `PI_SUBAGENT_*_V1` text | `result.content` marker text |

Always prefer the structured `receipt` object. Some older captures contain Python-style values such as `False` in the marker text, which is not valid JSON.

`sessions.py` returns raw messages (including `toolResult`) without sub-agent enrichment. `subagent.js` performs all sub-agent parsing via `enrichToolCalls()`, which joins `toolResult` records to their corresponding `toolCall` items and attaches `_timelineMessages`, `_status`, `_summary`, `_turns`, `_maxTurns`, and `_isError`. Both historic and live paths use the same JS rendering modules, so a completed run produces identical DOM regardless of source.

### Terminal Conditions

- A failed prompt `response` or WebSocket disconnect is terminal for the current browser-side run. `chat.js` clears transient streaming state and unlocks the composer.
- A successful prompt response that produces no `agent_start` (e.g., a handled extension command) also unlocks after a short grace period.


## A streaming answer jumps up and down (mitigated 2026-11-18, cause still open)

Reported verbally by Ben on 2026-11-18 and reproduced the same day: while an answer streams, the text already
on screen jumps up and down by about 28 px, repeatedly. Only on long answers, and only while streaming.

Measured with `tools/stream_stutter_probe.mjs` (headless Chrome, fake `pi` streaming a 14 KB markdown answer in
48-character deltas, typing a wrapped message halfway through):

```
frames 1149, answer reached 3967 chars; viewport height 711 to 736; composer height 93.6 to 119
  streamed block top   reversals  8   largest 28 px   at 8632, 8682, 11714, 11764, 14765, 14814, 17648, 17699
  scrollTop            reversals  8   largest 28 px   at the same eight frames
  block height         reversals  8   largest 27.6 px at the same eight frames
  viewport height      reversals  0
every frame the block top moved more than 2px: 0 with the viewport, 108 with the block's own height, 1 with neither
```

The composer is not the cause. The viewport's height never reversed, and the two hypotheses that came before
this one, both wrong, were argued rather than measured.

**Mechanism.** `static/chat.js` re-renders the whole streaming block on every batch:
`entry.render = () => setMarkdownContent(entry.element, entry.rawText)`. Partial markdown is not monotonic in
height: a chunk that has not yet been recognised as a list or a heading is a paragraph, and the block that
replaces it carries list or heading margins, about 28 px more. So the block's height oscillates mid-answer.
Because the browser pins the scroll position to the bottom during a stream, a shrink reduces `scrollHeight`,
`scrollTop` is clamped, and everything already read moves down; the regrowth moves it back up.

Two things follow. Replacing the scroll write with a gentler follow does not help, and this was tried: the
reversals are the browser's clamp, not our assignment, and an identical 8 reversals were measured with a
follow-only version. The fix has to stop the height oscillation.

**The fix to reach for.** Render completed blocks once and re-render only the incomplete tail: cut
`rawText` at the last block boundary, keep the prefix's DOM frozen, and put the tail in its own element. A
chunk that ends at a blank line cannot be reinterpreted later, so the prefix stops changing and the height
stops swinging. It also removes the cost the stub's own docstring complains about, 359 render passes and 298
times the answer's length handed to the sanitizer for a 4.2 KB answer, and a history render is already a
single whole-block render, so live and reload stay identical as long as the completed block ends by calling
`setMarkdownContent` once with the final text.

**Do not fix it by padding the block to a high-water height.** That holds the scroll still and leaves trailing
whitespace inside the answer's bubble, which is a visible artifact in the one place the user is looking.

Re-run the measurement with:

    PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=14000 FAKE_RPC_TURN_MS=45 \
      uv run python server.py
    node tools/stream_stutter_probe.mjs --url http://127.0.0.1:9022 --password test-only \
      --seconds 20 --type-after 5000 --fail-on-reversals 2

The answer has to be long enough to overflow the viewport. A 952-character answer produced zero reversals with
the defect present, which is how the first measurement of this bug misled me.

### The mitigation that shipped, and what it does not cure

Ben picked the cheap trade on 2026-11-18 and it is the right one: **the moment you type while an answer is
streaming, the transcript stops following the bottom.** Your reading position becomes yours, and the `Latest`
pill is how you get back. `static/stream-follow.js` owns the rules (typing during a run releases the follow;
scrolling to the bottom, sending, or the run settling takes it again), and the pill threshold drops from 240px
to 40px while it is released, because it is the only way back and 240px is too polite a bar for that.

Re-measured with the same harness, same server, same 14 KB answer:

```
                    before the fix      after
while typing        2 reversals         0
after typing        8 reversals         0     (block height still reverses 8 times, nothing moves)
frames that moved   108                 11    (only the window before the first keystroke)
```

The block's height oscillates exactly as much as before. What changed is that the scroll position no longer
takes the oscillation, so the jump is gone in the case that made it worth reporting: reading an answer while
sending another one. **Watching an answer with the follow still engaged still jitters**, and the cure for that
is the frozen-prefix render described above. It is worth doing on its own terms for the sanitizer cost alone,
but it is not urgent now that typing is calm.
