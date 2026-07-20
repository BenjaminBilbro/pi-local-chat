# pi --mode rpc Event Format

Complete reference of all JSON events emitted by `pi --mode rpc`, captured from live runs.

## Lifecycle Events

### `response`
Acknowledges a command was accepted.
```json
{
  "type": "response",
  "command": "prompt",
  "success": true
}
```

### `agent_start`
Agent loop begins (after prompt accepted).
```json
{ "type": "agent_start" }
```

### `agent_end`
Agent loop finishes. Includes full message history.
```json
{
  "type": "agent_end",
  "messages": [
    { "role": "user", "content": [{ "type": "text", "text": "..." }] },
    { "role": "assistant", "content": [...] },
    ...
  ]
}
```

### `agent_settled`
Final signal — agent is fully done and idle.
```json
{ "type": "agent_settled" }
```

### `extension_ui_request`
Extension UI state requests (e.g., telegram status).
```json
{
  "type": "extension_ui_request",
  "id": "uuid",
  "method": "setStatus",
  "statusKey": "telegram",
  "statusText": "telegram standby (another session is polling)"
}
```

## Turn Events

### `turn_start`
A new LLM turn begins.
```json
{ "type": "turn_start" }
```

### `turn_end`
Turn completes. Includes the full assistant message with usage stats.
```json
{
  "type": "turn_end",
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "thinking",
        "thinking": "Full reasoning text...",
        "thinkingSignature": "reasoning_content"
      },
      {
        "type": "toolCall",
        "id": "emOm4wpcphrqYAVP6iLyews7uIWlIoLA",
        "name": "bash",
        "arguments": { "command": "ls -la" }
      }
    ],
    "api": "openai-completions",
    "provider": "llama-cpp",
    "model": "Qwen3.6-27B.gguf",
    "usage": {
      "input": 225,
      "output": 55,
      "cacheRead": 10240,
      "cacheWrite": 0,
      "reasoning": 0,
      "totalTokens": 10520,
      "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0 }
    },
    "stopReason": "stop",
    "timestamp": 1784576790000
  }
}
```

## Message Events

### `message_start`
A message begins. The `message` object contains the full message content.

**User message:**
```json
{
  "type": "message_start",
  "message": {
    "role": "user",
    "content": [{ "type": "text", "text": "What files are in the current directory?" }],
    "timestamp": 1784576789268
  }
}
```

**Tool result message:**
```json
{
  "type": "message_start",
  "message": {
    "role": "toolResult",
    "toolCallId": "emOm4wpcphrqYAVP6iLyews7uIWlIoLA",
    "toolName": "bash",
    "content": [{ "type": "text", "text": ".venv/\nserver.py\n..." }],
    "isError": false,
    "timestamp": 1784576792291
  }
}
```

### `message_end`
Message completed. Same shape as `message_start`.

## Streaming Updates (`message_update`)

All streaming updates wrap an `assistantMessageEvent` with a `type` subtype:

### `thinking_start` — Reasoning block begins
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "thinking_start",
    "contentIndex": 0,
    "partial": { "role": "assistant", "content": [...], "usage": {...} }
  }
}
```

### `thinking_delta` — Incremental reasoning text
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "thinking_delta",
    "contentIndex": 0,
    "delta": "The",          // incremental chunk
    "partial": { ... }        // full message so far
  }
}
```

### `thinking_end` — Reasoning block complete
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "thinking_end",
    "contentIndex": 0,
    "content": "Full reasoning text here.\n",  // complete text
    "partial": { ... }
  }
}
```

### `text_start` — Response text block begins
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "text_start",
    "contentIndex": 1,
    "partial": { ... }
  }
}
```

### `text_delta` — Incremental response text
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "text_delta",
    "contentIndex": 1,
    "delta": "Here",         // incremental chunk
    "partial": { ... }
  }
}
```

### `text_end` — Response text complete
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "text_end",
    "contentIndex": 1,
    "content": "Here's what's in the directory...\n",  // complete text
    "partial": { ... }
  }
}
```

### `toolcall_start` — Tool call begins
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "toolcall_start",
    "contentIndex": 1,
    "partial": {
      "content": [
        { "type": "thinking", "thinking": "...", "thinkingSignature": "reasoning_content" },
        { "type": "toolCall", "id": "...", "name": "bash", "arguments": {}, "partialArgs": "{" }
      ]
    }
  }
}
```

### `toolcall_delta` — Incremental tool arguments
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "toolcall_delta",
    "contentIndex": 1,
    "delta": "{",            // incremental arg chunk
    "partial": { ... }
  }
}
```

### `toolcall_end` — Tool call complete
```json
{
  "type": "message_update",
  "assistantMessageEvent": {
    "type": "toolcall_end",
    "contentIndex": 1,
    "toolCall": {
      "type": "toolCall",
      "id": "emOm4wpcphrqYAVP6iLyews7uIWlIoLA",
      "name": "bash",
      "arguments": { "command": "ls -la" }
    },
    "partial": { ... }
  }
}
```

## Tool Execution Events

### `tool_execution_start`
Tool begins executing.
```json
{
  "type": "tool_execution_start",
  "toolCallId": "emOm4wpcphrqYAVP6iLyews7uIWlIoLA",
  "toolName": "bash",
  "args": { "command": "ls -la" }
}
```

### `tool_execution_update`
Progress during tool execution (may have `partialResult`).
```json
{
  "type": "tool_execution_update",
  "toolCallId": "...",
  "toolName": "bash",
  "args": { "command": "ls -la" },
  "partialResult": { "content": [] }
}
```

### `tool_execution_end`
Tool execution complete with result.
```json
{
  "type": "tool_execution_end",
  "toolCallId": "...",
  "toolName": "bash",
  "result": {
    "content": [{ "type": "text", "text": ".venv/\nserver.py\n..." }]
  },
  "isError": false
}
```

## Full Flow Sequence

```
prompt sent → response → agent_start → turn_start
  → message_start(user) → message_end(user)
  → message_start(assistant)
    → message_update(thinking_start)
    → message_update(thinking_delta) × N
    → message_update(toolcall_start)
    → message_update(toolcall_delta) × N
    → message_update(thinking_end)
    → message_update(toolcall_end)
  → message_end(assistant)
  → tool_execution_start
  → tool_execution_update × N
  → tool_execution_end
  → message_start(toolResult) → message_end(toolResult)
  → turn_end
  → turn_start (next turn after tool result)
    → (repeat assistant message cycle with text_start/text_delta/text_end)
  → turn_end
→ agent_end → agent_settled
```
