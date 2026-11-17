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

