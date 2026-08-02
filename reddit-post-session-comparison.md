# r/LocalLLaMA or r/PiCodingAgent Post Draft

**Title idea:** Ran the exact same coding task through Qwen3.6-27B twice with pi — same model, same prompt, wildly different behavior. Here's the breakdown.

---

So I had this idea to actually measure how consistent local LLM agents are, instead of just saying "it works good sometimes."

Ran the exact same prompt through pi + Qwen3.6-27B twice, same day, same system prompt, same tools. Task was to add PDF/TXT file upload support to my pi-chat app. Both sessions spawned a subagent to do the heavy lifting.

Both completed the task but the behavioral differences were wild. Here's what I measured:

## Setup

- Model: Qwen3.6-27B (same GGUF both runs)
- Harness: pi coding agent with subagent extension
- Prompt: identical, copy-pasted same text
- Hardware: 4090, running locally via llama.cpp

## Key Metrics

| Metric | Session 1 | Session 2 |
|--------|-----------|-----------|
| Total tokens | 104,055 | 110,975 |
| Main agent tokens | 83,808 | 88,288 |
| Subagent tokens | 20,247 | 22,687 |
| Total tool calls | 127 | 121 |
| Failed tool calls | 11 | 7 |
| Assistant messages | 98 | 95 |
| Unique files read | 12 | 15 |
| "wait"/"actually" in reasoning | 9 | 27 |

## What stood out

**Session 1 was more decisive.** Only 9 occurrences of "wait" or "actually" in all the reasoning text across both main agent and subagent. Session 2 said those words 27 times — triple the hesitation. You can literally see it second-guessing itself more.

**Session 1 had more failures but finished cheaper.** 11 failed tool calls vs 7, but used ~7% fewer total tokens. Some of those failures were from trying to spawn sub-subagents (which failed, for context), but it still came out ahead on efficiency.

**Session 2 was more exploratory.** Read 3 more unique files, had longer average reasoning (237 chars vs 164), and didn't attempt nested subagents. It was more cautious overall.

**Both subagents failed to properly complete.** Session 1's subagent got aborted mid-run. Session 2's subagent exited without calling submit_result (which is required in my extension). But both still did enough work that the task was functionally complete.

## Tool distribution

Session 1: bash (95), read (14), edit (13), subagent (3), telegram_attach (1), agent_status (1)

Session 2: bash (84), read (18), edit (14), telegram_attach (2), subagent (1), agent_status (1), write (1)

Session 1 went hard on bash commands and tried to orchestrate more aggressively. Session 2 read more files before making changes, which makes sense given the more hesitant reasoning.

## My take

This is what I expected honestly — local models at this size are not deterministic in their behavior, even with the same seed. The variance in hesitation alone (9 vs 27 "wait"/"actually" occurrences) is something I think would be interesting to track across more runs.

I'm curious if anyone else has done similar head-to-head runs with the same model on the same task. Would be interesting to see if this variance holds across different models or if it's specific to qwen at this parameter count.

Also happy to share the full session JSONL files if anyone wants to dig into the raw data.

---

*Edit: tokens are calculated as main agent's final cumulative totalTokens + (subagent contextTokens - main agent tokens before subagent call). Subagent tokens for session 1 are estimated from input+output since it was aborted and had no contextTokens value.*

---

## Code Review: What each session actually shipped

Since both sessions were given the same task (add PDF/TXT file upload support), I went through and compared the actual code changes. Both branches work, but they took completely different approaches.

### Session 1: `feature/file-uploads`

**Approach:** Process files in the websocket handler, no new HTTP endpoint.

**Files changed:** 5 files (+299 lines)
- `pi_chat/config.py` — added UPLOAD_DIR and UPLOAD_TTL_SECONDS settings
- `pi_chat/websocket.py` — added `_process_files`, `_save_uploaded_file`, `_cleanup_old_uploads`
- `static/index.html` — replaced image-input with file-input, added attachment-preview
- `static/chat.js` — refactored pendingImage to pendingFiles array
- `static/styles.css` — attachment-preview, attachment-item, file-badge styles

**How it works:**
- Frontend reads ALL files (images, PDFs, TXTs) as base64/DataURL
- Sends them in the prompt command as a `files` array
- Backend websocket handler processes them:
  - TXT files: content embedded directly in prompt as `<file name="...">...</file>`
  - PDF files: saved raw to `~/.pi-chat/uploads/` with UUID filename, path referenced in prompt
- Auto-cleanup of uploads older than 1 hour
- Supports multiple file selection (`multiple` attribute on input)

**Issues:**
- PDF text is never extracted — it just saves the raw PDF and tells the model "here's a path." So the model would have to read the PDF file itself, which defeats the purpose of sending content in-context.
- No file size limits enforced
- No validation on file types (trusts the frontend)
- Cleanup runs before every save, which is fine for low traffic but weird pattern

### Session 2: `feature/file-upload-pdf-txt`

**Approach:** Dedicated `/api/upload-file` endpoint with PDF text extraction.

**Files changed:** 6 files (+666 lines)
- `pi_chat/app.py` — added POST `/api/upload-file` endpoint
- `pyproject.toml` — added `pdfplumber` and `python-multipart` dependencies
- `static/index.html` — kept image-preview separate, added file-attachments div
- `static/chat.js` — separate handlers for images vs documents
- `static/styles.css` — file-chip styles (pill-shaped)
- `uv.lock` — dependency lock file

**How it works:**
- Frontend separates files on selection:
  - Images: read as DataURL, shown in existing image preview
  - PDF/TXT: uploaded via POST to `/api/upload-file`
- Backend endpoint:
  - Validates extension is .pdf or .txt
  - Enforces 5MB file size limit
  - TXT: decode as UTF-8, return content
  - PDF: extract text using pdfplumber, return content
  - Returns JSON with filename, content, mimeType
- Frontend stores returned content in pendingFiles
- On submit, appends file content to message as `<file name="...">...</file>`

**Issues:**
- Only supports single file selection (no `multiple` attribute)
- Only supports one image at a time (kept original image-preview behavior)
- File content is embedded directly in the prompt, which could blow up context for large files
- No cleanup mechanism (though files aren't persisted so this is fine)

### Head-to-head

| Aspect | Session 1 | Session 2 |
|--------|-----------|-----------|
| PDF handling | Saves raw file, references path | Extracts text with pdfplumber |
| Multiple files | Yes | No (single select) |
| File size limit | None | 5MB |
| Type validation | None | Checks extension |
| New dependencies | None | pdfplumber, python-multipart |
| Code added | 299 lines | 666 lines |
| Architecture | Websocket handler | REST endpoint |

Session 2's approach is actually more correct for the use case — extracting PDF text server-side means the model gets the actual content in-context instead of just a file path. But it's also heavier (new dependencies, more code) and more limited (no multi-select).

Session 1's approach is cleaner architecturally (no new endpoint, fewer deps) but the PDF handling is basically broken — telling the model "read this PDF at /path/to/file" is not the same as giving it the content.

Honestly the ideal solution is Session 2's PDF extraction + Session 1's multi-select + some context size guardrails. Neither agent thought that far ahead.

This is another example of how the same model given the same prompt will arrive at legitimately different architectural decisions. Neither is wrong per se, but they optimize for different things.
