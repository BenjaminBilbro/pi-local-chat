# Nested Sub-Agent Analysis

Comparing data available in historic session format vs live RPC streaming for nested sub-agents.

## A. Historic (Session File) Structure

The entire sub-agent execution (outer + nested inner) is contained in a single `toolResult` message.

### Top-level shape of `details.results[0]`

```
  agent: str = outer
  task: str = Do the following in order:

1. Make these 3 simple tool calls and capture their 
  taskSpec: dict[['kind', 'protocolVersion', 'taskId', 'name', 'objective', 'scope', 'nonGoals', 'acceptance', 'verification']]
  frame: dict[['kind', 'protocolVersion', 'rootRunId', 'runId', 'parentRunId', 'role', 'name', 'taskId', 'depth', 'maxDepth', 'stack', 'taskStack', 'deadlineAtMs', 'maxTurns', 'ledgerPath', 'preventCycles']]
  exitCode: int = 0
  messages: list[10]
  stderr: str = 
  stderrTruncated: bool = False
  usage: dict[['input', 'output', 'cacheRead', 'cacheWrite', 'cost', 'contextTokens', 'turns']]
  model: str = Qwen3.6-27B.gguf
  sawAgentStart: bool = True
  sawAgentEnd: bool = True
  sawAgentSettled: bool = True
  maxTurnsLimit: int = 20
  receiptRequired: bool = True
  receipt: dict[['kind', 'protocolVersion', 'receiptId', 'rootRunId', 'runId', 'parentRunId', 'taskId', 'role', 'name', 'submittedAtMs', 'status', 'summary', 'changedFiles', 'checks', 'artifacts', 'unresolved']]
  receiptTruncated: bool = False
  ledgerPath: str = /tmp/pi-subagent-e685b698-T1VFwc/ledger.jsonl
  startedAtMs: int = 1784827601220
  finishedAtMs: int = 1784827626442
```

### Message breakdown (10 messages total)

| Index | Role | Details |
|-------|------|---------|
| 0 | assistant | toolCalls: ['agent_status'], text: [] |
| 1 | toolResult(agent_status) | isError=False, has_details=True |
| 2 | assistant | toolCalls: ['bash', 'bash', 'bash'], text: ["I'm running as **outer** (manager, depth"] |
| 3 | toolResult(bash) | isError=False, has_details=False |
| 4 | toolResult(bash) | isError=False, has_details=False |
| 5 | toolResult(bash) | isError=False, has_details=False |
| 6 | assistant | toolCalls: ['subagent'], text: ['Got my 3 results. Now spawning **inner**'] |
| 7 | toolResult(subagent) | isError=False, has_details=True |
| 8 | assistant | toolCalls: ['submit_result'], text: ['All done. Here are all results:\n\n**outer'] |
| 9 | toolResult(submit_result) | isError=False, has_details=True |

### Inner sub-agent location

The inner sub-agent appears at two points:

1. **Message [6]** (assistant): Outer calls `subagent` with `name: inner`
   - Field: `messages[6].content[1].type = 'toolCall'`
   - Field: `messages[6].content[1].name = 'subagent'`
   - Field: `messages[6].content[1].arguments.name = 'inner'`

2. **Message [7]** (toolResult): Result of the inner sub-agent call
   - Field: `messages[7].toolName = 'subagent'`
   - Field: `messages[7].toolCallId = 'NyVfoBL9r52dYEGDwSP2CW5mYHqcDJ3S'`
   - Field: `messages[7].details.results[0].messages` = 8 messages (inner's full execution)
   - Field: `messages[7].details.results[0].receipt` = inner's receipt dict

### Inner sub-agent receipt

```
  kind: pi-subagent-receipt
  protocolVersion: 1
  receiptId: e254421a-f2af-493b-bd1c-e6b4521bbf93
  rootRunId: e685b698-3e65-487d-acb9-fd1d9d66544e
  runId: 6abbdafa-d2c6-4953-a735-c64db2634091
  parentRunId: 4913c81e-86f0-4031-a86b-f8dc00bd4a47
  taskId: inner-CW5mYHqcDJ3S
  role: worker
  name: inner
  submittedAtMs: 1784827619037
  status: completed
  summary: **inner (worker, depth 2) — 3 tool call results:**

1. `pwd` → `/home/bbilbro/pi-chat/sessions/b`
2. `uname -a` → `Linux...
  changedFiles: []
  checks: []
  artifacts: []
  unresolved: []
```

### Outer sub-agent tool calls (from messages)

| Message | Tool | isError | Args |
|---------|------|---------|------|
| [0] | agent_status | False | {} |
| [2] | bash | False | {"command": "echo hello"} |
| [2] | bash | False | {"command": "date"} |
| [2] | bash | False | {"command": "whoami"} |
| [6] | subagent | False | {"name": "inner", "task": "Make these 3 simple bash tool calls and report all ou |
| [8] | submit_result | False | {"status": "completed", "summary": "Outer (manager, depth 1) completed 3 bash co |

### Inner sub-agent tool calls (from messages)

| Message | Tool | isError | Args |
|---------|------|---------|------|
| [0] | agent_status | False | {} |
| [2] | bash | False | {"command": "pwd"} |
| [2] | bash | False | {"command": "uname -a"} |
| [2] | bash | False | {"command": "echo done"} |
| [6] | submit_result | False | {"status": "completed", "summary": "**inner (worker, depth 2) \u2014 3 tool call |

### isError availability

- **Tool calls in assistant messages**: Each toolCall has `isError: false` (or true on failure)
- **toolResult messages**: Each has `isError` at the message level
- **Both formats (session + RPC)**: Identical structure, both have isError on every tool call

## B. Live Streaming (RPC Events) Structure

### How the nested sub-agent appears in streaming events

The outer sub-agent (toolCallId `CWu5RSMGRl0iEvYn4o3O...`) is the ONLY top-level tool execution.
The inner sub-agent appears NESTED inside `partialResult.details.results[0].activeToolExecutions`.

Timeline of key events:

| Line | Event | Description |
|------|-------|-------------|
| 353 | tool_execution_start | Outer sub-agent starts |
| 354-373 | tool_execution_update | Outer runs bash commands (turns 1-2) |
| 374 | tool_execution_update | Outer finishes bash, text: 'Now spawning inner' (turn 3, msgs=7) |
| 375-376 | tool_execution_update | Inner sub-agent appears in activeToolExecutions |
| 377-401 | tool_execution_update | Inner runs (nested turns 1-3, msgs 1-8) |
| 402 | tool_execution_update | Inner completes, outer msgs=8, active=0 |
| 403-406 | tool_execution_update | Outer writes final summary (turn 4, msgs=10) |
| 407 | tool_execution_end | Outer completes with PI_SUBAGENT_RECEIPT_V1 |

### Where inner data lives in a streaming update

```
partialResult.details.results[0]              // outer's result
  .activeToolExecutions[0]                      // currently running tool
    .toolName = 'subagent'                      // it's a sub-agent call
    .args.name = 'inner'                        // inner's name
    .partialResult.details.results[0]           // inner's result
      .agent = 'inner'
      .usage.turns = N
      .messages = [...]                        // inner's full message history
```

### No separate top-level events for inner

There are NO separate `tool_execution_start` or `tool_execution_end` events for the inner sub-agent
at the top level. The inner sub-agent's entire lifecycle is contained within the outer's
`tool_execution_update` events inside `activeToolExecutions[0]`.

A live renderer would detect the inner sub-agent by checking:
1. `activeToolExecutions` has an entry with `toolName == 'subagent'`
2. `activeToolExecutions[0].args.name` gives the nested agent's name
3. `activeToolExecutions[0].partialResult.details.results[0]` has the inner agent's full state

### Streaming update vs final result

| Aspect | Streaming (tool_execution_update) | Final (tool_execution_end) |
|--------|----------------------------------|---------------------------|
| Outer messages | Full history in `results[0].messages` | Same, in `result` |
| Inner messages | Nested in `activeToolExecutions[0].partialResult.details.results[0].messages` | Same path while active, then in outer's final messages |
| Inner receipt | Not available until inner completes | Available in outer's messages[7].details.results[0].receipt |
| Outer receipt | Not available | Available in `result.content[0].text` (PI_SUBAGENT_RECEIPT_V1) |
| Turn counts | Available in `results[0].usage.turns` | Same |
| isError | Available on each toolCall in messages | Same |

## C. Parity Analysis

### 1:1 Field Mappings

| Concept | Session/Historic | Live Streaming |
|---------|-----------------|----------------|
| Agent name | `arguments.name` (assistant toolCall) | `args.name` (tool_execution_start) |
| Task | `arguments.task` | `args.task` |
| Messages | `details.results[0].messages` | `partialResult.details.results[0].messages` |
| Tool calls | `messages[N].content[M].type='toolCall'` | Same path in messages |
| isError (tool) | `toolCall.isError` | Same |
| isError (result) | `toolResult.isError` | Same |
| Status | `receipt.status` | Parse from `result.content[0].text` (PI_SUBAGENT_RECEIPT_V1) |
| Summary | `receipt.summary` | Parse from `result.content[0].text` |
| Turns | `usage.turns` | Same |
| MaxTurns | `maxTurnsLimit` | Same |

### What's the same (parity achieved)

- **Both formats have identical message arrays** (verified: all 10 messages match)
- **Both have isError on every tool call and tool result**
- **Both have the full nested sub-agent execution in the same structure**
- **Both have receipt data (status, summary, changedFiles, etc.)**
- **Both have usage data (turns, tokens, etc.)**

### What's different

| Aspect | Session | Live |
|--------|---------|------|
| Receipt format | Already parsed as `receipt` dict | Embedded in text as `PI_SUBAGENT_RECEIPT_V1\n{...}`, must parse |
| Inner visibility | Complete in `messages[7].details.results[0]` | Streaming: visible in `activeToolExecutions[0]` while running, then in messages after |
| Real-time updates | N/A (static) | Can show inner progress incrementally |

### Can live renderer produce the same timeline as historic?

**YES.** At `tool_execution_end`, the live renderer has access to:
1. The complete `result` with all messages (same as historic)
2. The receipt text that can be parsed to extract status/summary
3. The inner sub-agent's complete execution in `messages[7].details.results[0]`

The live renderer can build the EXACT same timeline by iterating the same message array.

## D. Practical Rendering Implications

### Outer sub-agent timeline (what should render)

```
┌─────────────────────────────────────────────────────────────┐
│ outer  Do the following in order: ...                       │
├─────────────────────────────────────────────────────────────┤
│ agent_status                                                │ ✓
│ bash: echo hello                                            │ ✓
│ bash: date                                                  │ ✓
│ bash: whoami                                                │ ✓
│ subagent: inner                                             │ ✓
│ submit_result                                               │ ✓
│                                                             │
│ Turns: 4/20                                                 │
│ Status: completed                                           │
│ Summary: Outer (manager, depth 1) completed 3 bash ...     │
└─────────────────────────────────────────────────────────────┘
```

### Where does 'inner' appear?

Inner appears as a **tool call row** in the outer's timeline: `subagent: inner`

It is NOT a deeply nested card within the outer card. The user's requirement is:
'assistant messages from sub-agent, tool calls (no result), colored by success/failure'

So the outer card shows its own tool calls, including `subagent → inner` as one row.
The inner sub-agent's details are NOT expanded by default.

Optionally, clicking the `subagent: inner` row could expand to show inner's timeline,
but that's a secondary feature. The primary view is flat: outer's tool calls.

### Tool calls to show for outer

From the messages array, extracting toolCalls from assistant messages:

| # | Tool | Args | isError |
|---|------|------|---------|
| 1 | agent_status | {} | false |
| 2 | bash | echo hello | false |
| 3 | bash | date | false |
| 4 | bash | whoami | false |
| 5 | subagent | name: inner | false |
| 6 | submit_result | status: completed | false |

Total: **6 tool calls**

### Tool calls to show for inner (if expanded)

| # | Tool | Args | isError |
|---|------|------|---------|
| 1 | agent_status | {} | false |
| 2 | bash | pwd | false |
| 3 | bash | uname -a | false |
| 4 | bash | echo done | false |
| 5 | submit_result | status: completed | false |

Total: **5 tool calls**

### Extraction field paths (for implementation)

**Historic (from session_loaded messages):**
```
// For each assistant message with subagent toolCall:
// message.content[i] where type='toolCall' and name='subagent'
// Enriched by sessions.py with _timelineMessages, _summary, _status, etc.

// Timeline data:
  _timelineMessages = details.results[0].messages  // full message array
  _summary = receipt.summary
  _status = receipt.status
  _isError = message.isError
  _turns = usage.turns
  _maxTurns = maxTurnsLimit
```

**Live (from tool_execution_end):**
```
// result.content[0].text → parse PI_SUBAGENT_RECEIPT_V1 for summary/status
// result.details.results[0].messages → full message array for timeline
// result.details.results[0].usage.turns → turn count
// result.details.results[0].maxTurnsLimit → max turns
```

**Live streaming (from tool_execution_update):**
```
// partialResult.details.results[0].messages → current message array
// partialResult.details.results[0].usage.turns → current turns
// partialResult.content[0].text → current status text
// partialResult.details.results[0].activeToolExecutions → currently running tools
```

### Key insight for nested sub-agents

The nested sub-agent (inner) data is AVAILABLE in both formats:
- **Historic**: `messages[7].details.results[0].messages` (8 messages) + `receipt`
- **Live final**: Same path in `result.details.results[0].messages[7].details.results[0]`
- **Live streaming**: `activeToolExecutions[0].partialResult.details.results[0].messages` (incremental)

For rendering, the outer card shows inner as a tool call row (`subagent: inner`).
If the user wants to see inner's details, the data is available for a nested expand.


---
*Generated by analyze_nested.py*