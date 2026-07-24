#!/usr/bin/env uv run python
"""
Simulate the expected UI for a session file.
Generates a self-contained HTML file with the rendered output.

Usage:
  uv run python test_historic_render.py [session_path] [--open]
"""

import json
import sys
import textwrap
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from pi_chat.sessions import parse_jsonl_messages

DEFAULT_SESSION = (
    "/home/bbilbro/.pi/agent/sessions/"
    "--home-bbilbro-pi-chat-sessions-b--/"
    "2026-07-24T06-37-19-924Z_019f92d7-bcb4-73cd-a03f-51ee9e24e5c5.jsonl"
)

OUTPUT = Path("/tmp/pi_chat_simulated_ui.html")


def escape_html(s):
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def simple_markdown(text):
    """Minimal markdown rendering for preview."""
    html = escape_html(text)
    # Bold
    html = html.replace("**", "<strong>", 1)
    # Code
    html = html.replace("`", "<code>", 1)
    # Newlines
    html = html.replace("\n", "<br>\n")
    return html


def tool_call_args_desc(args):
    """Extract a short description from tool call arguments."""
    if not args or not isinstance(args, dict):
        return ""
    for key in ("command", "prompt", "path", "query", "url"):
        if key in args:
            return str(args[key])[:100]
    if "name" in args:
        name = args["name"]
        task = args.get("task", "")
        if task:
            return f"{name}: {task[:70]}"
        return name
    return json.dumps(args, ensure_ascii=False)[:100]


def build_timeline_html(timeline_messages, summary, status, is_error, turns, max_turns):
    """Build the HTML for a sub-agent timeline body."""
    parts = []
    parts.append('<div class="subagent-timeline">')

    if not timeline_messages:
        parts.append('<span class="empty-timeline">No timeline data available</span>')
        parts.append('</div>')
        return "".join(parts)

    for msg in timeline_messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        for item in content:
            if item.get("type") == "text" and item.get("text"):
                text = item["text"].rstrip()
                parts.append(
                    f'<div class="tl-assistant"><div class="md">{simple_markdown(text)}</div></div>'
                )
            elif item.get("type") == "toolCall":
                tname = item.get("name", "?")
                args_desc = tool_call_args_desc(item.get("arguments", {}))
                is_err = item.get("isError", False)
                icon = "✗" if is_err else "✓"
                cls = "tl-tool-call is-error" if is_err else "tl-tool-call"
                parts.append(
                    f'<div class="{cls}">'
                    f'<span class="tl-icon">{icon}</span>'
                    f'<span class="tl-tname">{escape_html(tname)}</span>'
                    f"{f'<span class=\"tl-targs\">{escape_html(args_desc)}</span>' if args_desc else ''}"
                    f"</div>"
                )

    # Turn count
    if max_turns:
        parts.append(f'<div class="tl-turns">{turns or 0}/{max_turns} turns</div>')
    elif turns:
        parts.append(f'<div class="tl-turns">{turns} turns</div>')

    # Summary
    if summary or status:
        status_cls = "summary-status is-error" if is_error else "summary-status"
        parts.append('<div class="tl-summary">')
        if status:
            parts.append(f'<span class="{status_cls}">{escape_html(status)}</span>')
        if summary:
            parts.append(f'<div class="md">{simple_markdown(summary)}</div>')
        parts.append('</div>')

    parts.append('</div>')
    return "".join(parts)


def render_session(messages):
    """Render all messages to HTML."""
    html_parts = []

    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", [])

        if role == "user":
            text = ""
            for c in content:
                if c.get("type") == "text":
                    text = c.get("text", "")
            html_parts.append(
                f'<div class="msg user">'
                f'<div class="msg-label">you</div>'
                f'<div class="bubble">{escape_html(text)}</div>'
                f"</div>"
            )

        elif role == "assistant":
            html_parts.append('<div class="msg assistant">')
            html_parts.append('<div class="msg-label">assistant</div>')
            html_parts.append('<div class="timeline">')

            for item in content:
                if item.get("type") == "thinking":
                    thinking = item.get("thinking", "").strip()
                    html_parts.append(
                        f'<div class="tl-thinking">{escape_html(thinking[:200])}</div>'
                    )

                elif item.get("type") == "text" and item.get("text"):
                    html_parts.append(
                        f'<div class="tl-text"><div class="md">{simple_markdown(item["text"])}</div></div>'
                    )

                elif item.get("type") == "toolCall" and item.get("name") == "subagent":
                    # Render sub-agent card
                    args = item.get("arguments", {})
                    name = args.get("name", "sub-agent")
                    task = args.get("task", "")
                    tl_msgs = item.get("_timelineMessages", [])
                    turns = item.get("_turns")
                    max_turns = item.get("_maxTurns")
                    status = item.get("_status", "")
                    summary = item.get("_summary", "") or ""
                    is_error = item.get("_isError", False)

                    body_html = build_timeline_html(
                        tl_msgs, summary, status, is_error, turns, max_turns
                    )

                    html_parts.append(
                        f'<div class="subagent-card">'
                        f'<div class="sa-header">'
                        f'<span class="sa-name">{escape_html(name)}</span>'
                        f'<span class="sa-task">{escape_html(task[:80])}</span>'
                        f"<span class='sa-chevron'>▼</span>"
                        f"</div>"
                        f"{body_html}"
                        f"</div>"
                    )

                elif item.get("type") == "toolCall":
                    tname = item.get("name", "?")
                    html_parts.append(
                        f'<div class="tl-tool"><span class="tool-name">{escape_html(tname)}</span></div>'
                    )

            html_parts.append('</div><!-- timeline -->')
            html_parts.append('</div><!-- msg assistant -->')

    return "\n".join(html_parts)


def generate_html(session_path):
    """Generate the full HTML preview."""
    messages = parse_jsonl_messages(session_path)
    if not messages:
        print(f"ERROR: Could not parse {session_path}")
        return None

    body = render_session(messages)

    html = textwrap.dedent(f'''\
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <title>PI Chat — Simulated UI Preview</title>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      body {{
        background: #9ca2d1;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #FFF2F2;
        padding: 20px;
        max-width: 800px;
        margin: 0 auto;
      }}
      h1 {{ font-size: 14px; opacity: 0.7; margin-bottom: 16px; font-weight: 400; }}
      .path {{ font-size: 11px; opacity: 0.5; margin-bottom: 20px; word-break: break-all; }}

      /* Messages */
      .msg {{ margin-bottom: 16px; }}
      .msg-label {{
        font-size: 11px;
        margin-bottom: 4px;
        opacity: 0.6;
      }}
      .user .bubble {{
        background: #FFF2F2;
        color: #9ca2d1;
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 14px;
        line-height: 1.5;
        max-width: 85%;
      }}
      .assistant .timeline {{
        background: #FFF2F2;
        color: #9ca2d1;
        padding: 12px;
        border-radius: 12px;
        max-width: 85%;
      }}

      /* Timeline items */
      .tl-thinking {{
        font-size: 12px;
        color: #5D638E;
        font-style: italic;
        padding: 6px 0;
        line-height: 1.4;
      }}
      .tl-text {{ padding: 6px 0; }}
      .tl-text .md {{
        font-size: 14px;
        line-height: 1.5;
        color: #9ca2d1;
      }}
      .tl-tool {{
        font-size: 12px;
        font-family: monospace;
        padding: 4px 0;
        color: #5D638E;
      }}
      .tool-name {{ font-weight: 600; color: #414A82; }}

      /* Sub-agent card */
      .subagent-card {{
        border: 1px solid #E7DADD;
        border-left: 3px solid #414A82;
        border-radius: 8px;
        background: #FFF2F2;
        margin: 8px 0;
        overflow: hidden;
      }}
      .sa-header {{
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 12px;
        border-bottom: 1px solid #E7DADD;
      }}
      .sa-name {{
        font-size: 12px;
        font-family: monospace;
        font-weight: 600;
        color: #414A82;
      }}
      .sa-task {{
        font-size: 11px;
        color: #5D638E;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
      }}
      .sa-chevron {{ font-size: 10px; color: #5D638E; }}

      /* Sub-agent timeline */
      .subagent-timeline {{ padding: 6px 12px 10px; }}
      .tl-assistant {{
        font-size: 12px;
        line-height: 1.5;
        padding: 4px 0;
        color: #9ca2d1;
      }}
      .tl-assistant .md code {{
        background: rgba(45,51,107,0.1);
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 11px;
      }}
      .tl-tool-call {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        font-family: monospace;
        padding: 3px 0;
        color: #5D638E;
      }}
      .tl-icon {{
        width: 14px;
        text-align: center;
        color: #286A4A;
        font-size: 10px;
      }}
      .tl-tool-call.is-error .tl-icon {{ color: #A43B4B; }}
      .tl-tname {{ font-weight: 600; color: #414A82; }}
      .tl-targs {{
        color: #5D638E;
        opacity: 0.7;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .tl-turns {{
        font-size: 10px;
        color: #5D638E;
        padding: 4px 0 2px;
        opacity: 0.6;
      }}
      .tl-summary {{
        padding: 8px 0 0;
        font-size: 12px;
        line-height: 1.5;
      }}
      .summary-status {{
        display: inline-block;
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 4px;
        font-weight: 600;
        color: #286A4A;
        background: rgba(183,228,199,0.2);
        margin-bottom: 4px;
      }}
      .summary-status.is-error {{
        color: #A43B4B;
        background: rgba(255,173,173,0.2);
      }}
      .tl-summary .md {{ color: #9ca2d1; }}
      .tl-summary .md code {{
        background: rgba(45,51,107,0.1);
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 11px;
      }}
      .md strong {{ font-weight: 600; }}
      .md br {{ display: block; content: ""; margin-bottom: 2px; }}
      .empty-timeline {{ font-size: 11px; color: #5D638E; font-style: italic; }}
    </style>
    </head>
    <body>
    <h1>📋 Simulated UI Preview — Historic Session Render</h1>
    <div class="path">{escape_html(session_path)}</div>
    {body}
    </body>
    </html>
    ''')

    OUTPUT.write_text(html, encoding="utf-8")
    return str(OUTPUT)


def main():
    session_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SESSION

    if not Path(session_path).exists():
        print(f"Session not found: {session_path}")
        return False

    result = generate_html(session_path)
    if result:
        print(f"✓ Generated: {result}")
        print(f"  Open in browser: file://{result}")
        return True
    return False


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
