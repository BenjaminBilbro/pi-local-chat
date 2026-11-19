/**
 * preview.ts: the model-facing half of the pi-chat document preview pane.
 *
 * Loaded by pi_chat/process.py with `--extension <this file>` (plus PI_CHAT_PREVIEW_DIR pointing at
 * this connection's store directory), so it exists only for sessions the web UI started. It does
 * two things:
 *
 *   1. Registers the `preview` tool, which is how the assistant opens and revises a document.
 *   2. Registers `/preview`, the same operations for the human typing in the chat box.
 *
 * Content moves one way and pointers the other. The bytes are written to PI_CHAT_PREVIEW_DIR by
 * this process, and the session only ever carries a small entry:
 *
 *   pi.appendEntry("pichat_preview", { op, docId, kind, title, revision, path? , file? })
 *
 * pi emits that as `entry_appended` on RPC stdout, which pi-chat forwards to the browser, and it
 * also lands in the session JSONL, so reopening a session rebuilds the pane. Custom entries never
 * enter model context, so a 40 KB document costs no prompt tokens.
 *
 * A document is either *referenced* (an existing file the server is allowed to serve) or *stored*
 * (bytes this extension wrote into the preview directory). Updating a referenced document writes a
 * stored copy rather than touching the user's original file, which is the only destructive thing
 * this tool could have done.
 *
 * Payload contract is mirrored in pi_chat/preview.py and static/preview.js; bump `v` in three
 * places, not one.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, extname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const ENTRY_TYPE = "pichat_preview";
/** Mirrored in pi_chat/preview.py (`ENTRY_FIELDS`) and static/preview.js (`normalize`). */
const PROTOCOL_VERSION = 2;
/** Keep in step with PI_CHAT_PREVIEW_MAX_DOC_MB on the server. */
const MAX_DOC_BYTES = 25 * 1024 * 1024;

const KIND_BY_SUFFIX: Record<string, DocKind> = {
  ".md": "markdown",
  ".markdown": "markdown",
  ".html": "html",
  ".htm": "html",
  ".pdf": "pdf",
  ".png": "image",
  ".jpg": "image",
  ".jpeg": "image",
  ".webp": "image",
  ".gif": "image",
};

// A stored image is always written under the kind's own suffix, which is what `KIND_EXTENSIONS`
// maps `image` to in pi_chat/preview.py. A referenced one may be any suffix above.
const SUFFIX_BY_KIND: Record<DocKind, string> = {
  markdown: ".md",
  html: ".html",
  pdf: ".pdf",
  image: ".png",
};

type DocKind = "markdown" | "html" | "pdf" | "image";

/** A document that arrives in stages. Only the image tool sets these today. */
type DocStage = "rendering" | "ready" | "error";

const STAGES: readonly DocStage[] = ["rendering", "ready", "error"];

/** Text is the only thing this tool can write, so these are the kinds `content` may produce. */
const TEXT_KINDS: readonly DocKind[] = ["markdown", "html"];

interface DocState {
  id: string;
  kind: DocKind;
  title: string;
  revision: number;
  /** Set when the pane should read an existing file. */
  path?: string;
  /** Set when this extension wrote the bytes into the preview directory. */
  file?: string;
  note?: string;
  /** The stored bytes are a copy of `path`, taken because the server would not serve the original. */
  copied?: boolean;
  /** Opted in to the pane's disk staleness poll. Absent means "follow the pane's Follow-files setting". */
  watch?: boolean;
  /**
   * Where this document is in being made. Absent means "there is nothing to wait for", which is the
   * right reading for every kind the tool writes in one shot, and it is why the field is optional
   * rather than defaulted to `ready`.
   */
  state?: DocStage;
  /** Requested pixel size, "2048x2048", so the pane can draw a correctly shaped box before bytes exist. */
  canvas?: string;
}

/**
 * The flow the parameters exist to support, stated once here because every action is a step in it:
 *
 *   write or edit the file with the normal file tools -> preview { action:"open", path } -> the pane
 *   re-renders itself on every save, no further tool calls.
 *
 * Sending `content` is how you *invent* a document that has no file yet. Once it has one, stop.
 */
const PreviewParams = Type.Object({
  action: Type.Union([
    Type.Literal("open"),
    Type.Literal("update"),
    Type.Literal("close"),
    Type.Literal("list"),
    Type.Literal("state"),
    Type.Literal("watch"),
    Type.Literal("sync"),
    Type.Literal("clear"),
  ], {
    description:
      "open: show a document, mints the docId every other action needs. update: revise an open "
      + "document in its existing tab; pass no content to re-read the file from disk. close: one tab "
      + "(needs docId). list: what this extension has open. state: list plus what the user can actually "
      + "see right now (active tab, pane open). watch: turn disk following on or off for an open doc. "
      + "sync: re-copy the source file over a stored copy. clear: every tab.",
  }),
  kind: Type.Optional(
    Type.Union(
      [Type.Literal("markdown"), Type.Literal("html"), Type.Literal("pdf"), Type.Literal("image")],
      {
        description:
          "Document kind. Omit it: with `path` it comes from the file suffix, with `content` it defaults "
          + "to markdown. Only pass it to force a suffix the table above does not cover. Images are read "
          + "only: an image document comes from `path`, or from a tool that writes its bytes.",
      },
    ),
  ),
  title: Type.Optional(Type.String({ description: "Short title shown on the tab and in the pane header." })),
  content: Type.Optional(
    Type.String({
      description:
        "Complete document text, for a document that has no file on disk yet, or to store a copy over a "
        + "referenced one. It REPLACES the document, so send it whole. For anything already on disk, "
        + "write the file and let the pane follow it instead: content costs context on every revision.",
    }),
  ),
  path: Type.Optional(
    Type.String({
      description:
        "Absolute path of an existing file to show. Works for every kind, markdown and html included, and "
        + "this is the preferred backing whenever you intend to iterate: the pane watches it and a "
        + "`write`/`edit` to that path re-renders with no tool call. Nothing is copied and this tool "
        + "never writes to it. Outside the server's allowed roots the bytes are copied into the store "
        + "instead, which the receipt says out loud and which does not follow later edits.",
    }),
  ),
  docId: Type.Optional(
    Type.String({ description: "Document to act on. Returned by action=open as doc=..." }),
  ),
  note: Type.Optional(
    Type.String({ description: "One line shown at the foot of the pane, e.g. 'draft, unreviewed'." }),
  ),
  watch: Type.Optional(
    Type.Boolean({
      description:
        "With open or watch: re-check the file every second and re-render on its own, so you never send "
        + "content again. Set it true when you know the file will keep changing (a draft you are "
        + "iterating on, a render target). Omit it to let the pane's own Follow-files setting decide; "
        + "false pins this document off.",
    }),
  ),
  on: Type.Optional(
    Type.Boolean({ description: "With action=watch: true to follow the file, false to stop." }),
  ),
});

export default function (pi: ExtensionAPI) {
  const docs = new Map<string, DocState>();

  pi.on("session_start", async (_event, ctx) => {
    // Rebuild from the active branch rather than the whole file: an abandoned branch must not
    // resurrect documents the user navigated away from.
    docs.clear();
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type !== "custom" || entry.customType !== ENTRY_TYPE) continue;
      replay(entry.data as PreviewEntry | undefined);
    }
  });

  pi.registerTool({
    name: "preview",
    label: "Preview",
    description:
      "Show a document in the user's preview pane and keep it in sync as you work.\n"
      + "Two backings, and the choice between them is the whole decision. A STORED document is text you "
      + "sent in `content`: the pane shows a copy this extension wrote, and revising it means sending the "
      + "entire document again. A PATH document is a file that already exists: the pane reads it from "
      + "disk, and if it is being watched a `write` or `edit` to that file re-renders it with no tool "
      + "call at all.\n"
      + "So the flow to prefer: write the file with the normal file tools, then `open` it by `path`, then "
      + "keep editing the file. Use `content` only to invent a document that has no file yet, because "
      + "full-content updates put the whole document back through your context on every revision.\n"
      + "Actions in one line each: open (mints doc=...), update (revise in place; no content = re-read "
      + "from disk), watch (follow the file on or off), sync (re-copy the source over a stored copy), "
      + "state (what is open AND what the user is looking at), list, close, clear.\n"
      + "Never paste the same text into chat as well: the pane is where the user reads it.",
    promptSnippet:
      "preview(open|update|watch|sync|state|list|close|clear, path|content, title) - show and track a document in the preview pane",
    promptGuidelines: [
      "Open a preview for any document longer than about 15 lines, and for any file the user asks to see.",
      "To iterate, write the file and `open` it by `path`, with `watch:true` when you know it will change again. Do not re-send the document through `content` when an edit to the file does the job.",
      "`update` with no `content` re-reads a file-backed document in place; use it instead of `open` when a doc needs refreshing, since `open` mints a second tab.",
      "Call `state` before saying anything about what the user is looking at: `list` reports what you opened, `state` reports what they can see and which tab is in front.",
      "Revise a stored document with action=update and the complete new text; never send a partial diff.",
      "Preview documents are sandboxed: self-contained HTML only (inline CSS/SVG, data: URIs; no remote images, no fetch, no localStorage).",
      "Markdown relative image paths do not resolve; inline SVG or data: URIs instead.",
    ],
    parameters: PreviewParams,
    executionMode: "sequential",
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },

    async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
      const outcome = await run(params);
      return {
        content: [{ type: "text", text: outcome.text }],
        details: outcome.details ?? { action: params.action },
        ...(outcome.failed ? { isError: true as const } : {}),
      };
    },
  });

  pi.registerCommand("preview", {
    description: "Show a file in the preview pane: /preview <path> | store <path> | list | close [all]",
    handler: async (args, ctx: ExtensionContext) => {
      const argument = String(args || "").trim();
      const parts = argument.split(/\s+/).filter(Boolean);
      const verb = parts[0];

      if (!argument || verb === "list") {
        ctx.ui.notify(summary() || "No documents are open in the preview pane.", "info");
        return;
      }
      if (verb === "clear") {
        await run({ action: "clear" });
        ctx.ui.notify("Preview cleared.", "info");
        return;
      }
      if (verb === "close") {
        // `/preview close` closes everything; `/preview close <id>` closes one. Naming the id is
        // how the tool closes, because a model should say which document it meant.
        const target = parts[1];
        const outcome = target
          ? await run({ action: "close", docId: target })
          : await run({ action: "clear" });
        ctx.ui.notify(outcome.text, outcome.failed ? "error" : "info");
        return;
      }

      const stored = verb === "store";
      const raw = stored ? parts.slice(1).join(" ") : argument;
      if (!raw) {
        ctx.ui.notify("Usage: /preview <path> | store <path> | list | close [all]", "warning");
        return;
      }
      const outcome = await run({ action: "open", path: raw, ...(stored ? { store: true } : {}) });
      ctx.ui.notify(outcome.text, outcome.failed ? "error" : "info");
    },
  });

  /* ── The one implementation both entry points share ───────────── */

  async function run(params: {
    action: string;
    kind?: DocKind;
    title?: string;
    content?: string;
    path?: string;
    docId?: string;
    note?: string;
    watch?: boolean;
    on?: boolean;
    /** Command-only: copy the file into the store instead of referencing it. */
    store?: boolean;
  }): Promise<{ text: string; failed?: boolean; details?: PreviewEntry }> {
    // Awaiting inside the switch is only safe because nothing else can re-enter run(): the tool sets
    // `executionMode: "sequential"`, and the slash command handler's await lands in an RPC dispatch
    // that pi already serializes. If run() ever becomes concurrent, the `docs` map needs a lock.
    switch (params.action) {
      case "clear": {
        docs.clear();
        // Every entry carries the protocol version, including the two that name no document:
        // a reader must be able to tell a v1 clear from a future one without guessing.
        emit({ v: PROTOCOL_VERSION, op: "clear" });
        return { text: "Closed every document in the preview pane." };
      }

      case "list":
        return { text: summary() || "No documents are open in the preview pane." };

      case "close": {
        if (!params.docId) {
          const open = [...docs.keys()];
          if (!open.length) return { text: "Nothing is open to close.", failed: true };
          return {
            text: `Specify which one: ${open.map((id) => `${id} (${docs.get(id)?.title})`).join(", ")}`,
            failed: true,
          };
        }
        if (!docs.delete(params.docId)) {
          return { text: `No open document with doc=${params.docId}.`, failed: true };
        }
        emit({ op: "close", docId: params.docId });
        return { text: `Closed doc=${params.docId} in the preview pane.` };
      }

      case "update": {
        if (!params.docId) return { text: "action=update needs docId (from action=open).", failed: true };
        const doc = docs.get(params.docId);
        if (!doc) {
          return {
            text: `No open document with doc=${params.docId}. Open it first with action=open.`,
            failed: true,
          };
        }
        if (typeof params.content !== "string") {
          // No content means "what is on disk is newer than what you last sent me", which is the
          // common case now that the pane follows files on its own. This re-reads the existing
          // backing in place and bumps the revision, which is what busts the frame. It is also the
          // only way to refresh a PDF: a PDF has no content to send, and re-opening one used to
          // stack a second tab beside the first (docs/preview-pane-handoff.md gap 6).
          if (!doc.file && !doc.path) {
            return {
              text: `${doc.title} has no file behind it, so action=update needs the complete new \`content\`.`,
              failed: true,
            };
          }
          doc.revision += 1;
          if (params.title) doc.title = params.title;
          if (params.note !== undefined) doc.note = params.note;
          // A draw that finished is the one case where a document's own stage changes through this
          // action, and the entry has to say so or the pane keeps pulsing over a finished image.
          if (doc.state) doc.state = "ready";
          const reread = entryFor(doc, "update");
          emit(reread);
          return {
            text: `Re-read ${doc.title} (revision ${doc.revision}) from disk in the preview pane.\n${receipt(doc)}`,
            details: reread,
          };
        }
        // A referenced file is never rewritten: the new text becomes a stored copy and the pane
        // follows it. The user's original stays exactly as it was.
        if (!TEXT_KINDS.includes(doc.kind)) {
          return {
            text:
              `${doc.title} is a ${doc.kind}: it has no text to send. action=update with no \`content\` ` +
              "re-reads the file in place.",
            failed: true,
          };
        }
        const written = writeDoc(doc.id, doc.kind, params.content);
        if (!written) return { text: `Document is too large (limit is ${MAX_DOC_BYTES} bytes).`, failed: true };
        // `copied` means "stored bytes standing in for a file the server would not serve", so writing
        // text over an auto-copy keeps the flag and writing over a live reference clears it.
        if (doc.path) doc.copied = doc.copied === true;
        doc.file = written;
        doc.revision += 1;
        if (params.title) doc.title = params.title;
        if (params.note !== undefined) doc.note = params.note;
        const entry = entryFor(doc, "update");
        emit(entry);
        return {
          text: `Updated ${doc.title} (revision ${doc.revision}) in the preview pane.\n${receipt(doc)}`,
          details: entry,
        };
      }

      case "open": {
        const referenced = typeof params.path === "string" && params.path.length > 0;
        if (!referenced && typeof params.content !== "string") {
          return { text: "action=open needs `content` (markdown/html) or `path` (any kind).", failed: true };
        }

        if (referenced) {
          const path = params.path as string;
          const kind = params.kind ?? kindFromPath(path);
          if (!kind) {
            return {
              text: `Cannot preview ${basename(path)}: only .md, .html, .pdf and images (.png, .jpg, .webp, .gif) are supported.`,
              failed: true,
            };
          }
          let size = 0;
          try {
            const info = statSync(path);
            if (!info.isFile()) return { text: `No file at ${path}.`, failed: true };
            size = info.size;
          } catch {
            return { text: `No file at ${path}.`, failed: true };
          }
          // A PDF cannot be written from text at all, so `content` beside a PDF path is not a
          // redundant parameter but a misunderstanding, and the wrong one to paper over. An image is
          // the same trap: a model that has decided to paste base64 into `content` needs telling, not
          // a quiet text document that renders as a broken image.
          if (kind === "pdf" && typeof params.content === "string") {
            return {
              text: "PDFs are shown from `path` only; a PDF cannot be written from text.",
              failed: true,
            };
          }
          if (kind === "image" && typeof params.content === "string") {
            return {
              text: "Images are shown from `path` only; text cannot carry image bytes.",
              failed: true,
            };
          }
          if (size > MAX_DOC_BYTES) {
            return {
              text: `${basename(path)} is ${humanBytes(size)}, over the ${MAX_DOC_BYTES} byte preview limit.`,
              failed: true,
            };
          }

          // Reference where the browser can reach the file, copy where the server says it cannot (R1,
          // plans/pi-chat-preview-pdf-roots.md). `/preview store` forces the copy. Copying narrows
          // the browser-reachable surface to an id inside this scope; it places no limit on this
          // extension, which runs as pi and can already read anything.
          //
          // "The server could not be asked" is treated as fetchable, not as a refusal: a bare `pi -e`
          // run has no pane to render in either way, so there is nothing to rescue with a copy, and
          // silently duplicating a file the user is editing would be the worse mistake. A real
          // pi-chat child always has the token, so that branch is the only one that can decide.
          let file: string | undefined;
          let copied = false;
          let copyReason: string | null = null;
          const id = randomUUID().replace(/-/g, "");
          if (params.store === true || (await pathIsFetchable(path)) === false) {
            try {
              // The id is minted before the copy, not inside it: the stored file is named
              // `<docId>.<suffix>` and the route resolves a doc id to that path, so an id per side
              // would produce a tab whose document cannot be fetched (caught by
              // `test_store_mode_copies_the_bytes_into_the_connection_scope`).
              file = writeBytes(id, kind, readFileSync(path));
            } catch (error) {
              return { text: `Could not read ${path}: ${describe(error)}`, failed: true };
            }
            if (!file) return { text: "Document is too large to preview.", failed: true };
            copied = true;
            copyReason = params.store ? "you asked for a copy" : "it is outside the server's allowed roots";
          }

          const doc: DocState = {
            id,
            kind,
            title: params.title || basename(path),
            revision: 1,
            ...(file ? { file } : {}),
            // A copy is the thing being shown, so the pane must not poll the original it cannot read.
            ...(copied ? { copied: true } : { path }),
            ...(typeof params.watch === "boolean" ? { watch: params.watch } : {}),
            note: params.note,
          };
          docs.set(doc.id, doc);
          const entry = entryFor(doc, "open");
          emit(entry);
          return { text: openedMessage(doc), details: entry };
        }

        const kind = params.kind ?? "markdown";
        if (kind === "pdf") {
          return { text: "PDFs are shown from `path`; pass kind markdown or html for content.", failed: true };
        }
        if (kind === "image") {
          return {
            text:
              "Images are shown from `path`, or opened by the image tool while it draws. "
              + "Text cannot carry image bytes.",
            failed: true,
          };
        }
        const id = randomUUID().replace(/-/g, "");
        const file = writeBytes(id, kind, Buffer.from(params.content as string, "utf-8"));
        if (!file) return { text: `Document is too large (limit is ${MAX_DOC_BYTES} bytes).`, failed: true };
        const doc: DocState = {
          id,
          kind,
          title: params.title || defaultTitle(kind),
          revision: 1,
          file,
          ...(typeof params.watch === "boolean" ? { watch: params.watch } : {}),
          note: params.note,
        };
        docs.set(id, doc);
        const entry = entryFor(doc, "open");
        emit(entry);
        return { text: openedMessage(doc), details: entry };
      }

      case "state": {
        // `list` can only ever report what this process opened. Everything the user can actually see
        // (which tab is in front, whether the column is even open) lives in the browser, and the pane
        // POSTs it here. Without this action, "what do you see?" gets answered by a guess.
        const pane = await fetchPaneState();
        if ("blind" in pane) {
          const why =
            pane.blind === "no-endpoint"
              ? "no PI_CHAT_PORT or PI_CHAT_PREVIEW_TOKEN in this environment, which is a plain `pi -e` run rather than a pi-chat connection."
              : pane.blind === "no-report"
                ? "the pane has never reported to this connection's secret, so this load path did not carry one or this process predates your last reload."
                : "the pane did not answer: the browser is closed or this connection is gone.";
          return {
            text:
              `${summary() || "No documents are open in the preview pane."}\n`
              + `What the user can actually see is unavailable: ${why}`,
          };
        }
        const view = pane.view;
        const lines = [
          `pane: ${view.open ? "open" : "closed"}`
          + `${view.activeDocId ? `, active tab doc=${view.activeDocId}` : ", no active tab"}`
          + `${typeof view.width === "number" ? `, ${view.width}px wide` : ""}`,
          summary() || "No documents are open in the preview pane.",
        ];
        return { text: lines.join("\n"), details: { v: PROTOCOL_VERSION, op: "state", ...view } };
      }

      case "watch": {
        if (!params.docId || !docs.has(params.docId)) {
          return { text: "action=watch needs the docId of an open document.", failed: true };
        }
        if (typeof params.on !== "boolean") {
          return { text: "action=watch needs `on`: true follows the file, false stops.", failed: true };
        }
        const doc = docs.get(params.docId) as DocState;
        doc.watch = params.on;
        const entry = entryFor(doc, "update");
        emit(entry);
        return {
          text:
            `${doc.title}: ${doc.watch ? "watching" : "not watching"}`
            + `${doc.path ? " the file on disk" : " (no file behind this document, nothing to watch)"}.`,
          details: entry,
        };
      }

      case "sync": {
        if (!params.docId || !docs.has(params.docId)) {
          return { text: "action=sync needs the docId of an open document.", failed: true };
        }
        const doc = docs.get(params.docId) as DocState;
        if (!doc.path) {
          return { text: `${doc.title} has no source file to sync from.`, failed: true };
        }
        if (!existsSync(doc.path) || !statSync(doc.path).isFile()) {
          return { text: `No file at ${doc.path}.`, failed: true };
        }
        let bytes: Buffer;
        try {
          bytes = readFileSync(doc.path);
        } catch (error) {
          return { text: `Could not read ${doc.path}: ${describe(error)}`, failed: true };
        }
        const file = writeBytes(doc.id, doc.kind, bytes);
        if (!file) return { text: `Document is too large (limit is ${MAX_DOC_BYTES} bytes).`, failed: true };
        doc.file = file;
        doc.copied = true;
        doc.revision += 1;
        if (params.note !== undefined) doc.note = params.note;
        const entry = entryFor(doc, "update");
        emit(entry);
        return {
          text: `Synced ${doc.title} (revision ${doc.revision}) from ${doc.path} into the preview store.`,
          details: entry,
        };
      }

      default:
        return { text: `Unknown action ${params.action}.`, failed: true };
    }
  }

  /* ── Store and payload ──────────────────────────────────────── */

  function scopeDir(): string {
    // The server hands each connection its own directory, which is also what authorizes the
    // browser to read the file back. Without it (a plain `pi -e` run) fall back to a temp dir:
    // the entry is still written, the pane just cannot fetch it.
    const fromEnv = process.env.PI_CHAT_PREVIEW_DIR;
    const dir = fromEnv && fromEnv.trim() ? fromEnv.trim() : join(tmpdir(), "pi-chat-previews", String(process.pid));
    mkdirSync(dir, { recursive: true });
    return dir;
  }

  function writeDoc(id: string, kind: DocKind, content: string): string | null {
    return writeBytes(id, kind, Buffer.from(content, "utf-8"));
  }

  function writeBytes(id: string, kind: DocKind, bytes: Buffer): string | null {
    if (bytes.byteLength > MAX_DOC_BYTES) return null;
    const name = `${id}${SUFFIX_BY_KIND[kind]}`;
    const target = join(scopeDir(), name);
    const temporary = `${target}.tmp`;
    writeFileSync(temporary, bytes);
    renameSync(temporary, target);
    return name;
  }

  function entryFor(doc: DocState, op: "open" | "update"): PreviewEntry {
    return {
      v: PROTOCOL_VERSION,
      op,
      docId: doc.id,
      kind: doc.kind,
      title: doc.title,
      revision: doc.revision,
      ...(doc.path ? { path: doc.path } : {}),
      ...(doc.file ? { file: doc.file } : {}),
      ...(doc.note ? { note: doc.note } : {}),
      // Only the affirmative is written. A stored doc carries `watch: false`, but a document the
      // model never mentioned carries nothing, which is how the pane's own Follow-files default gets
      // its vote rather than being overridden by an absent flag.
      ...(doc.copied ? { copied: true } : {}),
      ...(doc.watch === true ? { watch: true } : doc.watch === false ? { watch: false } : {}),
      // An absent stage means "nothing is pending", so only a document made in pieces writes one.
      // `canvas` rides with it because a placeholder needs both the shape and the reason.
      ...(doc.state ? { state: doc.state } : {}),
      ...(doc.canvas ? { canvas: doc.canvas } : {}),
    };
  }

  function emit(entry: PreviewEntry) {
    pi.appendEntry(ENTRY_TYPE, entry);
  }

  function replay(data: PreviewEntry | undefined) {
    if (!data || typeof data !== "object") return;
    const id = String(data.docId || "");
    if (!id) {
      if (data.op === "clear") docs.clear();
      return;
    }
    if (data.op === "close") {
      docs.delete(id);
      return;
    }
    if (data.op !== "open" && data.op !== "update") return;
    // A kind that is not in the table stays unknown. Coercing it to markdown - which is what this line
    // used to do for anything that was not html or pdf - filed a document the server happily serves as
    // a PNG as a text note, so `preview list` lied and `update docId=<image>` would have rewritten a
    // PNG as text. An unknown kind is also how a future kind arrives in a session written by a newer
    // build, and dropping that tab beats rendering it wrong.
    const kind = typeof data.kind === "string" && data.kind in KIND_BY_SUFFIX
      ? (data.kind as DocKind)
      : undefined;
    if (!kind) return;
    const stage = STAGES.includes(data.state as DocStage) ? (data.state as DocStage) : undefined;
    docs.set(id, {
      id,
      kind,
      title: String(data.title || defaultTitle(kind)),
      revision: Number(data.revision) || 1,
      ...(data.path ? { path: String(data.path) } : {}),
      ...(data.file ? { file: String(data.file) } : {}),
      ...(data.note ? { note: String(data.note) } : {}),
      ...(data.copied ? { copied: true } : {}),
      ...(typeof data.watch === "boolean" ? { watch: data.watch } : {}),
      ...(stage ? { state: stage } : {}),
      ...(typeof data.canvas === "string" ? { canvas: String(data.canvas) } : {}),
    });
  }

  function summary(): string {
    return [...docs.values()]
      .map((doc) =>
        `${doc.id}  ${doc.kind}  r${doc.revision}  ${doc.title}`
        + `${doc.copied ? "  [copy]" : doc.path ? "  [file]" : "  [stored]"}`
        + `${doc.watch === true ? "  [watching]" : ""}`
        + `${doc.state && doc.state !== "ready" ? `  [${doc.state}${doc.canvas ? ` ${doc.canvas}` : ""}]` : ""}`)
      .join("\n");
  }

  /* ── One hop to the browser ────────────────────────────────── */

  interface PaneView {
    open?: boolean;
    activeDocId?: string | null;
    width?: number | null;
    watching?: string | null;
    revision?: number | null;
    fileToken?: string | null;
  }

  function paneEndpoint(query?: Record<string, string>): string | null {
    // `process.py` hands the child the port it spawned against plus a per-connection secret, which
    // is the only credential this process can present: a child has no browser cookie, and the
    // endpoint must not be readable by whatever else can reach the loopback port.
    const port = (process.env.PI_CHAT_PORT || "").trim();
    const token = (process.env.PI_CHAT_PREVIEW_TOKEN || "").trim();
    if (!/^\d{1,5}$/.test(port) || !token) return null;
    const url = new URL(`http://127.0.0.1:${port}/api/preview/state`);
    for (const [key, value] of Object.entries(query || {})) url.searchParams.set(key, value);
    return `${url.toString()}|${token}`;
  }

  /**
   * Why the pane cannot be asked, kept separate from the answer. Collapsing these into one null made
   * `state` tell a pi-chat child it was "a plain `pi -e` run", when the truth was that the pane simply
   * had never reported to this connection's secret (2026-11-19).
   */
  type PaneBlind = "no-endpoint" | "no-report" | "unreachable";

  async function fetchPaneState(): Promise<{ view: PaneView } | { blind: PaneBlind }> {
    const endpoint = paneEndpoint();
    if (!endpoint) return { blind: "no-endpoint" };
    const [url, token] = endpoint.split("|");
    try {
      const response = await fetch(url, {
        headers: { "x-preview-token": token },
        signal: AbortSignal.timeout(1500),
      });
      // 404 is `No pane state recorded`: the route is alive and this secret has no report behind it.
      if (response.status === 404) return { blind: "no-report" };
      if (!response.ok) return { blind: "unreachable" };
      const body = await response.json();
      return body && typeof body === "object" ? { view: body as PaneView } : { blind: "unreachable" };
    } catch {
      // A pane that is not reachable is not a failure to report: `list` and `state` differ in one
      // line, and a dead browser must not fail the document work the caller came to do.
      return { blind: "unreachable" };
    }
  }

  /**
   * Can the browser fetch this path itself? The server is the only thing that knows its allowed
   * roots and its `PI_CHAT_DEV` relaxation, so it is the only honest witness, and the freshness
   * route is exactly that question. null means "could not ask", and the caller treats that as
   * unfetchable, because a copy that renders beats a reference that paints an error body.
   */
  async function pathIsFetchable(path: string): Promise<boolean | null> {
    if ((process.env.PI_CHAT_DEV || "").trim()) return true;
    const endpoint = paneEndpoint({ path });
    if (!endpoint) return null;
    const [url, token] = endpoint.split("|");
    try {
      const response = await fetch(url, {
        headers: { "x-preview-token": token },
        signal: AbortSignal.timeout(1500),
      });
      if (!response.ok) return false;
      const body = (await response.json()) as { served?: boolean };
      return body?.served === true;
    } catch {
      return null;
    }
  }

  function openedMessage(doc: DocState, copyReason: string | null = null): string {
    return `Opened ${doc.title} (${doc.kind}) in the preview pane as doc=${doc.id}.\n${receipt(doc, copyReason)}`;
  }

  /**
   * What to do next, which used to be one string for all three kinds and therefore wrong for two of
   * them (docs/preview-pane-handoff.md gaps 5 and 6): it told callers to send `content` for a PDF,
   * which is impossible, and to re-send a whole document that was sitting in a file all along.
   */
  function receipt(doc: DocState, copyReason: string | null = null): string {
    const id = doc.id;
    if (doc.path) {
      const stamp = fileStamp(doc.path);
      const watching = doc.watch === true
        ? "Watching it: an edit re-renders the pane on its own."
        : doc.watch === false
          ? "Not watching it: `watch` with on=true to follow edits."
          : "If the pane's Follow-files is on it re-renders on each save; `watch` with on=true to force it.";
      return (
        `Backed by ${doc.path}${stamp ? ` (${stamp})` : ""}. The pane reads that file. ${watching}\n`
        + `Edit it with the normal file tools instead of sending \`content\` again. `
        + `action=update with no \`content\` re-reads it in place; action=sync stores a copy of it.`
      );
    }
    if (doc.kind === "image") {
      // Said before the copy branch, because the copy advice is the one thing that cannot apply here:
      // an image has no `content` to replace its bytes with.
      return (
        `Backed by ${doc.file || doc.path}${doc.copied ? " (a copy: action=sync re-copies the source)" : ""}. `
        + `The pane reads that file. Images are read only: re-write the file, then action=update with `
        + `no \`content\` re-reads it.`
      );
    }
    if (doc.copied) {
      return (
        `This is a copy of ${doc.path || "the source file"}${copyReason ? ` because ${copyReason}` : ""}, `
        + `so it will not follow later edits. action=sync re-copies the source; `
        + `action=update with \`content\` replaces these bytes.`
      );
    }
    return (
      `Stored document, no file behind it. action=update with the COMPLETE new text replaces it `
      + `(partial text is not merged); action=close takes the tab away.`
    );
  }
}

interface PreviewEntry {
  v: number;
  op: "open" | "update" | "close" | "clear" | "state";
  docId?: string;
  kind?: DocKind;
  title?: string;
  revision?: number;
  path?: string;
  file?: string;
  note?: string;
  copied?: boolean;
  watch?: boolean;
  /** Stage of a document made in pieces, and the pixel size it asked for. See `DocState`. */
  state?: string;
  canvas?: string;
  /** From `state`, which is not about one document: what the user has in front of them. */
  open?: boolean;
  activeDocId?: string | null;
  width?: number | null;
  watching?: string | null;
  fileToken?: string | null;
}

function kindFromPath(path: string): DocKind | null {
  return KIND_BY_SUFFIX[extname(path).toLowerCase()] ?? null;
}

/** "476 KB" beside a path, so a receipt about a big file says something about its size. */
function humanBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

/** What the file behind a document looks like right now, for a receipt. Never throws. */
function fileStamp(path: string): string | null {
  try {
    const info = statSync(path);
    return `${humanBytes(info.size)}, last written ${info.mtime.toISOString()}`;
  } catch {
    return null;
  }
}

function defaultTitle(kind: DocKind): string {
  return kind === "markdown" ? "Untitled note" : `Untitled ${kind}`;
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
