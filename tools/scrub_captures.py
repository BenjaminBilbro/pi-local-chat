"""Remove the injected agent memory from capture fixtures, without changing a single byte count.

Why this exists: ``tools/capture.py`` runs a real ``pi`` child, and ``--no-context-files`` disables
AGENTS.md and CLAUDE.md discovery only. The memory extension still injects its Tier-1 files, so the
recorded stream carries them inside the system prompt: profile notes, home city, family details, chat
ids. ``data-samples/`` is committed, and this repository is public. Four files from the 2026-11-10
markers capture carried the whole injection, which is how a fixture meant to prove event shapes nearly
published someone's household.

What it replaces: the string values of the injected memory sections (``user_profile``, ``core_memory``,
``now``, ``addendum``, ``recent``, ``env_notes``, ``memory_index``) wherever they appear, in the parsed
capture, in the raw stdout stream, and in pi's own session file. A string that merely *mentions* an
injection tag is caught too, so a capture that moves the payload somewhere else still gets scrubbed.

It also replaces personal literals: a home-directory name, or the filename of somebody's profile note,
which the 2026-11-11 and 2026-07-21 captures carry hundreds of times inside paths and directory listings.
Memory blocks are removed wholesale; a username in a path is not memory, it is a fact about one machine, and
it was published anyway because nothing checked. Each literal maps to a replacement of exactly the same byte
length, for the same reason the memory filler does: these fixtures describe byte offsets, and a
five-character shrink would make the fixture lie about its own stream.

Two rules keep that honest. The list of personal strings is never committed, because committing it would
publish the exact thing the tool exists to remove (see `literal_scrubs`); what IS committed is a pattern rule
that recognises a real person's home directory in a path without knowing their name, which is the check CI
can run. And every replacement is byte-length equal by construction, enforced where the pairs are read.

What it guarantees, and why the guarantee matters:

* Every replacement is exactly as long, in bytes, as what it removed. The captures carry ``chunk``,
  ``offset`` and ``bytes`` fields that describe where the pipe put the boundaries, and the raw stream is
  the authority for them. A shortening edit would silently turn a byte-exact fixture into a lying one.
* Nothing is re-serialized. The edit is a substitution of one encoded JSON string for another inside the
  line's text, so key order, separators and the escaping of every other value survive untouched. The tool
  checks that the encoded form it is looking for is actually present, and refuses a file where it is not,
  rather than falling back to "parse and dump".
* The filler says what it is, so a redacted fixture cannot be mistaken for a real prompt by whoever
  reads it next, and ``--check`` recognizes its own work.

Usage:
    uv run python tools/scrub_captures.py --check              # scan tracked files, exit 1 if dirty
    uv run python tools/scrub_captures.py --write PATH [PATH..]
    uv run python tools/scrub_captures.py --check data-samples/*.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Keys of the injected memory envelope. pi puts them on the system-prompt message under `sections`.
MEMORY_SECTION_KEYS = frozenset({
    "user_profile", "core_memory", "env_notes", "now", "recent", "memory_index", "addendum",
})

# A string that contains one of these is injected memory regardless of where it lives in the record.
INJECTION_TAGS = re.compile(r"<(user_profile|core_memory|env_notes|now|recent|memory_index)>")

# Already-scrubbed filler, so `--check` does not flag its own output.
REDACTED = re.compile(r"\[\[redacted by tools/scrub_captures\.py")

# Personal literals that are not memory blocks: a home-directory name, or the filename of somebody's profile
# note, which the captures carry hundreds of times inside paths and directory listings. Memory blocks have a
# shape and a key to give them away. These do not: a username inside a path, or the name of a profile note in
# a listing, is only recognisable as personal by somebody who knows the machine.
#
# So the list is not committed: a `LITERAL_SCRUBS = {"<somebody>": ...}` in a public repository would publish
# precisely what this file exists to keep out of one. It comes from two local sources instead, this machine's
# home directory name and `.scrub-literals` (gitignored) or `PI_CHAT_SCRUB_LITERALS` for a container, holding
# `literal = replacement` lines separated by `|`.
LITERALS_FILE = PROJECT_ROOT / ".scrub-literals"
LITERALS_ENV = "PI_CHAT_SCRUB_LITERALS"

# Home directories that mean nobody. Captures are mostly paths, so a check that flagged every path would be
# switched off inside a week; the neutral names are therefore named, not guessed at.
NEUTRAL_HOME_DIRS = {
    "local user", "user", "users", "public", "shared", "default", "default user", "all users",
    "admin", "administrator", "runner", "runneradmin", "githubrunner", "pi", "test", "sample",
    "example", "<user>", "<name>",
}

# A name is only a leak when it is used as a name. `proxy` contains one and must survive; a password built
# from one, spelled `<name>123`, must not. So the test looks backwards only, and knows what an escape looks like.
JSON_ESCAPE_AT_END = re.compile(r"\\(?:u[0-9a-fA-F]{4}|[nrtbf/\"\\])$")

# A user segment inside a captured path, including the doubled backslash a JSON string uses for the same
# separator. This half of the rule does not need anybody's name, so it is the half CI gets: a fixture naming
# a real person's home directory fails even on a machine that never saw `.scrub-literals`.
HOME_SEGMENT = re.compile(r"(?:Users|home)[\\/]{1,2}([^\\/\"',;:]{1,64})")
# pi names a session directory after the workspace path with `/` and `:` turned into dashes, which is the one
# place a username survives with no path separator in front of it: `--C--Users-Ben-pi-chat-...`.
SESSION_SLUG = re.compile(r"--[A-Za-z]--Users-([^-/\\]{2,64})-")

# What `_filler` pads out of. Named because the check has to know it: a three-byte username in a path becomes
# three bytes of filler, `Loc`, and a check that recognised only the full-length filler would reject the
# scrubber's own work.
FILLER_BASE = "Local User"


def _is_written_placeholder(segment: str, scrubbed: dict[str, str]) -> bool:
    """Whether a home-directory segment is something the scrubber writes, on any machine.

    The allowlist, the filler at whatever length a path needed, and the configured replacements. Nothing here
    depends on knowing a person's name, which is what makes it usable as the CI view of a file.
    """
    low = segment.lower()
    if low in NEUTRAL_HOME_DIRS or low == FILLER_BASE[:len(low)].lower():
        return True
    return any(low == value.lower() for value in scrubbed.values())


def _is_placeholder(segment: str, scrubbed: dict[str, str]) -> bool:
    """`_is_written_placeholder`, plus the one exemption that only makes sense where the names are known.

    A prefix of a name this machine scrubs is a truncated echo of a removal, so here it is not new information.
    CI has no names, so `check_file` does not use this view: see
    `test_the_shipped_corpus_is_clean_for_a_machine_that_knows_no_names`, and `scrub_home_echoes`, which removes
    the fragments from the corpus instead of excusing them at read time.
    """
    if _is_written_placeholder(segment, scrubbed):
        return True
    low = segment.lower()
    return any(full.lower().startswith(low) for full in scrubbed)


def _filler(width: int) -> str:
    """A replacement of exactly `width` bytes.

    Equal length is not a style preference. These fixtures record `chunk`, `offset` and `bytes` for a stream
    that was measured, so a replacement of any other length moves every byte behind it and leaves a fixture
    describing a stream it is not.
    """
    return FILLER_BASE.ljust(width)[:width]


def scrub_home_echoes(text: str, scrubbed: dict[str, str]) -> tuple[str, int]:
    """Neutralise half-typed paths, so a clean fixture is clean for a stranger too.

    A capture records a live stream, and a live stream echoes a half-typed argument: one `ls -la /home/<name>`
    reaches the file as `/home/b`, `/home/bb`, then in full. `substitute_literals` removes the whole name; the
    fragments are not names, and on the machine that knows it they can be excused as prefixes of one. CI does
    not know it, and refused the published corpus over exactly two of those fragments. A fixture whose
    cleanliness depends on a file deliberately kept out of the repository is not clean.

    Each fragment becomes the filler of its own length, byte for byte, so `Loc` stands where `bbi` stood.
    """
    pieces: list[str] = []
    cursor = 0
    count = 0
    for match in HOME_SEGMENT.finditer(text):
        segment = match.group(1)
        low = segment.lower()
        if _is_written_placeholder(segment, scrubbed):
            continue
        if not any(full.lower().startswith(low) and low != full.lower() for full in scrubbed):
            continue
        pieces.append(text[cursor:match.start(1)])
        pieces.append(_filler(len(segment)))
        cursor = match.end(1)
        count += 1
    if not count:
        return text, 0
    pieces.append(text[cursor:])
    return "".join(pieces), count


def _boundary_ok(text: str, start: int) -> bool:
    """Whether the text in front of a match is a word boundary, or only looks like one.

    Refusing a match whose left neighbour is a letter is what keeps `proxy` intact. It nearly threw away the
    opposite case: in JSON-escaped text a newline is two characters, so a filename sitting just after a line
    break reads as `...219B\n<name>.md`, and the `n` of that escape is a letter. Six real leaks survived the
    first version of the rule for exactly that reason, reported as clean.
    """
    if start == 0:
        return True
    before = text[start - 1]
    if not (before.isalnum() or before == "_"):
        return True
    # `\n`, `\t`, `\"`, `\/`, `\\` and `\uXXXX` are escapes: two to six characters meaning one non-letter.
    return bool(JSON_ESCAPE_AT_END.search(text[max(0, start - 6):start]))


def literal_matches(text: str, literal: str) -> list[re.Match[str]]:
    """Every occurrence of `literal` in `text` that is a use of it, not the middle of another word."""
    return [match for match in re.finditer(re.escape(literal), text) if _boundary_ok(text, match.start())]


def substitute_literals(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """Apply `mapping` to `text`, returning the new text and the number of replacements made.

    Spliced by hand rather than with `re.subn`, because the boundary test reads the surrounding text and the
    replacement may contain characters `re.sub` would interpret. Every pair is byte-length equal by the time it
    gets here, so the spans line up as the text is rebuilt.
    """
    total = 0
    for bad, good in mapping.items():
        pieces: list[str] = []
        cursor = 0
        for match in literal_matches(text, bad):
            pieces.append(text[cursor:match.start()])
            pieces.append(good)
            cursor = match.end()
            total += 1
        if cursor:
            pieces.append(text[cursor:])
            text = "".join(pieces)
    return text, total


def _parse_literal_line(line: str, source: str) -> tuple[str, str] | None:
    """One `literal = replacement` pair, or None for a blank or comment line.

    The byte-length rule is enforced here rather than asserted once at import, because these pairs are typed
    by a person into an untracked file and a mistake in it is a silent corruption of every fixture it touches.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        raise ValueError(f"{source}: expected `literal = replacement`, got {line!r}")
    bad, good = (part.strip() for part in line.split("=", 1))
    if not bad:
        raise ValueError(f"{source}: empty literal in {line!r}")
    if len(bad.encode("utf-8")) != len(good.encode("utf-8")):
        raise ValueError(
            f"{source}: {bad!r} -> {good!r} changes the byte length "
            f"({len(bad.encode('utf-8'))} -> {len(good.encode('utf-8'))}), and fixture offsets would start lying"
        )
    return bad, good


def literal_scrubs() -> dict[str, str]:
    """The literal replacements that apply here, derived rather than declared in the repository."""
    out: dict[str, str] = {}

    home = Path.home().name
    if home and home.lower() not in NEUTRAL_HOME_DIRS:
        out[home] = _filler(len(home))

    if LITERALS_FILE.exists():
        for number, line in enumerate(LITERALS_FILE.read_text(encoding="utf-8").splitlines(), 1):
            pair = _parse_literal_line(line, f"{LITERALS_FILE.name}:{number}")
            if pair:
                out[pair[0]] = pair[1]

    if os.environ.get(LITERALS_ENV):
        for number, entry in enumerate(os.environ[LITERALS_ENV].split("|"), 1):
            pair = _parse_literal_line(entry, f"${LITERALS_ENV}[{number}]")
            if pair:
                out[pair[0]] = pair[1]

    return out


def unneutral_home_dirs(text: str, scrubbed: dict[str, str] | None = None) -> list[str]:
    """Home-directory segments in `text` that name a person rather than a placeholder.

    Three kinds of segment are not leaks, and each exemption is here because the check would otherwise be
    either blind or unusable:

    * a name in `NEUTRAL_HOME_DIRS`, or a replacement this tool writes; a CI box has no `.scrub-literals`, so
      without this it would reject the scrubbed fixtures it exists to protect;
    * a prefix of a name the scrubber removes. A capture records a live stream, and a live stream echoes a
      half-typed argument, so the same `ls -la /home/<name>` appears truncated on the way to being complete.
      A truncated echo of a removed name carries nothing the full name did not.
    """
    scrubbed = literal_scrubs() if scrubbed is None else scrubbed
    found = {match.group(1) for match in HOME_SEGMENT.finditer(text)}
    found |= {match.group(1) for match in SESSION_SLUG.finditer(text)}
    return sorted(name for name in found if not _is_placeholder(name, scrubbed))

# The originals live outside the repository. Keep this pointer current if the backup moves.
BACKUP_NOTE = "~/.pi/agent/tmp/pi-chat-captures-2026-11-17/"


def _walk(node, key: str | None = None):
    """Yield (dict_key, value) for every string leaf, with the key it was stored under."""
    if isinstance(node, str):
        yield key, node
    elif isinstance(node, dict):
        for name, value in node.items():
            yield from _walk(value, name)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)


# Memory blocks are long. The length floor keeps a key that merely happens to be spelled `now` from
# being treated as a payload. Below the floor a value is still caught if it carries an injection tag,
# so this only narrows the by-key rule.
MIN_MEMORY_CHARS = 80


def personal_values(record: object) -> list[tuple[str, str]]:
    """The string values in `record` that must not leave this machine.

    Returns (section_name, value) pairs. A section whose text is already redacted is skipped, which is
    what makes the tool safe to re-run.
    """
    found: list[tuple[str, str]] = []
    for key, value in _walk(record):
        is_memory = ((key in MEMORY_SECTION_KEYS and len(value) >= MIN_MEMORY_CHARS)
                     or bool(INJECTION_TAGS.search(value)))
        if not is_memory or REDACTED.search(value):
            continue
        found.append((key or "unkeyed", value))
    return found


def _filler_for(section: str, encoded: str) -> str:
    """An encoded JSON string, the same number of UTF-8 bytes as `encoded`, saying what used to be here.

    Byte length, not character length: these blocks are full of em dashes and accented words, so a
    same-length ASCII filler would come out shorter in bytes and move every offset behind it.

    The explanatory note is long. When the payload is smaller than the note, a shorter one takes its
    place rather than failing, because a 120-character memory string still has to come out.
    """
    budget = len(encoded.encode("utf-8")) - 2
    heads = [
        f"[[redacted by tools/scrub_captures.py: the {section} block of the agent memory that was "
        f"injected into this pi run; the originals are in {BACKUP_NOTE} and the fixture's shapes, "
        f"offsets and byte counts are unchanged]]",
        f"[[redacted by tools/scrub_captures.py: injected {section} block]]",
        f"[[redacted:{section}]]",
    ]
    head = next((h for h in heads if len(h.encode("utf-8")) <= budget), None)
    if head is None:
        raise ValueError(f"{section}: payload ({budget} bytes) is shorter than the shortest redaction mark")
    pad = budget - len(head.encode("utf-8"))
    return json.dumps(head + "x" * pad, ensure_ascii=False)


def scrub_line(line: str) -> tuple[str, int]:
    """Scrub one JSONL line.

    A line that is not JSON at all is returned untouched with no hits, which is different from the failure
    this function still raises for. Not-JSON means a pretty-printed fixture such as
    `markers_rpc_capture.jsonl.meta.json`, where there is no memory envelope to find and the literal pass
    covers it anyway. A JSON line whose memory payload cannot be located verbatim is the dangerous case, and
    it still stops the run.
    """
    stripped = line.rstrip("\n")
    if not stripped.strip():
        return line, 0
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return line, 0
    hits = personal_values(record)
    if not hits:
        return line, 0

    out = line
    for section, value in hits:
        encoded = json.dumps(value, ensure_ascii=False)
        if encoded not in out:
            encoded = json.dumps(value)  # the writer escaped non-ASCII instead
        if encoded not in out:
            raise ValueError(
                f"cannot locate the encoded {section} value in this line; refusing to guess, because a "
                "partial scrub is worse than no scrub"
            )
        replacement = _filler_for(section, encoded)
        assert len(replacement.encode("utf-8")) == len(encoded.encode("utf-8")), \
            "the replacement must not change the byte count"
        out = out.replace(encoded, replacement, 1)
    return out, len(hits)


def scrub_text(text: str) -> tuple[str, int, int]:
    """Scrub a whole JSONL document, line by line, preserving the trailing newline structure.

    Returns (text, memory_blocks_replaced, literals_replaced), where the literal count includes the path
    fragments that `scrub_home_echoes` neutralises. The literal passes run on the raw text rather than on parsed
    values, because a username appears inside paths buried in larger strings, and a path is not worth parsing
    here: it is a substring somebody's privacy depends on.
    """
    total = 0
    pieces: list[str] = []
    for line in text.splitlines(keepends=True):
        new, hits = scrub_line(line)
        total += hits
        pieces.append(new)
    out = "".join(pieces)

    literals_map = literal_scrubs()
    out, literals = substitute_literals(out, literals_map)
    # Echoes second: the whole names are gone by then, so anything left in a path position is a fragment, and
    # a fragment is the only thing `scrub_home_echoes` rewrites.
    out, echoes = scrub_home_echoes(out, literals_map)
    return out, total, literals + echoes


def read_capture(path: Path) -> str:
    """Read a capture as bytes and decode it.

    Deliberately not `read_text`: universal-newline translation turns CRLF into LF, and this file is
    written in text mode on Windows, so every line ends CRLF. Reading it that way silently dropped one
    byte per line (893 in the 2026-11-10 capture) while the tool reported "bytes unchanged", which is
    the exact failure this tool exists to prevent.
    """
    if path.name.endswith(".gz"):
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_bytes().decode("utf-8")


def write_capture(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    if path.name.endswith(".gz"):
        # mtime=0 so scrubbing twice produces the same bytes, and a re-run cannot look like a change.
        path.write_bytes(gzip.compress(data, mtime=0))
    else:
        path.write_bytes(data)


def check_file(path: Path) -> int:
    """Count personal payloads in one file (parsable or not, checked as text)."""
    text = read_capture(path)
    hits = 0
    try:
        for line in text.splitlines():
            if line.strip():
                hits += len(personal_values(json.loads(line)))
    except json.JSONDecodeError:
        hits = len(INJECTION_TAGS.findall(text))
    # Literals are counted in the raw text either way: a pretty-printed .json fixture never parses line by
    # line, and the personal-name rule has to hold for it too, not only for the JSONL captures.
    literals = literal_scrubs()
    hits += sum(len(literal_matches(text, bad)) for bad in literals)
    # The committed half of the rule, in the CI view: an empty literal map, so a segment counts unless the
    # name-free placeholders explain it. Checking with this machine's own list lets a corpus pass at home and
    # fail on GitHub, which is what happened on 2026-11-17, and "clean here, dirty there" is not a gate.
    hits += len(unneutral_home_dirs(text, {}))
    return hits


def tracked_files() -> list[Path]:
    """Tracked capture fixtures, which is the set where an injection can hide.

    Scope is deliberate. `docs/system-prompt-composition.md` explains the injection mechanism and quotes
    its tag names; scanning prose for them produces a finding that is not one, and a guard that cries
    wolf gets ignored. Pass paths explicitly to scan anything else.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "data-samples"], cwd=PROJECT_ROOT, capture_output=True, text=True,
        check=True,
    ).stdout
    # `.json` is included because the capture metadata is a fixture too: `markers_rpc_capture.jsonl.meta.json`
    # carried the username eight times while the JSONL guard looked only at .jsonl and .gz.
    return [PROJECT_ROOT / name for name in out.split("\x00") if name.endswith((".jsonl", ".gz", ".json"))]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="Report files that still carry injected memory or personal data and exit 1")
    mode.add_argument("--write", action="store_true", help="Scrub the named files in place")
    parser.add_argument("paths", nargs="*", help="Files to check (default: every tracked file)")
    args = parser.parse_args(argv)

    if args.write:
        status = 0
        for raw in args.paths:
            path = Path(raw)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            before = path.stat().st_size
            text = read_capture(path)
            scrubbed, hits, literals = scrub_text(text)
            if not hits and not literals:
                print(f"{path.name:38s} clean")
                continue
            if len(scrubbed.encode("utf-8")) != len(text.encode("utf-8")):
                print(f"!! {path}: byte length changed, refusing to write")
                status = 1
                continue
            write_capture(path, scrubbed)
            after = path.stat().st_size
            note = f"{before} bytes unchanged" if after == before else (
                # A .gz holds the same text, so the text length is what the byte-exactness promise is about.
                # The compressed size moves because the new bytes compress differently, which is not a
                # fixture lying about its stream and must not be reported as if it were.
                f"text length unchanged, container {before} -> {after} bytes after recompressing"
            )
            print(f"{path.name:38s} scrubbed {hits} injected block(s), {literals} literal(s), {note}")
        return status

    paths = [Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p for p in args.paths] or tracked_files()
    dirty: list[tuple[Path, int, list[str]]] = []
    for path in paths:
        if not path.exists() or path.is_dir():
            continue
        hits = check_file(path)
        if hits:
            dirty.append((path, hits, unneutral_home_dirs(read_capture(path), {})))
    if not dirty:
        print(f"clean: no injected memory or personal literals in {len(paths)} file(s)")
        return 0
    print(f"{len(dirty)} file(s) carry injected agent memory or personal literals:")
    for path, hits, homes in dirty:
        # Naming what was found is the difference between a gate somebody can act on and a number. This is a
        # terminal on the machine that captured the file, so printing the name it objected to is safe here in a
        # way that committing it to the repository is not.
        detail = f"  home dir(s): {', '.join(homes)}" if homes else ""
        print(f"  {path.relative_to(PROJECT_ROOT)}  ({hits} hit(s)){detail}")
    print(f"Originals belong outside the repository: {BACKUP_NOTE}")
    print(f"Personal literals come from {LITERALS_FILE.name} or ${LITERALS_ENV}, never from this file.")
    print("Scrub with: uv run python tools/scrub_captures.py --write <file> [<file>...]")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
