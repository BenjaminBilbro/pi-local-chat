/**
 * capture_probe.ts: emits the RPC markers a model call cannot be relied on to produce.
 *
 * Loaded only by tools/capture.py (`pi -e tests/fixtures/capture_probe.ts`). Each slash command is
 * handled in-process, so it produces `entry_appended`, `extension_ui_request` and `extension_error`
 * with zero inference and zero variance, which is what makes a capture fixture reproducible enough to
 * write a test against.
 *
 * It is a fixture, not a feature: nothing in the app loads this file.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const ENTRY_TYPE = "capture_probe";

/** ui is present in an interactive/RPC session and absent in headless ones, and an extension that
 *  throws on startup would take the capture with it. */
function setStatus(ctx: ExtensionContext | undefined, text: string) {
  const ui = (ctx as { ui?: { setStatus?: (key: string, value: string) => void } } | undefined)?.ui;
  try {
    ui?.setStatus?.("capture-probe", text);
  } catch {
    /* nothing to emit to */
  }
}

function notify(ctx: ExtensionContext | undefined, text: string) {
  const ui = (ctx as
    { ui?: { notify?: (message: string, level?: string) => void } } | undefined)?.ui;
  try {
    ui?.notify?.(text, "info");
  } catch {
    /* nothing to emit to */
  }
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    setStatus(ctx, "probe loaded");
  });

  pi.registerCommand("capture-probe", {
    description: "Emit entry_appended + extension_ui_request (RPC capture fixture)",
    handler: async (args, ctx) => {
      const text = String(args ?? "ping");
      pi.appendEntry(ENTRY_TYPE, { text, at: new Date().toISOString() });
      setStatus(ctx, text);
      notify(ctx, `capture probe: ${text}`);
    },
  });

  pi.registerCommand("capture-probe-fail", {
    description: "Throw from a handler so RPC emits extension_error (RPC capture fixture)",
    handler: async () => {
      throw new Error("capture_probe: deliberate failure to emit extension_error");
    },
  });
}
