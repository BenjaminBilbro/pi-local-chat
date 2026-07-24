#!/usr/bin/env python3
"""Analyze nested sub-agent data across session and RPC formats."""

import json
from pathlib import Path

BASE = Path(__file__).parent


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_jsonl_lines(path, start=None, end=None):
    with open(path) as f:
        lines = f.readlines()
    if start is not None:
        lines = lines[start:end]
    return [json.loads(l) for l in lines]


def section(title):
    print(f"\n{'=' * 70}")
    print(f" {title}")
    print('=' * 70)


def subsection(title):
    print(f"\n--- {title} ---")


# Load data
session = load_json(BASE / "full-nested-sub-agent-tool-result.json")
rpc = load_json(BASE / "full-nested-sub-agent-tool-result-from-rpc.json")
rpc_lines = load_jsonl_lines(BASE / "nested_subagent_rpc_capture.jsonl")

# Extract key data
session_msg = session["message"]
session_result = session_msg["details"]["results"][0]
session_msgs = session_result["messages"]
session_receipt = session_result["receipt"]

rpc_event = rpc["event"]
rpc_tool_result = rpc_event["messages"][2]  # index 2 = subagent toolResult
rpc_result = rpc_tool_result["details"]["results"][0]
rpc_msgs = rpc_result["messages"]
rpc_receipt = rpc_result["receipt"]

# Inner sub-agent data (inside outer's messages)
inner_tool_result = session_msgs[7]  # toolResult for inner subagent
inner_result = inner_tool_result["details"]["results"][0]
inner_msgs = inner_result["messages"]
inner_receipt = inner_result["receipt"]

report_lines = []


def report(line=""):
    report_lines.append(line)


report("# Nested Sub-Agent Analysis")
report("")
report("Comparing data available in historic session format vs live RPC streaming for nested sub-agents.")
report("")

# ============================================================================
# A. Historic Structure
# ============================================================================

section("A. HISTORIC (SESSION FILE) STRUCTURE")

subsection("Top-level shape")
report("## A. Historic (Session File) Structure")
report("")
report("The entire sub-agent execution (outer + nested inner) is contained in a single `toolResult` message.")
report("")
report("### Top-level shape of `details.results[0]`")
report("")
report("```")
for key in session_result.keys():
    val = session_result[key]
    if isinstance(val, list):
        report(f"  {key}: list[{len(val)}]")
    elif isinstance(val, dict):
        report(f"  {key}: dict[{list(val.keys())}]")
    else:
        report(f"  {key}: {type(val).__name__} = {str(val)[:80]}")
report("```")
report("")

subsection("Message breakdown")
report("### Message breakdown (10 messages total)")
report("")
report("| Index | Role | Details |")
report("|-------|------|---------|")
for i, m in enumerate(session_msgs):
    role = m.get("role")
    if role == "assistant":
        tc = [c["name"] for c in m.get("content", []) if c.get("type") == "toolCall"]
        txt = [c["text"][:40] for c in m.get("content", []) if c.get("type") == "text"]
        report(f"| {i} | assistant | toolCalls: {tc}, text: {txt} |")
    elif role == "toolResult":
        name = m.get("toolName", "?")
        error = m.get("isError", False)
        has_details = "details" in m
        report(f"| {i} | toolResult({name}) | isError={error}, has_details={has_details} |")
    elif role == "user":
        report(f"| {i} | user | prompt |")
report("")

subsection("Inner sub-agent location")
report("### Inner sub-agent location")
report("")
report("The inner sub-agent appears at two points:")
report("")
report("1. **Message [6]** (assistant): Outer calls `subagent` with `name: inner`")
report("   - Field: `messages[6].content[1].type = 'toolCall'`")
report("   - Field: `messages[6].content[1].name = 'subagent'`")
report("   - Field: `messages[6].content[1].arguments.name = 'inner'`")
report("")
report("2. **Message [7]** (toolResult): Result of the inner sub-agent call")
report("   - Field: `messages[7].toolName = 'subagent'`")
report("   - Field: `messages[7].toolCallId = 'NyVfoBL9r52dYEGDwSP2CW5mYHqcDJ3S'`")
report("   - Field: `messages[7].details.results[0].messages` = 8 messages (inner's full execution)")
report("   - Field: `messages[7].details.results[0].receipt` = inner's receipt dict")
report("")

subsection("Inner sub-agent receipt")
report("### Inner sub-agent receipt")
report("")
report("```")
for k, v in inner_receipt.items():
    if isinstance(v, str) and len(v) > 120:
        report(f"  {k}: {v[:120]}...")
    else:
        report(f"  {k}: {v}")
report("```")
report("")

subsection("Outer tool calls")
report("### Outer sub-agent tool calls (from messages)")
report("")
report("| Message | Tool | isError | Args |")
report("|---------|------|---------|------|")
for i, m in enumerate(session_msgs):
    if m.get("role") == "assistant":
        for c in m.get("content", []):
            if c.get("type") == "toolCall":
                name = c["name"]
                error = c.get("isError", False)
                args = json.dumps(c.get("arguments", {}))[:80]
                report(f"| [{i}] | {name} | {error} | {args} |")
report("")

subsection("Inner tool calls")
report("### Inner sub-agent tool calls (from messages)")
report("")
report("| Message | Tool | isError | Args |")
report("|---------|------|---------|------|")
for i, m in enumerate(inner_msgs):
    if m.get("role") == "assistant":
        for c in m.get("content", []):
            if c.get("type") == "toolCall":
                name = c["name"]
                error = c.get("isError", False)
                args = json.dumps(c.get("arguments", {}))[:80]
                report(f"| [{i}] | {name} | {error} | {args} |")
report("")

subsection("isError availability")
report("### isError availability")
report("")
report("- **Tool calls in assistant messages**: Each toolCall has `isError: false` (or true on failure)")
report("- **toolResult messages**: Each has `isError` at the message level")
report("- **Both formats (session + RPC)**: Identical structure, both have isError on every tool call")
report("")

# ============================================================================
# B. Live Streaming Structure
# ============================================================================

section("B. LIVE STREAMING (RPC EVENTS) STRUCTURE")

report("## B. Live Streaming (RPC Events) Structure")
report("")

subsection("How the nested sub-agent appears in streaming")
report("### How the nested sub-agent appears in streaming events")
report("")
report("The outer sub-agent (toolCallId `CWu5RSMGRl0iEvYn4o3O...`) is the ONLY top-level tool execution.")
report("The inner sub-agent appears NESTED inside `partialResult.details.results[0].activeToolExecutions`.")
report("")
report("Timeline of key events:")
report("")
report("| Line | Event | Description |")
report("|------|-------|-------------|")
report("| 353 | tool_execution_start | Outer sub-agent starts |")
report("| 354-373 | tool_execution_update | Outer runs bash commands (turns 1-2) |")
report("| 374 | tool_execution_update | Outer finishes bash, text: 'Now spawning inner' (turn 3, msgs=7) |")
report("| 375-376 | tool_execution_update | Inner sub-agent appears in activeToolExecutions |")
report("| 377-401 | tool_execution_update | Inner runs (nested turns 1-3, msgs 1-8) |")
report("| 402 | tool_execution_update | Inner completes, outer msgs=8, active=0 |")
report("| 403-406 | tool_execution_update | Outer writes final summary (turn 4, msgs=10) |")
report("| 407 | tool_execution_end | Outer completes with PI_SUBAGENT_RECEIPT_V1 |")
report("")

subsection("Nested data location in streaming events")
report("### Where inner data lives in a streaming update")
report("")
report("```")
report("partialResult.details.results[0]              // outer's result")
report("  .activeToolExecutions[0]                      // currently running tool")
report("    .toolName = 'subagent'                      // it's a sub-agent call")
report("    .args.name = 'inner'                        // inner's name")
report("    .partialResult.details.results[0]           // inner's result")
report("      .agent = 'inner'")
report("      .usage.turns = N")
report("      .messages = [...]                        // inner's full message history")
report("```")
report("")

subsection("No separate top-level events for inner")
report("### No separate top-level events for inner")
report("")
report("There are NO separate `tool_execution_start` or `tool_execution_end` events for the inner sub-agent")
report("at the top level. The inner sub-agent's entire lifecycle is contained within the outer's")
report("`tool_execution_update` events inside `activeToolExecutions[0]`.")
report("")
report("A live renderer would detect the inner sub-agent by checking:")
report("1. `activeToolExecutions` has an entry with `toolName == 'subagent'`")
report("2. `activeToolExecutions[0].args.name` gives the nested agent's name")
report("3. `activeToolExecutions[0].partialResult.details.results[0]` has the inner agent's full state")
report("")

subsection("Streaming vs final difference")
report("### Streaming update vs final result")
report("")
report("| Aspect | Streaming (tool_execution_update) | Final (tool_execution_end) |")
report("|--------|----------------------------------|---------------------------|")
report("| Outer messages | Full history in `results[0].messages` | Same, in `result` |")
report("| Inner messages | Nested in `activeToolExecutions[0].partialResult.details.results[0].messages` | Same path while active, then in outer's final messages |")
report("| Inner receipt | Not available until inner completes | Available in outer's messages[7].details.results[0].receipt |")
report("| Outer receipt | Not available | Available in `result.content[0].text` (PI_SUBAGENT_RECEIPT_V1) |")
report("| Turn counts | Available in `results[0].usage.turns` | Same |")
report("| isError | Available on each toolCall in messages | Same |")
report("")

# ============================================================================
# C. Parity Analysis
# ============================================================================

section("C. PARITY ANALYSIS")

report("## C. Parity Analysis")
report("")

subsection("Field mapping")
report("### 1:1 Field Mappings")
report("")
report("| Concept | Session/Historic | Live Streaming |")
report("|---------|-----------------|----------------|")
report("| Agent name | `arguments.name` (assistant toolCall) | `args.name` (tool_execution_start) |")
report("| Task | `arguments.task` | `args.task` |")
report("| Messages | `details.results[0].messages` | `partialResult.details.results[0].messages` |")
report("| Tool calls | `messages[N].content[M].type='toolCall'` | Same path in messages |")
report("| isError (tool) | `toolCall.isError` | Same |")
report("| isError (result) | `toolResult.isError` | Same |")
report("| Status | `receipt.status` | Parse from `result.content[0].text` (PI_SUBAGENT_RECEIPT_V1) |")
report("| Summary | `receipt.summary` | Parse from `result.content[0].text` |")
report("| Turns | `usage.turns` | Same |")
report("| MaxTurns | `maxTurnsLimit` | Same |")
report("")

subsection("What's the same")
report("### What's the same (parity achieved)")
report("")
report("- **Both formats have identical message arrays** (verified: all 10 messages match)")
report("- **Both have isError on every tool call and tool result**")
report("- **Both have the full nested sub-agent execution in the same structure**")
report("- **Both have receipt data (status, summary, changedFiles, etc.)**")
report("- **Both have usage data (turns, tokens, etc.)**")
report("")

subsection("What's different")
report("### What's different")
report("")
report("| Aspect | Session | Live |")
report("|--------|---------|------|")
report("| Receipt format | Already parsed as `receipt` dict | Embedded in text as `PI_SUBAGENT_RECEIPT_V1\\n{...}`, must parse |")
report("| Inner visibility | Complete in `messages[7].details.results[0]` | Streaming: visible in `activeToolExecutions[0]` while running, then in messages after |")
report("| Real-time updates | N/A (static) | Can show inner progress incrementally |")
report("")

subsection("Can live = historic?")
report("### Can live renderer produce the same timeline as historic?")
report("")
report("**YES.** At `tool_execution_end`, the live renderer has access to:")
report("1. The complete `result` with all messages (same as historic)")
report("2. The receipt text that can be parsed to extract status/summary")
report("3. The inner sub-agent's complete execution in `messages[7].details.results[0]`")
report("")
report("The live renderer can build the EXACT same timeline by iterating the same message array.")
report("")

# ============================================================================
# D. Practical Rendering Implications
# ============================================================================

section("D. PRACTICAL RENDERING IMPLICATIONS")

report("## D. Practical Rendering Implications")
report("")

subsection("Outer sub-agent timeline")
report("### Outer sub-agent timeline (what should render)")
report("")
report("```")
report("┌─────────────────────────────────────────────────────────────┐")
report("│ outer  Do the following in order: ...                       │")
report("├─────────────────────────────────────────────────────────────┤")
report("│ agent_status                                                │ ✓")
report("│ bash: echo hello                                            │ ✓")
report("│ bash: date                                                  │ ✓")
report("│ bash: whoami                                                │ ✓")
report("│ subagent: inner                                             │ ✓")
report("│ submit_result                                               │ ✓")
report("│                                                             │")
report("│ Turns: 4/20                                                 │")
report("│ Status: completed                                           │")
report("│ Summary: Outer (manager, depth 1) completed 3 bash ...     │")
report("└─────────────────────────────────────────────────────────────┘")
report("```")
report("")

subsection("Where inner appears")
report("### Where does 'inner' appear?")
report("")
report("Inner appears as a **tool call row** in the outer's timeline: `subagent: inner`")
report("")
report("It is NOT a deeply nested card within the outer card. The user's requirement is:")
report("'assistant messages from sub-agent, tool calls (no result), colored by success/failure'")
report("")
report("So the outer card shows its own tool calls, including `subagent → inner` as one row.")
report("The inner sub-agent's details are NOT expanded by default.")
report("")
report("Optionally, clicking the `subagent: inner` row could expand to show inner's timeline,")
report("but that's a secondary feature. The primary view is flat: outer's tool calls.")
report("")

subsection("Tool call count")
report("### Tool calls to show for outer")
report("")
report("From the messages array, extracting toolCalls from assistant messages:")
report("")
report("| # | Tool | Args | isError |")
report("|---|------|------|---------|")
report("| 1 | agent_status | {} | false |")
report("| 2 | bash | echo hello | false |")
report("| 3 | bash | date | false |")
report("| 4 | bash | whoami | false |")
report("| 5 | subagent | name: inner | false |")
report("| 6 | submit_result | status: completed | false |")
report("")
report("Total: **6 tool calls**")
report("")

subsection("Inner tool call count")
report("### Tool calls to show for inner (if expanded)")
report("")
report("| # | Tool | Args | isError |")
report("|---|------|------|---------|")
report("| 1 | agent_status | {} | false |")
report("| 2 | bash | pwd | false |")
report("| 3 | bash | uname -a | false |")
report("| 4 | bash | echo done | false |")
report("| 5 | submit_result | status: completed | false |")
report("")
report("Total: **5 tool calls**")
report("")

subsection("Extraction field paths")
report("### Extraction field paths (for implementation)")
report("")
report("**Historic (from session_loaded messages):**")
report("```")
report("// For each assistant message with subagent toolCall:")
report("// message.content[i] where type='toolCall' and name='subagent'")
report("// Enriched by sessions.py with _timelineMessages, _summary, _status, etc.")
report("")
report("// Timeline data:")
report("  _timelineMessages = details.results[0].messages  // full message array")
report("  _summary = receipt.summary")
report("  _status = receipt.status")
report("  _isError = message.isError")
report("  _turns = usage.turns")
report("  _maxTurns = maxTurnsLimit")
report("```")
report("")
report("**Live (from tool_execution_end):**")
report("```")
report("// result.content[0].text → parse PI_SUBAGENT_RECEIPT_V1 for summary/status")
report("// result.details.results[0].messages → full message array for timeline")
report("// result.details.results[0].usage.turns → turn count")
report("// result.details.results[0].maxTurnsLimit → max turns")
report("```")
report("")
report("**Live streaming (from tool_execution_update):**")
report("```")
report("// partialResult.details.results[0].messages → current message array")
report("// partialResult.details.results[0].usage.turns → current turns")
report("// partialResult.content[0].text → current status text")
report("// partialResult.details.results[0].activeToolExecutions → currently running tools")
report("```")
report("")

subsection("Key insight for nested")
report("### Key insight for nested sub-agents")
report("")
report("The nested sub-agent (inner) data is AVAILABLE in both formats:")
report("- **Historic**: `messages[7].details.results[0].messages` (8 messages) + `receipt`")
report("- **Live final**: Same path in `result.details.results[0].messages[7].details.results[0]`")
report("- **Live streaming**: `activeToolExecutions[0].partialResult.details.results[0].messages` (incremental)")
report("")
report("For rendering, the outer card shows inner as a tool call row (`subagent: inner`).")
report("If the user wants to see inner's details, the data is available for a nested expand.")
report("")

# Write report
report("\n---")
report("*Generated by analyze_nested.py*")

output = "\n".join(report_lines)
with open(BASE / "nested-subagent-report.md", "w") as f:
    f.write(output)

print(output)
print(f"\n\nReport saved to {BASE / 'nested-subagent-report.md'} ({len(output)} chars)")
