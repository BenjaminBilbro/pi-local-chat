"""The scrub tool is what keeps a public repository from publishing someone's household.

Two of its guarantees are load-bearing and easy to lose in a refactor, so they are asserted rather than
promised: a replacement is the same number of UTF-8 bytes as what it removed (the captures carry
`offset` and `bytes` fields describing pipe boundaries, and `tools/capture.py` writes CRLF on Windows),
and a value it cannot find verbatim is refused rather than approximated.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("scrub_captures", ROOT / "tools" / "scrub_captures.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scrub():
    return _tool()


MEMORY = "<core_memory>\nsafety rules, home city, someone's family\n</core_memory>"


def capture_record(**sections) -> dict:
    return {"seq": 1, "offset": 0, "bytes": 42,
            "event": {"type": "message_start", "message": {"sections": sections}}}


def test_the_memory_sections_are_the_ones_removed(scrub):
    record = capture_record(core_memory=MEMORY, user_profile="# USER\n" + "x" * 200,
                            description="a tool name, harmless")

    found = dict(scrub.personal_values(record))

    assert set(found) == {"core_memory", "user_profile"}
    assert "description" not in found


def test_a_replacement_costs_exactly_what_it_removed(scrub):
    """Em dashes and accents are why this is bytes and not characters."""
    text = "# USER — Ben\n" + "café — naïve — 日本語\n" * 12
    record = capture_record(user_profile=text)
    record["event"]["message"]["sections"]["description"] = "a tool name, harmless"
    line = json.dumps(record, ensure_ascii=False) + "\n"

    scrubbed, hits = scrub.scrub_line(line)

    assert hits == 1
    assert len(scrubbed.encode("utf-8")) == len(line.encode("utf-8"))
    assert "日本語" not in scrubbed
    sections = json.loads(scrubbed)["event"]["message"]["sections"]
    assert sections["description"] == "a tool name, harmless", "untouched values must survive verbatim"
    assert sections["user_profile"].startswith("[[redacted by tools/scrub_captures.py")


def test_scrubbing_twice_changes_nothing(scrub):
    line = json.dumps(capture_record(core_memory=MEMORY), ensure_ascii=False) + "\n"

    once, first_hits = scrub.scrub_line(line)
    twice, second_hits = scrub.scrub_line(once)

    assert first_hits == 1
    assert second_hits == 0, "an already-redacted block must not be counted or rewritten again"
    assert once == twice


def test_crlf_survives_the_round_trip(scrub, tmp_path):
    """`read_text` would translate CRLF to LF and quietly drop a byte per line while reporting success."""
    path = tmp_path / "capture.jsonl"
    original = json.dumps(capture_record(core_memory=MEMORY), ensure_ascii=False) + "\r\n"
    path.write_bytes(original.encode("utf-8"))

    text = scrub.read_capture(path)
    scrubbed, hits, literals = scrub.scrub_text(text)
    scrub.write_capture(path, scrubbed)

    assert hits == 1
    written = path.read_bytes()
    assert written.endswith(b"\r\n"), "line endings are part of the byte count"
    assert len(written) == len(original.encode("utf-8"))


def test_a_personal_literal_is_replaced_at_the_same_byte_length(scrub, monkeypatch):
    """A username inside a captured path is not memory, and it still must not be published.

    Equal byte length is the load-bearing part. These fixtures record `offset` and `bytes` for a stream that
    was measured on a machine, so a replacement of any other length shifts everything behind it and leaves a
    fixture that describes a stream it is not.

    The literal is supplied through the environment rather than written into the assertion. A test that
    spelled the real one would be a file in the public repository carrying the thing the tool removes.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "Gerald = Robert")

    line = json.dumps({"cwd": r"C:\Users\Gerald\pi-chat\data-samples"}) + "\n"
    scrubbed, hits, literals = scrub.scrub_text(line)

    assert "Gerald" not in scrubbed
    assert json.loads(scrubbed)["cwd"] == r"C:\Users\Robert\pi-chat\data-samples"
    assert hits == 0, "no memory block here, so the memory pass must report nothing"
    assert literals == 1
    assert len(scrubbed.encode("utf-8")) == len(line.encode("utf-8")), "a scrub that moves offsets is a fake fixture"


def test_a_name_inside_a_longer_word_is_left_alone(scrub, monkeypatch):
    """A name is also the middle of an ordinary word, and that collision is how this rule got its shape.

    The first pass replaced every substring and reported clean byte counts while turning 21 mentions of a
    proxy into the scrubbed spelling. Byte-exact and wrong is worse than leaking, so: something glued to the
    front means a different word, something glued to the back is still the name (a password built from one).
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "ann = ivy")
    text = json.dumps({"command": "uv run python planning.py", "file": "ann.md", "pw": "ann123"}) + "\n"

    scrubbed, hits, literals = scrub.scrub_text(text)
    record = json.loads(scrubbed)

    assert record["command"] == "uv run python planning.py", "the scrub replaced a word that merely contains the name"
    assert record["file"] == "ivy.md"
    assert record["pw"] == "ivy123", "a name glued to a digit is still the name"
    assert hits == 0 and literals == 2
    assert len(scrubbed.encode("utf-8")) == len(text.encode("utf-8"))


def test_a_name_after_an_escaped_newline_is_still_a_name(scrub, monkeypatch):
    """In JSON text a newline is two characters, so the neighbour of a leak can be the letter `n`.

    The boundary rule exists to keep words like `proxy` whole. On its first run that same rule let six real
    leaks through: a captured directory listing in which every filename followed a newline, so each name sat
    behind the `n` of `\n` and looked like the middle of a word. A check that says clean while the corpus still
    carries the name is the failure this whole file is about, so the escape is a case in the test, not a
    comment in the code.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "ann = ivy")
    text = json.dumps({"listing": "notes.md 3K\nann.md 1.7K\nplanning.md 2K"}) + "\n"

    scrubbed, hits, literals = scrub.scrub_text(text)
    record = json.loads(scrubbed)

    assert record["listing"] == "notes.md 3K\nivy.md 1.7K\nplanning.md 2K"
    assert hits == 0 and literals == 1, "one name after an escape, and the word that merely contains it untouched"
    assert len(scrubbed.encode("utf-8")) == len(text.encode("utf-8"))


def test_a_literal_pair_that_would_move_the_bytes_is_refused(scrub, monkeypatch):
    """The pairs are typed by a person into an untracked file, so the byte rule is enforced where they are read.

    A typo here is not a crash at the typo. It shortens every fixture it touches, and the fixtures keep
    passing, because a fixture cannot tell that the stream it describes is no longer the same length.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "Gerald = Bob")

    with pytest.raises(ValueError, match="changes the byte length"):
        scrub.literal_scrubs()


def test_a_home_directory_that_names_a_person_fails_the_check_with_no_literal_list_at_all(scrub, monkeypatch, tmp_path):
    """The half of the rule that can live in the repository, which is the half a stranger's CI gets.

    A contributor who never heard of `.scrub-literals` still cannot publish a capture carrying their own home
    directory, because the check does not need to know the name to know that `/home/someone/` is one.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "")
    leaky = tmp_path / "capture.jsonl"
    leaky.write_text(json.dumps({"cwd": "/home/mallory/pi-chat"}) + "\n", encoding="utf-8")
    neutral = tmp_path / "clean.jsonl"
    neutral.write_text(json.dumps({"cwd": r"C:\Users\Local User\pi-chat"}) + "\n", encoding="utf-8")

    assert scrub.unneutral_home_dirs(leaky.read_text(encoding="utf-8")) == ["mallory"]
    assert scrub.check_file(leaky) >= 1
    assert scrub.check_file(neutral) == 0, "the scrubbed placeholder must not be flagged as a person"


def test_a_truncated_echo_is_excused_at_check_time_only_where_the_name_is_known(scrub, monkeypatch):
    """A capture records a live stream, and a live stream echoes a half-typed argument.

    `ls -la /home/charlie` appears three times on the way to the terminal: as `/home/c`, `/home/cha`, then in
    full. Flagging the prefixes would be flagging a name that is already configured for removal, and a check
    that can never go green is a check that gets deleted.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "charlie = dorothy")

    assert scrub.unneutral_home_dirs('"command": "ls -la /home/cha"') == []
    assert scrub.unneutral_home_dirs('"cwd": "/home/mallory/x"') == ["mallory"]


def test_check_sees_a_personal_literal_in_an_unparsable_meta_file(scrub, monkeypatch, tmp_path):
    """`markers_rpc_capture.jsonl.meta.json` carried a username eight times, and it is pretty-printed.

    It never parses line by line, so a check that only understood JSONL would have called it clean.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "Gerald = Robert")
    path = tmp_path / "capture.jsonl.meta.json"
    path.write_text('{\n  "cwd": "C:\\\\Users\\\\Gerald\\\\pi-chat"\n}\n', encoding="utf-8")

    # Two hits, and both are true: the literal is still in there, and so is a home directory that the name-free
    # CI view cannot explain. Counting only the literal is how this corpus passed at home and failed on GitHub.
    assert scrub.check_file(path) == 2


def test_the_personal_literal_list_cannot_be_committed(scrub):
    """The list of personal strings is the last file that belongs in a public repository.

    So the mechanism is the rule, and a rule nobody checks is a guess: this asserts that git is actually
    ignoring the file, which is the only reason a `git add -A` after a capture session stays harmless.
    """
    import subprocess

    assert scrub.LITERALS_FILE == scrub.PROJECT_ROOT / ".scrub-literals"
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "--", scrub.LITERALS_FILE.name],
        cwd=scrub.PROJECT_ROOT, capture_output=True,
    )

    assert probe.returncode == 0, f"{scrub.LITERALS_FILE.name} must stay gitignored: it names the people the scrubber protects"


def test_a_file_that_no_longer_matches_its_own_json_is_refused(scrub):
    """A partial scrub is worse than no scrub, so a value that cannot be found verbatim stops the run.

    The construction is a file written by some third encoder that escaped an ASCII letter as `\\u0073`.
    It is still valid JSON, so it parses, but neither of the two encodings the tool looks for (as written,
    or with non-ASCII escaped) appears in the text, and guessing here is how a half-scrubbed fixture
    would reach a public repository looking clean.
    """
    line = json.dumps(capture_record(core_memory=MEMORY), ensure_ascii=False)
    hand_edited = line.replace("safety rules", "\\u0073afety rules")
    assert json.loads(hand_edited), "the line must still be valid JSON for this to be the interesting case"

    with pytest.raises(ValueError, match="cannot locate the encoded"):
        scrub.scrub_line(hand_edited + "\n")


def test_a_string_that_merely_carries_an_injection_tag_is_caught_wherever_it_lives(scrub):
    record = {"event": {"type": "response", "nested": [{"payload": "prefix <user_profile> suffix " + "y" * 90}]}}

    assert [key for key, _ in scrub.personal_values(record)] == ["payload"]


def test_a_short_key_named_now_is_not_mistaken_for_a_memory_block(scrub):
    record = {"event": {"type": "turn_start", "now": "2026-11-17T04:00:00Z"}}

    assert scrub.personal_values(record) == []


def test_a_half_typed_path_is_neutralised_like_the_full_one(scrub, monkeypatch):
    """The echo of a half-typed argument is not a name, but it is a fragment of one.

    `ls -la /home/charlie` arrives in a capture three times: as `/home/c`, `/home/cha`, then whole. Replacing
    only the whole name leaves the fragments, and they are unrecognisable as scrubbed output to anyone who does
    not have the local literal list, which is the same as saying they are unrecognisable to CI.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "charlie = example")
    text = json.dumps({"a": "ls -la /home/c", "b": "ls -la /home/cha", "c": "ls -la /home/charlie"}) + "\n"

    scrubbed, hits, literals = scrub.scrub_text(text)

    assert literals == 3, "two fragments and the whole name, all three in one document"
    assert "charlie" not in scrubbed
    assert json.loads(scrubbed)["c"] == "ls -la /home/example"
    # What CI sees: a machine with no names at all must call this clean.
    assert scrub.unneutral_home_dirs(scrubbed, {}) == []
    assert len(scrubbed.encode("utf-8")) == len(text.encode("utf-8"))


def test_a_replacement_has_to_read_as_a_placeholder(scrub, monkeypatch):
    """`charlie = rogerxx` removes a name and installs one, and CI cannot tell which is which.

    So the replacement side of a pair is held to the same standard as an unscrubbed path: it has to be something
    the name-free rules already call a placeholder. Otherwise the cure gets published as a leak, and the only
    reason anyone notices is that it still looks like a username.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "charlie = rogerxx")
    text = json.dumps({"cwd": "/home/charlie/pi-chat"}) + "\n"

    scrubbed, _hits, literals = scrub.scrub_text(text)

    assert literals == 1 and "charlie" not in scrubbed
    assert scrub.unneutral_home_dirs(scrubbed, {}) == ["rogerxx"], "a name-shaped replacement is still a name"


def test_the_shipped_corpus_is_clean_for_a_machine_that_knows_no_names(scrub, monkeypatch, tmp_path):
    """The CI twin of the corpus check, and the one that was missing when CI went red.

    `test_the_shipped_corpus_is_clean` runs with this machine's literal list, so it can excuse a fragment as a
    prefix of a name it knows. GitHub Actions has no such list, deliberately: the list is the thing that must
    never be published. It rejected `rpc_capture.jsonl` for two of those fragments on 2026-11-17, which is the
    difference between a fixture that is clean and a fixture that is clean at home.
    """
    monkeypatch.setenv(scrub.LITERALS_ENV, "")
    monkeypatch.setattr(scrub, "LITERALS_FILE", tmp_path / "absent-scrub-literals")
    monkeypatch.setattr(scrub.Path, "home", classmethod(lambda cls: tmp_path / "runner"))

    assert scrub.literal_scrubs() == {}, "the simulation is only worth running if this machine is nobody's"
    dirty = [(p.name, scrub.check_file(p)) for p in scrub.tracked_files() if scrub.check_file(p)]

    assert dirty == [], f"fixtures only pass where the personal names are known: {dirty}"


def test_the_shipped_corpus_is_clean(scrub):
    """The check that should have existed before these files were ever committed.

    Two halves run here. The personal literals, which only this machine knows, and the home-directory pattern,
    which needs no names and is therefore the half a contributor's CI actually enforces.
    """
    paths = scrub.tracked_files()
    assert paths, "no fixtures to check"

    dirty = [(p.name, scrub.check_file(p)) for p in paths if scrub.check_file(p)]

    assert dirty == [], f"tracked fixtures carry injected agent memory: {dirty}"


def test_the_backup_of_the_originals_is_where_the_tool_says_it_is(scrub):
    """The redaction is only survivable if the originals can be found later.

    The backup lives outside the repository, on the machine that did the scrubbing, so CI skips this and
    it earns its keep here. What it protects against is the quiet version of the accident: someone
    scrubs a fixture, the pointer rots, and the only copy of a tedious capture is gone.
    """
    backup = Path(os.path.expanduser(scrub.BACKUP_NOTE))
    if not backup.is_dir():
        pytest.skip(f"{backup} is not on this machine; the originals are not portable by design")

    for name in ("markers_rpc_capture.jsonl", "markers_rpc_capture.jsonl.gz", "markers_session.jsonl"):
        assert (backup / name).is_file(), f"{backup / name} missing"
    assert (backup / "README.md").is_file(), "the backup needs its provenance note"
