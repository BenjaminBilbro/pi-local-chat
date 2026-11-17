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
import { basename, extname, join } from "node:path";
import { randomUUID } from "node:crypto";

import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const ENTRY_TYPE = "pichat_preview";
const PROTOCOL_VERSION = 1;
/** Keep in step with PI_CHAT_PREVIEW_MAX_DOC_MB on the server. */
const MAX_DOC_BYTES = 25 * 1024 * 1024;

const KIND_BY_SUFFIX: Record<string, DocKind> = {
  ".md": "markdown",
  ".markdown": "markdown",
  ".html": "html",
  ".htm": "html",
  ".pdf": "pdf",
};

const SUFFIX_BY_KIND: Record<DocKind, string> = {
  markdown: ".md",
  html: ".html",
  pdf: ".pdf",
};

type DocKind = "markdown" | "html" | "pdf";

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
}

const PreviewParams = Type.Object({
  action: Type.Union([
    Type.Literal("open"),
    Type.Literal("update"),
    Type.Literal("close"),
    Type.Literal("list"),
    Type.Literal("clear"),
  ]),
  kind: Type.Optional(
    Type.Union([Type.Literal("markdown"), Type.Literal("html"), Type.Literal("pdf")], {
      description: "Document kind. Required with action=open unless path names a supported suffix.",
    }),
  ),
  title: Type.Optional(Type.String({ description: "Short title shown on the tab and in the pane header." })),
  content: Type.Optional(
    Type.String({
      description:
        "Complete document text for markdown/html. Replaces the whole document, so send it in full.",
    }),
  ),
  path: Type.Optional(
    Type.String({
      description:
        "Absolute path of an existing file to show. Required for pdf. Nothing is copied and the file is never modified.",
    }),
  ),
  docId: Type.Optional(
    Type.String({ description: "Document to update or close. Returned by action=open as doc=..." }),
  ),
  note: Type.Optional(
    Type.String({ description: "One line shown at the foot of the pane, e.g. 'draft, unreviewed'." }),
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
      "Show a document in the user's preview pane and keep it in sync as you work. "
      + "Use action=open once, then action=update with the FULL replacement content after each "
      + "substantial edit. Do not paste the same text into chat as well: the pane is where the "
      + "user reads it. markdown and html take `content`; pdf takes `path` to an existing file.",
    promptSnippet: "preview(kind|content|path, title) - open or update a document in the preview pane",
    promptGuidelines: [
      "Open a preview for any document longer than about 15 lines, and for any file the user asks to see.",
      "Revise with action=update and the complete new text; never send a partial diff.",
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
      const outcome = run(params);
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
        run({ action: "clear" });
        ctx.ui.notify("Preview cleared.", "info");
        return;
      }
      if (verb === "close") {
        // `/preview close` closes everything; `/preview close <id>` closes one. Naming the id is
        // how the tool closes, because a model should say which document it meant.
        const target = parts[1];
        const outcome = target
          ? run({ action: "close", docId: target })
          : run({ action: "clear" });
        ctx.ui.notify(outcome.text, outcome.failed ? "error" : "info");
        return;
      }

      const stored = verb === "store";
      const raw = stored ? parts.slice(1).join(" ") : argument;
      if (!raw) {
        ctx.ui.notify("Usage: /preview <path> | store <path> | list | close [all]", "warning");
        return;
      }
      const outcome = run({ action: "open", path: raw, ...(stored ? { store: true } : {}) });
      ctx.ui.notify(outcome.text, outcome.failed ? "error" : "info");
    },
  });

  /* ── The one implementation both entry points share ───────────── */

  function run(params: {
    action: string;
    kind?: DocKind;
    title?: string;
    content?: string;
    path?: string;
    docId?: string;
    note?: string;
    /** Command-only: copy the file into the store instead of referencing it. */
    store?: boolean;
  }): { text: string; failed?: boolean; details?: PreviewEntry } {
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
          return { text: "action=update needs the complete new `content`.", failed: true };
        }
        // A referenced file is never rewritten: the new text becomes a stored copy and the pane
        // follows it. The user's original stays exactly as it was.
        const written = writeDoc(doc.id, doc.kind, params.content);
        if (!written) return { text: `Document is too large (limit is ${MAX_DOC_BYTES} bytes).`, failed: true };
        doc.file = written;
        doc.revision += 1;
        if (params.title) doc.title = params.title;
        if (params.note !== undefined) doc.note = params.note;
        const entry = entryFor(doc, "update");
        emit(entry);
        return {
          text: `Updated ${doc.title} (revision ${doc.revision}) in the preview pane.`,
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
              text: `Cannot preview ${basename(path)}: only .md, .html and .pdf are supported.`,
              failed: true,
            };
          }
          if (kind === "pdf" && typeof params.content === "string") {
            return {
              text: "PDFs are shown from `path` only; a PDF cannot be written from text.",
              failed: true,
            };
          }
          if (!existsSync(path) || !statSync(path).isFile()) {
            return { text: `No file at ${path}.`, failed: true };
          }

          if (params.store) {
            // Explicit copy: for a file outside the server's allowed roots, or one that the user
            // is editing while the pane shows it.
            let bytes: Buffer;
            try {
              bytes = readFileSync(path);
            } catch (error) {
              return { text: `Could not read ${path}: ${describe(error)}`, failed: true };
            }
            const id = randomUUID().replace(/-/g, "");
            const file = writeBytes(id, kind, bytes);
            if (!file) return { text: "Document is too large to preview.", failed: true };
            const doc: DocState = {
              id,
              kind,
              title: params.title || basename(path),
              revision: 1,
              file,
              note: params.note,
            };
            docs.set(id, doc);
            const entry = entryFor(doc, "open");
            emit(entry);
            return { text: openedMessage(doc), details: entry };
          }

          const doc: DocState = {
            id: randomUUID().replace(/-/g, ""),
            kind,
            title: params.title || basename(path),
            revision: 1,
            path,
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
        const id = randomUUID().replace(/-/g, "");
        const file = writeBytes(id, kind, Buffer.from(params.content as string, "utf-8"));
        if (!file) return { text: `Document is too large (limit is ${MAX_DOC_BYTES} bytes).`, failed: true };
        const doc: DocState = {
          id,
          kind,
          title: params.title || defaultTitle(kind),
          revision: 1,
          file,
          note: params.note,
        };
        docs.set(id, doc);
        const entry = entryFor(doc, "open");
        emit(entry);
        return { text: openedMessage(doc), details: entry };
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

  function entryFor(doc: DocState, op: "open" | "update" | "close"): PreviewEntry {
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
    const kind = data.kind === "html" || data.kind === "pdf" ? data.kind : "markdown";
    docs.set(id, {
      id,
      kind,
      title: String(data.title || defaultTitle(kind)),
      revision: Number(data.revision) || 1,
      ...(data.path ? { path: String(data.path) } : {}),
      ...(data.file ? { file: String(data.file) } : {}),
      ...(data.note ? { note: String(data.note) } : {}),
    });
  }

  function summary(): string {
    return [...docs.values()]
      .map((doc) => `${doc.id}  ${doc.kind}  r${doc.revision}  ${doc.title}`)
      .join("\n");
  }

  function openedMessage(doc: DocState): string {
    return `Opened ${doc.title} (${doc.kind}) in the preview pane as doc=${doc.id}. `
      + `Update it with preview action=update, docId="${doc.id}", content=<the whole new document>.`;
  }
}

interface PreviewEntry {
  v: number;
  op: "open" | "update" | "close" | "clear";
  docId?: string;
  kind?: DocKind;
  title?: string;
  revision?: number;
  path?: string;
  file?: string;
  note?: string;
}

function kindFromPath(path: string): DocKind | null {
  return KIND_BY_SUFFIX[extname(path).toLowerCase()] ?? null;
}

function defaultTitle(kind: DocKind): string {
  return kind === "markdown" ? "Untitled note" : `Untitled ${kind}`;
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
