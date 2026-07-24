# Handoff v2 — Sub-Agent Rendering Parity

## Goal

Achieve visual parity between **live RPC rendering** (`chat.js`) and **historic session rendering** (`history.js`) for sub-agent tool calls. Both should render a **sub-agent timeline** containing:

- Assistant text messages from the sub-agent
- Tool calls (no results — too verbose), colored by success/failure
- The receipt summary at the bottom
- Turn count (e.g., `4/20 turns`)
- Collapsible card, header shows sub-agent name + task only (no robot emoji)

## Current state

### Live rendering (`static/chat.js`)

Works. Shows the sub-agent card with status text, active tool, and turn count as events stream in. Uses:

- `createSubagentToolItem()` — creates the card with status/activeTool/turns elements
- `updateSubagentToolItem()` — updates status text and active tool on `tool_execution_update`
- `finalizeSubagentToolItem()` — sets final summary from receipt on `tool_execution_end`

### Historic rendering (`static/history.js`)

**Broken.** Shows only the sub-agent name and task with an empty body. The `_collect_subagent_tool_calls()` in `sessions.py` doesn't extract data from the native session format, so `_attach_subagent_details()` attaches nothing, and `renderSubagentTool()` has no data to render.

### Root cause

`_collect_subagent_tool_calls()` in `pi_chat/sessions.py` only handles RPC capture format (records with an `event` key wrapping `tool_execution_update`, `tool_execution_end`, etc.). The native session format stores `type:message` records directly, so the function never matches.

The sub-agent receipt data IS in the session file at line 10 — a `toolResult` message with `PI_SUBAGENT_RECEIPT_V1` + `details.results[0].messages` containing the full sub-agent conversation. It just isn't being extracted.

## Proposed implementation

### Files to change

| File | Changes |
|------|---------|
| `pi_chat/sessions.py` | Fix `_collect_subagent_tool_calls()` for native session format; enrich attached metadata |
| `static/subagent.js` | **NEW** — Shared sub-agent card/timeline DOM factory |
| `static/chat.js` | Refactor sub-agent rendering to use `subagent.js`; update live streaming logic |
| `static/history.js` | Refactor sub-agent rendering to use `subagent.js`; render full timeline from attached data |

---

### 1. `pi_chat/sessions.py` — Fix sub-agent detail collection

**Problem:** `_collect_subagent_tool_calls()` handles RPC capture format only.

**Current logic (lines ~119-145):**
```python
def _collect_subagent_tool_calls(content: list[str]) -> dict[str, dict]:
    for line in content:
        record = json.loads(line)
        event = record.get("event", record)  # Falls back to record for native format
        # Checks event.type == "tool_execution_update", "tool_execution_end", "message"
        # But native format has type == "message" with message.role == "toolResult"
```

For native format, `record.get("event", record)` returns the full record. Then `event.get("type")` returns `"message"`. The current code checks for `message.get("role") == "toolResult"` and `message.get("toolName") == "subagent"` — BUT this check is inside the `event.get("type") == "message"` branch which expects `event` to be an RPC event wrapper. For native format, the record structure is:

```json
{"type": "message", "message": {"role": "toolResult", "toolName": "subagent", ...}}
```

So `event.get("type")` is `"message"` and `event.get("message", {})` is `{"role": "toolResult", ...}`. The current code DOES check this path:

```python
if event.get("type") == "message":
    message = event.get("message", {})
    if (
        message.get("role") == "toolResult"
        and message.get("toolName") == "subagent"
    ):
        _record_subagent_result(subagent_tool_calls, message)
```

**The real issue:** `_record_subagent_result()` expects `message.details.results[0]` to have a `messages` array with tool calls. Looking at the actual data, the native session `toolResult` DOES have this data. So the extraction path might actually work for the native format — the issue could be that `_record_subagent_result()` is being called but the toolCallId hasn't been registered yet (from `tool_execution_update`), so `setdefault` creates an empty entry.

**Fix:** Add native session format handling that:
1. Registers the sub-agent toolCallId from the `toolResult` message even if no `tool_execution_update` was seen
2. Extracts `details.results[0].messages` — the full message array with tool calls
3. Extracts `usage.turns` and `maxTurnsLimit` for turn count
4. Parses the receipt from `content[0].text` for status and summary

**New metadata to attach (via `_attach_subagent_details()`):**

```python
content["_toolCalls"] = details.get("toolCalls", [])       # List of {name, args, isError}
content["_summary"] = details.get("summary")                # Receipt summary text
content["_status"] = details.get("status")                  # "completed", "failed", etc.
content["_isError"] = details.get("isError", False)         # Boolean
content["_timelineMessages"] = details.get("timelineMessages", [])  # Full messages array
content["_turns"] = details.get("turns")                    # e.g., 4
content["_maxTurns"] = details.get("maxTurns")              # e.g., 20
```

---

### 2. New: `static/subagent.js` — Shared DOM factory

```javascript
/**
 * Creates a sub-agent timeline card.
 * @param {string} agentName - Display name of the sub-agent
 * @param {string} task - Task description
 * @param {object} [options] - Optional config
 * @param {boolean} [options.live=false] - If true, show live status elements
 * @returns {{element, header, body, timeline, statusElement, turnsElement, chevron}}
 */
export function createSubagentCard(agentName, task, options = {}) { ... }

/**
 * Adds an assistant text message to the timeline.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} text - The text content
 */
export function addAssistantMessage(timeline, text) { ... }

/**
 * Adds a tool call to the timeline, colored by success/failure.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} toolName - Name of the tool
 * @param {string} [argsDescription] - Truncated args description
 * @param {boolean} [isError=false] - Whether the tool call failed
 */
export function addToolCall(timeline, toolName, argsDescription, isError) { ... }

/**
 * Adds the receipt summary to the timeline.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} summary - The receipt summary text (rendered as markdown)
 * @param {string} [status] - The status ("completed", "failed", etc.)
 * @param {boolean} [isError=false] - Whether the sub-agent failed
 */
export function addSummary(timeline, summary, status, isError) { ... }

/**
 * Adds or updates the turn count display.
 * @param {HTMLElement} timeline - The timeline container
 * @param {number} turns - Current turn count
 * @param {number} [maxTurns] - Maximum turns (optional)
 * @returns {HTMLElement} The turns element for later updates
 */
export function addTurnCount(timeline, turns, maxTurns) { ... }

/**
 * Updates an existing turns element.
 * @param {HTMLElement} element - The turns element returned by addTurnCount
 * @param {number} turns - New turn count
 * @param {number} [maxTurns] - New max turns
 */
export function updateTurnCount(element, turns, maxTurns) { ... }

/**
 * Updates the status text element (live mode only).
 * @param {HTMLElement} element - The status element from createSubagentCard
 * @param {string} text - New status text
 */
export function updateStatus(element, text) { ... }

/**
 * Builds the full timeline from a messages array (historic mode).
 * Iterates messages, adds assistant text and tool calls.
 * @param {HTMLElement} timeline - The timeline container
 * @param {Array} messages - Full messages array from details.results[0].messages
 * @param {string} summary - Receipt summary text
 * @param {string} [status] - Receipt status
 * @param {boolean} [isError=false] - Whether the sub-agent failed
 * @param {number} [turns] - Final turn count
 * @param {number} [maxTurns] - Max turns limit
 */
export function buildHistoricalTimeline(timeline, messages, summary, status, isError, turns, maxTurns) { ... }
```

**Timeline item design:**

```
┌─ Sub-agent card (collapsible)
│  Header: [name] [task] [chevron]
│  Body:
│    ┌─ Timeline
│    │  [connector]
│    │  [assistant-message] "I'm running as outer..."
│    │  [connector]
│    │  [tool-call ✓] agent_status
│    │  [connector]
│    │  [tool-call ✓] bash: echo hello
│    │  [connector]
│    │  [tool-call ✓] bash: date
│    │  [connector]
│    │  [tool-call ✓] bash: whoami
│    │  [connector]
│    │  [tool-call ✓] subagent: inner → "Make 3 bash calls..."
│    │  [connector]
│    │  [assistant-message] "Outer completed. All results..."
│    │  [connector]
│    │  [tool-call ✓] submit_result
│    │  [connector]
│    │  [turn-count] "4/20 turns"
│    │  [connector]
│    │  [summary] (markdown-rendered receipt summary)
│    └─
└─
```

---

### 3. `static/chat.js` — Refactor live rendering

Replace current sub-agent functions:

```javascript
// OLD:
createSubagentToolItem(toolCallId, agentName, task)
updateSubagentToolItem(toolCallId, event)
finalizeSubagentToolItem(toolCallId, event)

// NEW:
import { createSubagentCard, addAssistantMessage, addToolCall, updateTurnCount, updateStatus, addSummary } from './subagent.js';

// On tool_execution_start (subagent):
  card = createSubagentCard(name, task, { live: true });
  subagentCards.set(toolCallId, card);

// On tool_execution_update (subagent):
  card = subagentCards.get(toolCallId);
  const result = event.partialResult?.details?.results?.[0];
  if (result) {
    // Find NEW messages since last update
    const newMessages = result.messages.slice(card.lastMessageCount || 0);
    for (const msg of newMessages) {
      if (msg.role === 'assistant') {
        for (const item of msg.content) {
          if (item.type === 'text' && item.text) {
            addAssistantMessage(card.timeline, item.text);
          }
          if (item.type === 'toolCall') {
            addToolCall(card.timeline, item.name, toolCallArgsDescription(item.arguments));
          }
        }
      }
    }
    card.lastMessageCount = result.messages.length;

    // Update turns
    const usage = result.usage || {};
    updateTurnCount(card.turnsElement, usage.turns || 0, result.maxTurnsLimit);

    // Update status text
    const statusText = firstText(result.content || []);
    if (statusText) updateStatus(card.statusElement, statusText);
  }

// On tool_execution_end (subagent):
  card = subagentCards.get(toolCallId);
  const receipt = parseReceipt(event.result?.content);
  addSummary(card.timeline, receipt.summary, receipt.status, event.isError);
```

---

### 4. `static/history.js` — Refactor historic rendering

Replace `renderSubagentTool()`:

```javascript
import { createSubagentCard, buildHistoricalTimeline } from './subagent.js';

function renderSubagentTool(timeline, toolCall, lastItem) {
  const args = toolCall.arguments || {};
  const agentName = args.name || 'sub-agent';
  const task = args.task || '';
  const summary = toolCall._summary || '';
  const status = toolCall._status || '';
  const isError = toolCall._isError || false;
  const messages = toolCall._timelineMessages || [];
  const turns = toolCall._turns;
  const maxTurns = toolCall._maxTurns;

  const card = createSubagentCard(agentName, task);
  // Append to parent timeline with connector
  if (lastItem) addConnector(timeline, lastItem);
  timeline.appendChild(card.element);

  buildHistoricalTimeline(
    card.timeline,
    messages,
    summary,
    status,
    isError,
    turns,
    maxTurns
  );
}
```

---

## Nested sub-agent analysis task

The following analysis was attempted via sub-agent but failed due to sub-agent instability. A Python script should be written and run manually.

### Data files

- `/home/bbilbro/pi-chat/data-samples/full-nested-sub-agent-tool-result.json` — Full `toolResult` message (line 10 of session file) as single JSON. Contains the complete outer sub-agent execution including nested inner sub-agent.
- `/home/bbilbro/pi-chat/data-samples/full-nested-sub-agent-tool-result-from-rpc.json` — Same data from the RPC `agent_end` event.
- `/home/bbilbro/pi-chat/data-samples/nested_subagent_rpc_capture.jsonl` — Full RPC capture. Lines 374-410 contain the live streaming `tool_execution_update` events for the outer sub-agent with nested inner data.

### Questions to answer

**A. Historic (single JSON) — `full-nested-sub-agent-tool-result.json`:**

The `details.results[0].messages` array contains 10 messages for the outer sub-agent:
- `[0]` assistant: thinking + toolCall(agent_status)
- `[1]` toolResult: agent_status
- `[2]` assistant: thinking + text("I'm running as outer...") + toolCall(bash: echo hello) + toolCall(bash: date) + toolCall(bash: whoami)
- `[3]` toolResult: bash (hello)
- `[4]` toolResult: bash (date output)
- `[5]` toolResult: bash (bbilbro)
- `[6]` assistant: thinking + toolCall(subagent: inner)
- `[7]` toolResult: subagent (inner's PI_SUBAGENT_RECEIPT_V1)
- `[8]` assistant: thinking + text + toolCall(submit_result)
- `[9]` toolResult: submit_result

The inner sub-agent result at `[7]` contains its own `details.results[0].messages` array with inner's full conversation.

Key fields:
- `message.details.results[0].messages` — Full conversation array
- `message.details.results[0].usage.turns` — Turn count (e.g., 4)
- `message.details.results[0].maxTurnsLimit` — Max turns (e.g., 20)
- `message.content[0].text` — Contains `PI_SUBAGENT_RECEIPT_V1\n{...receipt JSON...}`
- Each toolCall in messages has `isError` field for coloring

**B. Live streaming — lines 374-410 of `nested_subagent_rpc_capture.jsonl`:**

The `tool_execution_update` events for the outer sub-agent (toolCallId: `CWu5RSMGRl0iEvYn4o3O9XwR2fY2gFin`) contain `partialResult.details.results[0]` with the same structure as the historic result — `messages`, `usage`, `activeToolExecutions`.

The inner sub-agent appears INSIDE the outer's updates:
- `partialResult.details.results[0].messages[6]` has `toolCall(name: "subagent", args: {name: "inner", ...})`
- `partialResult.details.results[0].messages[7]` has `toolResult(toolName: "subagent", content: [PI_SUBAGENT_RECEIPT_V1...])`

There is NO separate top-level `tool_execution_start`/`tool_execution_end` for the inner sub-agent in the parent's stream. The inner sub-agent's data is embedded inside the outer's `tool_execution_update` events.

**C. Parity:**

Both formats have the same `details.results[0].messages` array with the same structure. The live renderer CAN produce the same timeline as the historic renderer because:
- Both have the full messages array with assistant text + tool calls + isError
- Both have the receipt with summary + status
- Both have usage.turns + maxTurnsLimit

The only difference is timing: live gets incremental updates, historic gets the final result.

**D. Rendering implications for nested sub-agents:**

For the outer sub-agent timeline:
- Show assistant messages: "I'm running as outer...", "Got my 3 results. Now spawning inner."
- Show tool calls: agent_status, bash (x3), subagent (inner), submit_result
- The `subagent` tool call (inner) is just another tool call row — NOT a nested card
- Color each by isError
- Show summary at bottom
- Show turn count

For a deeply nested scenario (outer → inner → deepest), the inner's subagent tool call would similarly be a flat tool call row in the inner's timeline.

---

## Existing code reference

### `sessions.py` — Key functions

- `_collect_subagent_tool_calls(content)` — Iterates JSONL lines, collects sub-agent data into dict keyed by toolCallId
- `_record_subagent_update(dict, event)` — Called on `tool_execution_update`, extracts tool calls from `partialResult.details.results[0]`
- `_record_subagent_result(dict, message)` — Called on `message` with `role:toolResult`, extracts receipt status and tool calls
- `_extract_receipt_status(dict, id, result, isError)` — Parses PI_SUBAGENT_RECEIPT_V1 / PI_SUBAGENT_FAILURE_V1 from text
- `_attach_subagent_details(messages, dict)` — Attaches `_toolCalls`, `_status`, `_summary`, `_isError` to sub-agent toolCall items in messages

### `chat.js` — Current sub-agent functions

- `createSubagentToolItem(toolCallId, agentName, task)` — Creates card with statusElement, activeToolElement, turnsElement
- `updateSubagentToolItem(toolCallId, event)` — Updates status text, active tool, turn count from `partialResult`
- `finalizeSubagentToolItem(toolCallId, event)` — Sets final summary from receipt, marks errors
- `firstText(content)` — Extracts first text item from content array
- `receiptSummary(content)` — Parses PI_SUBAGENT_RECEIPT_V1 JSON from content

### `history.js` — Current sub-agent function

- `renderSubagentTool(timeline, toolCall, lastItem)` — Renders card from `_status`, `_summary`, `_isError`, `_toolCalls` (currently broken — no data attached)
- `extractSubagentToolCalls(toolCall)` — Maps `_toolCalls` to {name, args} (currently returns empty)

### Data sample analysis script

`/home/bbilbro/pi-chat/data-samples/compare_sessions.py` — Original comparison script that identified the root cause. Outputs to `comparison_report.txt`.

---

## Next steps

1. **Write the nested sub-agent analysis** — Run the analysis task described above as a Python script, save report to `data-samples/nested-subagent-report.md`
2. **Fix `sessions.py`** — Update `_collect_subagent_tool_calls()` and `_attach_subagent_details()` for native session format with enriched metadata
3. **Create `static/subagent.js`** — Shared DOM factory with all exported functions listed above
4. **Refactor `chat.js`** — Replace sub-agent functions with `subagent.js` imports
5. **Refactor `history.js`** — Replace `renderSubagentTool()` with `subagent.js` imports
6. **Test** — Verify both live and historic rendering produce identical output for the nested sub-agent sample
