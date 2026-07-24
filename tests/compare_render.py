"""compare_render.py

Compare HTML output of assistant messages rendered from two JSONL sources:
  1. Native pi session format  (agent_end.messages)
  2. RPC capture format        (wrapped {seq, ts, event})

Both are parsed through the existing sessions.py::parse_jsonl_messages,
then rendered through the existing static/history.js via Node.js.

Usage:
  uv run python tests/compare_render.py data-samples/nested_subagent_session.jsonl \
                                      data-samples/nested_subagent_rpc_capture.jsonl
"""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Re-use the existing session parser
sys.path.insert(0, str(PROJECT_ROOT))
from pi_chat.sessions import parse_jsonl_messages  # noqa: E402


def render_messages(messages: list[dict]) -> list[str]:
    """Render assistant messages to HTML via the existing JS functions."""
    # Filter to assistant messages only
    assistant = [m for m in messages if m.get("role") == "assistant"]
    if not assistant:
        return []

    result = subprocess.run(
        ["node", str(PROJECT_ROOT / "tests" / "render_message.js")],
        input=json.dumps(assistant, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Node render failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Split output into per-message blocks
    blocks = []
    current = []
    in_block = False
    for line in result.stdout.splitlines():
        if line == "---MESSAGE---":
            in_block = True
            current = []
        elif line == "---END---":
            in_block = False
            blocks.append("\n".join(current))
        elif in_block:
            current.append(line)
    return blocks


def main():
    if len(sys.argv) < 3:
        print("Usage: compare_render.py <native_session.jsonl> <rpc_capture.jsonl>")
        sys.exit(1)

    native_path = sys.argv[1]
    rpc_path = sys.argv[2]

    print(f"Native session: {native_path}")
    print(f"RPC capture:    {rpc_path}")
    print()

    # Parse both through the same function
    native_msgs = parse_jsonl_messages(native_path)
    rpc_msgs = parse_jsonl_messages(rpc_path)

    if not native_msgs:
        print(f"ERROR: No messages parsed from {native_path}", file=sys.stderr)
        sys.exit(1)
    if not rpc_msgs:
        print(f"ERROR: No messages parsed from {rpc_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Native messages: {len(native_msgs)} ({sum(1 for m in native_msgs if m['role']=='assistant')} assistant)")
    print(f"RPC messages:    {len(rpc_msgs)} ({sum(1 for m in rpc_msgs if m['role']=='assistant')} assistant)")
    print()

    # Render both
    native_html = render_messages(native_msgs)
    rpc_html = render_messages(rpc_msgs)

    print(f"Native rendered blocks: {len(native_html)}")
    print(f"RPC rendered blocks:    {len(rpc_html)}")
    print()

    # Compare
    if len(native_html) != len(rpc_html):
        print(f"MISMATCH: {len(native_html)} native blocks vs {len(rpc_html)} RPC blocks")
        for i, (n, r) in enumerate(zip(native_html, rpc_html)):
            match = "✓" if n == r else "✗"
            print(f"  Block {i}: {match}")
        # Show extra blocks
        for i in range(max(len(native_html), len(rpc_html))):
            if i >= len(native_html):
                print(f"  Block {i}: (native missing)")
                print(f"    RPC: {rpc_html[i][:200]}")
            elif i >= len(rpc_html):
                print(f"  Block {i}: (RPC missing)")
                print(f"    Native: {native_html[i][:200]}")
    else:
        all_match = True
        for i, (n, r) in enumerate(zip(native_html, rpc_html)):
            if n == r:
                print(f"  Block {i}: ✓ match")
            else:
                all_match = False
                print(f"  Block {i}: ✗ MISMATCH")
                # Show diff
                n_lines = n.splitlines()
                r_lines = r.splitlines()
                for j, (nl, rl) in enumerate(zip(n_lines, r_lines)):
                    if nl != rl:
                        print(f"    Line {j}:")
                        print(f"      Native: {nl[:150]}")
                        print(f"      RPC:    {rl[:150]}")
                if len(n_lines) != len(r_lines):
                    print(f"    Line count: {len(n_lines)} native vs {len(r_lines)} RPC")

        if all_match:
            print("\nAll blocks match! ✓")
        else:
            print("\nSome blocks differ — see details above.")

    # Also dump raw HTML for inspection
    for src_name, blocks in [("native", native_html), ("rpc", rpc_html)]:
        out_path = PROJECT_ROOT / f"tests/output_{src_name}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            for i, block in enumerate(blocks):
                f.write(f"<!-- Message {i} -->\n{block}\n\n")
        print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
