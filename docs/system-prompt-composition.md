# What a pi-chat session's system prompt is made of

Measured on this machine, 2026-11-09, against a real child process rather than read off the source:
`~/.pi/agent/tmp/sysdump.mjs` spawns `pi --mode rpc` exactly the way `pi_chat/process.py` does and
asks the child, through an extension command, for `ctx.getSystemPrompt()`. Extension commands run
without a model call, so mapping the prompt costs nothing. It writes the whole prompt to
`sysdump-<account>.json.raw.txt` plus a JSON sidecar with section sizes, tool names, context files,
skills and the byte sizes of the injected memory sections. Re-run it after changing anything that
could move the prompt.

## The spawn

```
node <npm prefix>/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
  --mode rpc --approve --no-context-files \
  --extension <repo>/pi_chat/extension/preview.ts
cwd = <repo>/sessions/<account>      env: PI_CHAT_PREVIEW_DIR=<per-connection scope>, PI_CHAT_ACCOUNT=<account>
```

No `--append-system-prompt`, for either profile. A CLI append **replaces** the discovered
`<agent-dir>/APPEND_SYSTEM.md` rather than adding to it, which is how a persona-only append silently
deleted the memory protocol from profile `r`'s sessions. Until 2026-11-17 the fix was to pass both files;
now nothing is passed, pi discovers the addendum itself, and that profile's persona arrives through the
memory extension instead (injected on `PI_CHAT_ACCOUNT=r` by a persona rule in
`~/.pi/agent/lib/memory-core.ts`). The persona file that used to live in this repo is deleted. If a
profile ever needs a prompt piece again, the memory extension is where it goes.

- `--approve` trusts project-local files for the run. Project resources are read from the child's
  cwd only (`<cwd>/.pi/...`), and there is no `.pi` in `sessions/<account>`, so today it trusts
  nothing; it is what keeps a hand-placed `.pi/SYSTEM.md` working without a TTY prompt.
- `--no-context-files` is the reason there is no `project_context` section below. pi walks up from
  the cwd looking for `AGENTS.md`, and the child's cwd is inside the repo, so without this every
  chat inherited the 39 KB developer card. That card is for a pi session *developing on* pi-chat,
  not one *talking through* it. `PI_CHAT_CONTEXT_FILES=1` puts it back.
- `--append-system-prompt` **replaces** pi's discovery of the agent dir's `APPEND_SYSTEM.md` rather
  than adding to it (`resource-loader.js`: a CLI source suppresses file discovery). Passing only
  one profile's persona therefore used to delete the memory and project-context protocol from its
  sessions.
  The flag is repeatable and pi joins the list, so both files go on the command line, persona last.

## The sections, measured

Account `b`, after the change. `size` is the section body; `offset` is where it starts in the prompt,
which is what decides how much of the prefix a change invalidates (see the cache section).

| section | size | offset | where it comes from |
|---|---|---|---|
| preamble (untagged head) | 171 | 0 | pi's built-in line, replaced only by `.pi/SYSTEM.md` or `~/.pi/agent/SYSTEM.md` (neither exists here) |
| `tools` | 1,155 | 171 | one line per tool that declares a snippet, from the tools below |
| `rules` | 2,416 | 1,336 | pi's built-in rules + `toolGuidelines` contributed by each loaded extension/package |
| `docs` | 1,387 | 3,762 | pi's own pointer block at its installed docs path |
| `addendum` | 1,998 | 5,158 | `~/.pi/agent/APPEND_SYSTEM.md`, discovered, same for both profiles |
| `skills` | 4,746 | 7,169 | 15 skills, `~/.agents/skills/*` plus the pi-subagent package's `skills/` |
| `available_skills` | 4,389 | 7,506 | the router list (name, description, location) for those skills |
| `cwd` | 45 | 11,926 | `C:/Users/<user>/pi-chat/sessions/<account>`, the length column being what this machine's real path measured |
| memory sections | 22,177 | 11,977→ | `~/.pi/agent/extensions/memory.ts`, appended by the extension, not by pi |
| `user_profile` | 1,894 | | `memories/USER.md` |
| `core_memory` | 5,576 | | `memories/CORE.md` |
| `env_notes` | 4,101 | | `memories/ENV.md` |
| `now` | 2,241 | | `memories/NOW.md` |
| `recent` | 1,395 | | `memories/recent/*.md`, newest first, 7-day window |
| `memory_index` | 6,970 | | `memories/INDEX.md` + a footer naming the snapshot time |
| **total** | **34,154** | | account `r`: 35,899 |

Before `--no-context-files` the total was 73,677 chars for `b`, of which 39,529 was
`project_context` = `pi-chat/AGENTS.md`. Roughly 10k tokens of architecture doc on every request of
every chat, including the second profile's.

## The 16 declared tools

| tool | registered by |
|---|---|
| `read`, `bash`, `edit`, `write` | pi built-ins, the default loadout |
| `codemode` | pi built-in, enabled by `defaultTools: ["+codemode"]` in `~/.pi/agent/settings.json` |
| `preview` | `pi_chat/extension/preview.ts`, loaded with `--extension`, so only pi-chat children get it |
| `memory` | `~/.pi/agent/extensions/memory.ts` |
| `telegram_send`, `telegram_send_to` | `~/.pi/agent/extensions/telegram.ts` |
| `questionnaire` | package `npm:@that-yolanda/pi-questionnaire` |
| `web_search` | package `npm:pi-smart-web-search` |
| `web_fetch`, `batch_web_fetch` | package `npm:pi-smart-fetch` |
| `subagent`, `agent_status`, `submit_result` | package `git:github.com/BenjaminBilbro/pi-subagent@strata-compat` |

Everything in `~/.pi/agent/{settings.json,models.json,extensions}` is global, so a chat child has the
same reach as a terminal session: the whole memory store, Telegram sends, sub-agents, and the web.
`tool-guard.ts` loads too (it guards tool calls and registers no tool), as does
`pi-prefix-cache-compaction` (it replaces compaction and registers no tool). Two consequences worth
knowing rather than rediscovering:

- `telegram.ts` auto-connects on `session_start` and tries for the poll lock. A pi-chat child sits in
  standby while a leader holds it, then takes over within ~15 s if the lock goes free. A chat window
  can therefore become the Telegram bridge for the bot without anyone asking it to.
- RPC prompts arrive at extensions with `source: "rpc"` rather than `"interactive"`, which is the hook
  an input handler can use to treat the two surfaces differently.

## Does a memory write mid-conversation re-prefill the next call

No, and two separate mechanisms make sure of it.

1. `memory.ts` reads the Tier-1 files once, at `session_start`, into a snapshot, and every later run
   hands `before_agent_start` those same bytes. A `memory put` in turn 4 changes disk, not the prompt.
   Only `/memory refresh`, a new session, or `/reload` re-takes the snapshot; the command says so when
   it does, because it knows it is spending the cache once.
2. Even if a section *did* change, pi does not rewrite the leading system prompt. The prompt lives in
   the transcript as a `system` entry with structured `sections`; each run diffs the freshly built
   sections against the ones the model already has (`diffSystemPromptSections`) and appends a second
   system message carrying only the changed sections. The head stays byte-identical, so the prefix
   cache survives a genuine mid-session prompt change.

What that leaves as the real cost:

- **Within one session**: memory writes are free. A retrieval hint is a transcript message *after* the
  prompt, so it extends the tail and costs its own tokens, nothing more.
- **Between sessions**: nothing "reprocesses" - a new session is a cold start regardless - but the
  engine can reuse the shared prefix across sessions only up to the first changed byte. The memory
  block is last, so an unchanged store plus a changed `NOW.md` costs `now` + `recent` + `memory_index`
  ≈ 10.6 KB of cold prefix, out of 34 KB total. A changed `CORE.md` costs everything from offset
  ~13.9 KB on. A content-free change costs only the snapshot footer, which sits at the very end of
  `memory_index`: about 200 chars.
- So writing memory early in a session is not expensive and writing it late is not free either; the
  cost lands on the *next* session and is proportional to the size of the section you touched.
  `topics/strata-inference.md` covers the engine side: one global pin per engine, and another client
  on :8081 moves it.

Two ways the prompt moves that are easy to miss: editing anything under `~/.pi/agent/`
(`APPEND_SYSTEM.md`, an extension, a skill's front matter) changes `addendum`, `rules` or `skills`
near the front, invalidating most of the prefix; and loading or unloading a tool changes `tools` at
offset 171, which invalidates everything.

## Why memory hints kept appearing, and what fixes it

The fired-hint sets in `memory.ts` are module state, cleared at `session_start`. "Once per session"
was therefore true only inside one pi process, and pi-chat does not map a conversation onto a
process: it spawns one per WebSocket, so a page refresh, a dropped connection with its 2 s
auto-reconnect, a New-session click or loading a saved session all start a brand-new session in a
brand-new process with empty sets. Each of those re-nudged at notes the conversation had already been
told about. So the instinct was right, but the RPC invocation was not the culprit - the process
lifecycle around it was. The session files under
`~/.pi/agent/sessions/--C--Users-<user>-pi-chat-sessions-b--/` show that churn directly.

The fix is to stop trusting process memory: the delivered hint records the rule ids it carried in its
own session entry (`details.inject`, `details.suggest`, `details.auto`), and `session_start` rebuilds
the fired state by reading the transcript branch (`getBranch()`), including which notes the
conversation already opened. A resumed session now knows what it already said.

Two deliberate edges:

- Everything before the newest `compaction` entry is ignored. A summarize takes the delivered blocks
  out of context, which is exactly when a hint should be able to speak again - the same reason
  `session_before_compact` clears the sets.
- Hints delivered by a build before this change carry no ids, so they are not remembered. One session
  of drift, no code path needed.

`/memory rules` reports what came back: `re-armed from transcript: 1 injected, 1 suggested, 2 note(s)
already read`.

## Where each piece lives

| piece | file |
|---|---|
| spawn argv, queue and run mirror | `pi_chat/process.py` |
| prompt shape tests | `tests/test_child_argv.py` |
| memory sections, snapshot, hints | `~/.pi/agent/extensions/memory.ts` + `~/.pi/agent/lib/memory-core.ts` |
| global pi config that the children inherit | `~/.pi/agent/settings.json` (packages, defaultTools, default model), `~/.pi/agent/models.json` (the `strata` provider) |
| the injected addendum | `~/.pi/agent/APPEND_SYSTEM.md` |
| the second profile's persona, and how its child gets it | a persona note under `~/.pi/agent/memories/`, injected by the memory extension's persona rule on `PI_CHAT_ACCOUNT=r` |
| re-measure the prompt | `~/.pi/agent/tmp/sysdump.mjs` + `sysdump-ext.ts` |
