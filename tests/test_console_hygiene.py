"""FE-02: first-party browser code logs nothing at info level.

The audit found 32 `console.*` calls in `static/*.js` where the registry had recorded 15. Most of the
increase was one category: fourteen `[STT DEBUG]` statements left behind by a July session that chased why the
microphone would not start, printing on every mic press, on every state transition, and once per STT message
with the whole message object attached. Nobody turned them off, because nothing was ever going to.

That is the shape of this finding, and it is not really about cleanliness. There is no log level, no filter and
no console viewer in this app, so an unconditional `console.log` is permanent output for the lifetime of the
file: it costs time in the hot path (`stt.js` had two per audio packet path), it buries the messages that
matter, and it teaches the next person that printing here is normal.

So the rule is one line, and it is enforced rather than requested:

  `console.log`, `console.info`, `console.debug` and friends do not appear in first-party JS.
  `console.warn` and `console.error` are welcome, because they are the only way a failure the user cannot see
  becomes a failure anyone can.

When tracing is genuinely needed again, build the gate at that moment (one helper reading a query flag, in one
file), rather than leaving fourteen unconditional prints in the meantime.

The same pass found the identical marker in server-side log lines, including one that was a `warning` wearing a
`[STT DEBUG]` prefix, which is how an actionable message gets skimmed. So the second rule here is that the
string `[STT DEBUG]` may not appear anywhere in the repository: it names a debugging session that ended, and
keeping it would teach the next reader that a prefix is a substitute for a message.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
ROOT = STATIC.parent

# Vendored bundles are excluded by suffix: they are not ours to tidy, and a "fix" would be overwritten on the
# next vendor bump. `marked.min.js` and `purify.min.js` are the two that ship today.
FORBIDDEN = re.compile(r"\bconsole\s*\.\s*(log|info|debug|trace|dir|dirxml|table|group|groupEnd|count|countReset|time|timeEnd|assert)\b")


def first_party_scripts() -> list[Path]:
    return sorted(p for p in STATIC.glob("*.js") if not p.name.endswith(".min.js"))


def test_the_stale_debug_marker_is_gone_from_the_whole_repository():
    """`[STT DEBUG]` was the prefix of a July session that chased the microphone; the code it traced is gone.

    Checked across tracked source rather than only `static/`, because the worst instance of it was a server-side
    `log.warning`, where the prefix actively lowered the message's priority in a reader's eye.
    """
    offenders = []
    here = Path(__file__).resolve()
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".js", ".mjs", ".html", ".css"}:
            continue  # .md is where the removal is recorded, so it has to name the thing it removed
        if path.resolve() == here:
            continue  # the enforcer has to contain the string it bans; every other file can say it in words
        if any(part in {".venv", "__pycache__", ".git", "node_modules", "sessions", "data-samples"} for part in path.parts):
            continue
        if "[STT DEBUG]" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert not offenders, (
        "A log line is wearing the prefix of a debugging session that ended. Say what happened instead, at the "
        "level the fact deserves: " + ", ".join(offenders)
    )


def test_there_are_scripts_to_check():
    # Guards the glob above: if `static/` moved, the scan would pass by checking nothing.
    names = {p.name for p in first_party_scripts()}
    assert {"app.js", "chat.js", "stt.js", "socket.js", "timeline.js"} <= names, sorted(names)


def test_no_info_level_logging_in_first_party_js():
    offenders: list[str] = []
    for path in first_party_scripts():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:110]}")

    assert not offenders, (
        "First-party JS logged at info level. Use console.warn or console.error if this reports a failure "
        "someone should see; if it is tracing for a bug you are chasing right now, delete it before you "
        "commit, or add the one-flag gate this file's docstring describes.\n  " + "\n  ".join(offenders)
    )


def test_the_two_silent_microphone_states_still_say_something():
    """The mic button has exactly two no-op paths, and both must stay loud.

    Deleting debug output should not mean deleting the only signal that a press did nothing. `chat.js` returns
    without touching the controller when the controller is missing or the socket is down, and the user gets no
    other feedback, so both branches carry a `console.warn`.
    """
    source = (STATIC / "chat.js").read_text(encoding="utf-8")
    handler = source.split("micButton.addEventListener('click'")[1].split("});")[0]
    assert handler.count("console.warn") == 2, handler
    assert "sttController.toggle(ws)" in handler
