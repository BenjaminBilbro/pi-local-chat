"""The argv every pi child is spawned with, and therefore the shape of its system prompt.

Two decisions live here and both are invisible until you diff a prompt:

``--no-context-files``: pi walks up from the child's cwd, which is ``sessions/<account>``, so left
alone every chat inherits pi-chat's own developer card (39 KB of architecture) in its system prompt.
That card is for a pi session developing on this repo, not one talking through it.

No ``--append-system-prompt`` for either profile. pi discovers ``<agent-dir>/APPEND_SYSTEM.md`` by
itself and that flag only exists to *replace* what it finds, so passing anything here is how profile `r`'s
sessions once lost the memory protocol. ``PI_CHAT_ACCOUNT`` in the child's environment is what tells
the memory extension which persona to inject; see docs/system-prompt-composition.md.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from pi_chat import process as process_module
from pi_chat.process import PiProcess, _loggable_command


class _EofStream:
    def at_eof(self) -> bool:
        return True

    async def readline(self) -> bytes:
        return b""


class _Stdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _FakeProcess:
    stdin = _Stdin()
    stdout = _EofStream()
    stderr = _EofStream()
    pid = 4242

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return 0


@pytest.fixture
def captured_argv(monkeypatch):
    """Spawn without spawning: hand PiProcess a subprocess stand-in and keep its argv."""
    captured: dict[str, object] = {}

    async def fake_exec(*command, stdin=None, stdout=None, stderr=None, cwd=None, env=None, limit=None):
        captured["argv"] = list(command)
        captured["cwd"] = cwd
        captured["env"] = env
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def flags(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


class TestPreviewChannel:
    """The two values a child needs to ask the server what the human is looking at.

    `preview(action:"state")` and the extension's "can the browser read this path" probe are both a
    loopback call from the child back to this server. They cannot work without a port to call and a
    credential to call with, and neither survives a spawn that forgets to pass them.
    """

    async def test_the_child_is_told_which_port_to_call_back_on(
        self, captured_argv, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(process_module, "PORT", 9123)

        await spawn(captured_argv, monkeypatch, "b", tmp_path)

        assert captured_argv["env"]["PI_CHAT_PORT"] == "9123"

    async def test_the_child_gets_a_fresh_state_secret_per_connection(
        self, captured_argv, monkeypatch, tmp_path
    ):
        tokens = set()
        for _ in range(3):
            pi = await spawn(captured_argv, monkeypatch, "b", tmp_path)
            token = captured_argv["env"]["PI_CHAT_PREVIEW_TOKEN"]
            assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)
            # The value handed to the child is the one the server holds on the PiProcess, because
            # that is the pair the state routes compare.
            assert token == pi.preview_token
            tokens.add(token)

        assert len(tokens) == 3, "a per-connection secret that repeats is not per-connection"

    async def test_every_spawn_replaces_both_rather_than_inheriting_a_previous_one(
        self, captured_argv, monkeypatch, tmp_path
    ):
        # A stale inherited value would be worse than an absent one: the child would talk about some
        # other connection's pane and nobody would notice.
        monkeypatch.setenv("PI_CHAT_PORT", "1")
        monkeypatch.setenv("PI_CHAT_PREVIEW_TOKEN", "deadbeef" * 4)
        monkeypatch.setattr(process_module, "PORT", 9123)

        await spawn(captured_argv, monkeypatch, "b", tmp_path)

        assert captured_argv["env"]["PI_CHAT_PORT"] == "9123"
        assert captured_argv["env"]["PI_CHAT_PREVIEW_TOKEN"] != "deadbeef" * 4

async def spawn(captured_argv, monkeypatch, account: str, project_root: Path) -> PiProcess:
    pi = PiProcess(project_root=project_root)
    pi.account = account
    await pi.spawn()
    await pi.kill()
    return pi


class TestContextFiles:
    async def test_children_start_without_a_project_context(self, captured_argv, monkeypatch, tmp_path):
        await spawn(captured_argv, monkeypatch, "b", tmp_path)

        assert "--no-context-files" in captured_argv["argv"]

    async def test_the_flag_is_an_opt_out_not_a_hard_rule(self, captured_argv, monkeypatch, tmp_path):
        monkeypatch.setattr(process_module, "CHILD_CONTEXT_FILES", True)

        await spawn(captured_argv, monkeypatch, "b", tmp_path)

        assert "--no-context-files" not in captured_argv["argv"]


class TestSystemPromptSources:
    """What reaches the child's system prompt, and what must never be passed here again.

    Profile `r`'s persona used to be ``--append-system-prompt <repo>/<persona file>.md`` on top of a second
    append of
    the agent dir's APPEND_SYSTEM.md, because a CLI append *replaces* discovery rather than adding to
    it. It now arrives through the memory extension, keyed on ``PI_CHAT_ACCOUNT``, which is why there
    is nothing profile-specific left in this argv.
    """

    async def test_no_profile_appends_a_system_prompt_from_the_command_line(
        self, captured_argv, monkeypatch, tmp_path
    ):
        for account in ("b", "r"):
            await spawn(captured_argv, monkeypatch, account, tmp_path)

            assert flags(captured_argv["argv"], "--append-system-prompt") == [], (
                "a CLI append replaces the discovered <agent-dir>/APPEND_SYSTEM.md, which is the "
                "memory protocol: passing a persona here silently deletes it"
            )

    async def test_the_child_is_told_which_profile_it_is_serving(self, captured_argv, monkeypatch, tmp_path):
        """The only way anything inside the child can tell one profile's chat from the other's. The memory
        extension's persona rule fires on this, and that profile's voice note never reaches its sessions
        without it: its turns are plain text, so the Telegram prefix the other rule matches cannot appear."""
        await spawn(captured_argv, monkeypatch, "r", tmp_path)

        assert captured_argv["env"]["PI_CHAT_ACCOUNT"] == "r"

    async def test_both_profiles_get_the_same_argv(self, captured_argv, monkeypatch, tmp_path):
        await spawn(captured_argv, monkeypatch, "b", tmp_path)
        ben_argv = list(captured_argv["argv"])
        await spawn(captured_argv, monkeypatch, "r", tmp_path)

        assert captured_argv["argv"] == ben_argv, (
            "the profiles have diverged again. If this is deliberate, say in docs/system-prompt-composition.md "
            "what one profile gets that the other does not"
        )


class TestSharedFlags:
    async def test_rpc_mode_and_preview_extension_are_still_there(self, captured_argv, monkeypatch, tmp_path):
        await spawn(captured_argv, monkeypatch, "b", tmp_path)
        argv = captured_argv["argv"]

        assert argv[argv.index("--mode") + 1] == "rpc"
        assert "--approve" in argv
        assert str(process_module.PREVIEW_EXTENSION) in flags(argv, "--extension")
        assert "PI_CHAT_PREVIEW_DIR" in captured_argv["env"]


class TestSpawnLog:
    """BE-21: this line is written once per connection and lives in the log forever.

    For profile `r` it used to carry the entire system prompt twice, because the prompt is an argv
    value like any other and the argv was logged whole.
    """

    async def test_the_prompt_text_is_absent_from_the_spawn_line(
        self, captured_argv, monkeypatch, tmp_path, caplog
    ):
        # Whatever supplies the flag, the redaction has to follow it. This is the shape profile `r`
        # used to spawn with every connection: the whole of APPEND_SYSTEM.md and the whole of a persona file.
        monkeypatch.setenv(
            "PI_CHAT_PI_ARGS",
            '--append-system-prompt "entire personal persona that must not reach the log"',
        )

        with caplog.at_level(logging.INFO, logger="pi-chat"):
            await spawn(captured_argv, monkeypatch, "r", tmp_path)

        line = next(record.getMessage() for record in caplog.records if "Spawning pi" in record.getMessage())

        assert "must not reach the log" not in line, "the prompt was in the log, on every connection"
        assert "chars of prompt" in line, "the size stays: a prompt bug is debugged with it"
        assert "--append-system-prompt" in line
        # Redaction is the log line only. The child still gets what was asked for.
        assert any("must not reach the log" in token for token in captured_argv["argv"])

    def test_paths_and_switches_are_logged_untouched(self):
        argv = ["pi", "--mode", "rpc", "--no-context-files", "C:/Users/me/.pi/extensions/preview.ts"]

        assert _loggable_command(argv) == argv

    def test_a_long_unrecognized_token_is_truncated_anyway(self):
        """Whatever `PI_CHAT_PI_ARGS` grows next, the log line stays a bounded size."""
        logged = _loggable_command(["pi", "--a-future-flag", "z" * 4000])

        assert logged[2].endswith("more chars>")
        assert len(logged[2]) < 160
