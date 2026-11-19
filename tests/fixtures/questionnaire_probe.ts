/**
 * A probe for capturing pi's extension dialog wire, without spending a token.
 *
 * `/questionnaire-probe select` calls `ctx.ui.select` and nothing else. pi dispatches an extension
 * slash command over RPC with `disposition: "handled"` and no model call, so the dialog request that
 * comes out on stdout is what a real extension's is, byte for byte, at zero inference cost. That is the
 * only reason this file exists: `tools/capture.py` cannot drive a tool directly (`executeTool` is handed
 * to tools, not to commands), and the shape of `extension_ui_request` is the contract
 * `pi_chat/ui_relay.py` reads when it turns a child's question into something this application can show.
 *
 * The answer it gets back is not interesting to a capture; the request is. So the handlers report the
 * result as text and finish.
 */

import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

const WHICH = new Set(["select", "confirm", "input", "editor"]);

export default function questionnaireProbe(pi: ExtensionAPI) {
	pi.registerCommand("questionnaire-probe", {
		description: "Call one ctx.ui dialog method, for the RPC capture",
		async handler(args: string, ctx: ExtensionCommandContext) {
			const which = (args || "").trim().toLowerCase() || "select";
			if (!WHICH.has(which)) {
				ctx.ui.notify(`questionnaire-probe: unknown method "${which}"`, "error");
				return;
			}

			let outcome: string;
			switch (which) {
				case "select":
					outcome = String(
						await ctx.ui.select("Allow this?", ["Allow", "Block", "Something else"], { timeout: 60_000 }),
					);
					break;
				case "confirm":
					outcome = String(await ctx.ui.confirm("Clear the draft?", "Everything typed so far is lost."));
					break;
				case "input":
					outcome = String(await ctx.ui.input("Name the branch", "feature/x"));
					break;
				default:
					outcome = String(await ctx.ui.editor("Fix the title", "a short title"));
					break;
			}
			ctx.ui.notify(`questionnaire-probe ${which}: ${outcome}`, "info");
		},
	});
}
