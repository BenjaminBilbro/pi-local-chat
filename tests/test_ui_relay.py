"""The UI relay: a child process asking the human something through somebody else's tab.

Two paths are tested here. pi's own dialog methods (`select`/`confirm`/`input`/`editor`) arrive on the
child's stdout as `extension_ui_request` and are answered by an `extension_ui_response` back on stdin;
the questionnaire shape has no RPC surface at all in pi, so its child reaches the browser over HTTP
using the per-connection token. Everything the browser sends is treated as untrusted, which is why
normalisation runs server-side in both directions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pi_chat import app as app_module
from pi_chat import ui_relay
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password
from pi_chat.websocket import _dispatch_command


class StubPi:
    """The slice of PiProcess the relay touches, with both wires recorded."""

    def __init__(self, token: str = "0" * 32) -> None:
        self.preview_token = token
        self.browser: list[dict] = []
        self.sent: list[dict] = []
        self.proc = object()
        self.audit = None

    async def send_browser_json(self, payload: dict) -> None:
        self.browser.append(payload)

    async def send(self, message: dict) -> None:
        self.sent.append(message)


async def settle(browser: list) -> None:
    """Wait until a request has actually reached the socket, rather than sleeping a guess.

    A fixed sleep is the flake here, and it also lies about the system: nothing in this design promises
    the request arrives within 20 ms, only that it arrives before any answer could be given. Returning
    early is worse than slow - the abort would then cancel nothing, and the route would wait fifteen
    minutes for a tab that was never asked.
    """
    for _ in range(300):
        if browser:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the request never reached the browser")


@pytest.fixture(autouse=True)
def _clear_relay_state():
    ui_relay.PENDING.clear()
    ui_relay.CONNECTIONS.clear()
    yield
    ui_relay.PENDING.clear()
    ui_relay.CONNECTIONS.clear()


# ------------------------------------------------------------------ the questionnaire's shape

def test_defaults_match_the_extension():
    (question,) = ui_relay.normalise_questions({"questions": [{"id": "scope", "prompt": "How big?"}]})
    assert question == {
        "id": "scope",
        # A question with no label is Q1, `allowOther` is on unless turned off, and a question with no
        # options is a free-text field: all three are the extension's defaults, and if the two ever
        # disagree the model gets a different form depending on which client asked.
        "label": "Q1",
        "prompt": "How big?",
        "options": [],
        "allowOther": True,
        "multiSelect": False,
    }


@pytest.mark.parametrize(
    "body, reason",
    [
        ({}, "non-empty array"),
        ({"questions": []}, "non-empty array"),
        ({"questions": "x"}, "non-empty array"),
        ({"questions": [{"prompt": "no id"}]}, "required"),
        ({"questions": [{"id": "a", "prompt": "p"}, {"id": "a", "prompt": "q"}]}, "duplicate"),
        ({"questions": [{"id": "a", "prompt": 3}]}, "must be a string"),
        # A present-but-wrong optional field is rejected rather than coerced: `label: 42` reaching a
        # person as a tab heading is a form nobody should see.
        ({"questions": [{"id": "a", "prompt": "p", "label": 42}]}, "must be a string"),
        ({"questions": [{"id": "a", "prompt": "p", "options": "x"}]}, "must be an array"),
        ({"questions": [{"id": "a", "prompt": "p", "options": [{"value": ""}]}]}, "needs a value"),
    ],
)
def test_bad_questionnaires_are_refused(body, reason):
    with pytest.raises(ui_relay.QuestionError) as caught:
        ui_relay.normalise_questions(body)
    assert reason in str(caught.value)


def test_size_limits_are_real():
    many = [{"id": f"q{n}", "prompt": "p"} for n in range(ui_relay.MAX_QUESTIONS + 1)]
    with pytest.raises(ui_relay.QuestionError, match="too many questions"):
        ui_relay.normalise_questions({"questions": many})

    options = [{"value": f"v{n}"} for n in range(ui_relay.MAX_OPTIONS + 1)]
    with pytest.raises(ui_relay.QuestionError, match="over the limit"):
        ui_relay.normalise_questions({"questions": [{"id": "a", "prompt": "p", "options": options}]})

    (question,) = ui_relay.normalise_questions({"questions": [{"id": "a", "prompt": "p " + "x" * 5000}]})
    assert len(question["prompt"]) == ui_relay.MAX_TEXT


def test_answers_are_filtered_to_the_questions_that_were_asked():
    questions = ui_relay.normalise_questions({"questions": [
        {"id": "a", "prompt": "p", "options": [{"value": "x", "label": "X"}, {"value": "y", "label": "Y"}]},
        {"id": "b", "prompt": "p"},
    ]})
    answers = ui_relay.normalise_answers(questions, [
        # Out of asked order in, and the model reads them in the order they were asked: a form whose
        # answers reorder per reload is not readable as a form.
        {"id": "b", "value": "typed by hand"},
        {"id": "nope", "value": "invented"},
        {"id": "a", "value": "x"},
        {"id": "a", "value": "y"},
    ])
    assert [entry["id"] for entry in answers] == ["a", "b"]
    # Last answer wins: an answer form can be resubmitted and the second one is the one meant.
    assert answers[0]["value"] == "y" and answers[0]["index"] == 2
    assert answers[0]["wasCustom"] is False
    # A value that is not on the list is only an answer when the question lets you write your own, and
    # it is reported as writing your own.
    assert answers[1]["wasCustom"] is True


def test_free_text_on_a_closed_list_is_not_an_answer():
    (question,) = ui_relay.normalise_questions({
        "questions": [{"id": "a", "prompt": "p", "options": [{"value": "x"}], "allowOther": False}]
    })
    assert ui_relay.normalise_answers([question], [{"id": "a", "value": "made up"}]) == []
    assert ui_relay.normalise_answers([question], [{"id": "a", "value": "x"}])[0]["label"] == "x"


def test_multi_select_answers():
    (question,) = ui_relay.normalise_questions({
        "questions": [{"id": "m", "prompt": "p", "options": [{"value": "x"}, {"value": "y"}],
                      "multiSelect": True}]
    })
    (answer,) = ui_relay.normalise_answers([question], [{
        "id": "m",
        "selections": [{"value": "x"}, {"value": "written in"}, "not an object", {"value": ""}],
    }])
    assert [entry["value"] for entry in answer["selections"]] == ["x", "written in"]
    assert answer["selections"][0]["index"] == 1
    assert answer["selections"][1]["wasCustom"] is True
    assert answer["label"] == "x, written in"


# ------------------------------------------------------------------ pi's own dialog methods

def test_dialog_request_payload_is_derived_not_forwarded():
    payload = ui_relay.dialog_request_payload({
        "type": "extension_ui_request",
        "id": "abc",
        "method": "select",
        "title": "Pick",
        "message": "Which one?",
        "options": ["a", "b", 3, None],
        "timeout": 4000,
    })
    assert payload == {
        "type": "ui_request", "id": "abc", "method": "select", "title": "Pick",
        "message": "Which one?", "options": ["a", "b"], "timeoutMs": 4000,
    }


def test_an_editor_request_keeps_its_text():
    # From the real bytes in `data-samples/questionnaire_dialog_rpc_capture.jsonl`: an `editor` request
    # has a `prefill` and no `message`, and a browser that never sees the text has nothing to edit.
    payload = ui_relay.dialog_request_payload({
        "type": "extension_ui_request", "id": "e1", "method": "editor",
        "title": "Fix the title", "prefill": "a short title",
    })
    assert payload == {
        "type": "ui_request", "id": "e1", "method": "editor",
        "title": "Fix the title", "prefill": "a short title",
    }


@pytest.mark.parametrize("event", [
    {"type": "extension_ui_request", "id": "x", "method": "notify", "message": "hi"},
    {"type": "extension_ui_request", "method": "select", "options": []},
    {"type": "chat"},
])
def test_what_is_not_a_dialog_is_ignored(event):
    # `notify` and friends are display hints that may not be answered; a dialog with no id could never
    # be matched to a response, so pi would wait on it forever.
    assert ui_relay.dialog_request_payload(event) is None


def test_dialog_response_shapes():
    assert ui_relay.dialog_response({"id": "a", "method": "select", "value": "Option A"}) == {
        "type": "extension_ui_response", "id": "a", "value": "Option A"
    }
    assert ui_relay.dialog_response({"id": "a", "method": "confirm", "confirmed": False}) == {
        "type": "extension_ui_response", "id": "a", "confirmed": False
    }
    assert ui_relay.dialog_response({"id": "a", "cancelled": True}) == {
        "type": "extension_ui_response", "id": "a", "cancelled": True
    }


@pytest.mark.parametrize("payload", [
    {"method": "select", "value": "a"},
    {"id": "a", "method": "select", "value": 3},
    {"id": "a", "method": "confirm", "confirmed": "yes"},
    {"id": "a", "method": "telepathy", "value": "x"},
    {},
])
def test_an_answer_that_could_not_be_matched_is_dropped(payload):
    # Forwarding a response pi cannot pair with a request leaves the child blocked on a dialog nobody
    # will answer, which is worse than refusing it.
    assert ui_relay.dialog_response(payload) is None


# ------------------------------------------------------------------ the relay itself

async def test_ask_pushes_and_answer_resolves():
    pi = StubPi()
    ui_relay.register(pi)
    task = asyncio.create_task(ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}))
    await asyncio.sleep(0)
    assert pi.browser == [{"type": "questionnaire_request", "id": "r1", "questions": []}]

    assert ui_relay.answer("r1", {"cancelled": False, "answers": [{"id": "a"}]}) is True
    assert await task == {"cancelled": False, "answers": [{"id": "a"}], "id": "r1"}
    assert ui_relay.pending_count() == 0


async def test_a_request_outlives_nobody():
    pi = StubPi()
    ui_relay.register(pi)
    task = asyncio.create_task(ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}))
    await asyncio.sleep(0)

    # A future left pending is a tool call that never returns, and a pi child that never settles: the
    # tab going away has to read as a cancel, not as silence.
    assert await ui_relay.unregister(pi) == 1
    assert await task == {"cancelled": True, "reason": "closed"}
    assert {"type": "ui_cancel", "id": "r1"} in pi.browser


async def test_stop_releases_the_dialog_without_killing_the_connection():
    pi = StubPi()
    ui_relay.register(pi)
    task = asyncio.create_task(ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}))
    await asyncio.sleep(0)
    await ui_relay.cancel(pi, reason="aborted")
    assert await task == {"cancelled": True, "reason": "aborted"}
    # Still registered: the run was cancelled, the tab did not go anywhere.
    assert ui_relay.connection_for_token(pi.preview_token) is pi


async def test_an_unanswered_request_is_released_on_a_clock():
    pi = StubPi()
    ui_relay.register(pi)
    result = await ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}, timeout=0.02)
    assert result == {"cancelled": True, "reason": "timeout"}
    assert {"type": "ui_cancel", "id": "r1"} in pi.browser
    assert ui_relay.pending_count() == 0


async def test_only_the_owner_is_cancelled():
    a, b = StubPi("a" * 32), StubPi("b" * 32)
    ui_relay.register(a)
    ui_relay.register(b)
    first = asyncio.create_task(ui_relay.ask(a, "questionnaire", {"id": "r1", "questions": []}))
    second = asyncio.create_task(ui_relay.ask(b, "questionnaire", {"id": "r2", "questions": []}))
    await asyncio.sleep(0)
    await ui_relay.cancel(a, reason="aborted")
    assert await first == {"cancelled": True, "reason": "aborted"}
    assert second.done() is False
    ui_relay.answer("r2", {"cancelled": False, "answers": []})
    assert (await second)["cancelled"] is False
    # b was asked its own question, and that is all it got: a's cancellation must not close
    # somebody else's dialog.
    assert [entry["type"] for entry in b.browser] == ["questionnaire_request"]


async def test_a_send_that_fails_leaves_nothing_pending():
    class DeadPi(StubPi):
        async def send_browser_json(self, payload):
            raise RuntimeError("browser is gone")

    pi = DeadPi()
    with pytest.raises(RuntimeError):
        await ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []})
    assert ui_relay.pending_count() == 0


def test_token_lookup_survives_a_respawn():
    pi = StubPi("c" * 32)
    ui_relay.register(pi)
    assert ui_relay.connection_for_token("c" * 32) is pi
    # `spawn()` mints a fresh token, which is why connections are keyed by process and the token is
    # compared at lookup time rather than cached at registration.
    pi.preview_token = "d" * 32
    assert ui_relay.connection_for_token("d" * 32) is pi
    assert ui_relay.connection_for_token("") is None
    assert ui_relay.connection_for_token("nope") is None


# ------------------------------------------------------------------ the captured wire

DIALOG_CAPTURE = (
    Path(__file__).resolve().parent.parent
    / "data-samples" / "questionnaire_dialog_rpc_capture.jsonl"
)


@pytest.mark.skipif(not DIALOG_CAPTURE.exists(), reason="the dialog capture is not in this checkout")
def test_the_committed_dialog_capture_still_derives():
    """Re-derive the browser messages from pi's real bytes, not from my memory of them.

    The fixture came from `tools/capture.py --scenario questionnaire_dialogs`: pi's own
    `extension_ui_request` traffic, including the fire-and-forget methods a client must ignore. If pi
    renames `prefill` or reshapes `select`, this fails here rather than as a dialog that silently never
    appears while a child blocks waiting for it.
    """
    events = [json.loads(line)["event"] for line in DIALOG_CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [event for event in events if event.get("type") == "extension_ui_request"]
    assert requests, "the capture holds no dialog traffic"

    dialogs = [payload for payload in (ui_relay.dialog_request_payload(event) for event in requests) if payload]
    assert [payload["method"] for payload in dialogs] == ["select", "confirm", "input", "editor"]

    # Everything else on that wire is a display hint: relayed as a dialog it would be a question nobody
    # asked, and answering it is not something pi would accept.
    ignored = [event for event in requests if ui_relay.dialog_request_payload(event) is None]
    assert {event["method"] for event in ignored} <= {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}
    assert ignored, "the capture has stopped showing the methods that must be ignored"

    # The `select` in the fixture carries a timeout, and the browser has to close on the same clock pi
    # resolves on.
    select = next(payload for payload in dialogs if payload["method"] == "select")
    assert select["timeoutMs"] > 0
    assert select["options"] == ["Allow", "Block", "Something else"]


# ------------------------------------------------------------------ the browser's commands

async def test_questionnaire_answer_command_settles_the_wait():
    pi = StubPi()
    ui_relay.register(pi)
    task = asyncio.create_task(ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}))
    await asyncio.sleep(0)
    await _dispatch_command(pi, "b", {
        "type": "questionnaire_answer", "id": "r1",
        "answers": [{"id": "scope", "value": "small"}],
    }, None, None)
    assert (await task)["answers"] == [{"id": "scope", "value": "small"}]


async def test_answering_a_stale_request_is_silent():
    pi = StubPi()
    await _dispatch_command(pi, "b", {"type": "questionnaire_answer", "id": "gone", "answers": []},
                            None, None)
    # Stop won the race. A "Command failed" toast for an answer nobody asked for is noise, and the
    # browser already closed the dialog.
    assert pi.browser == []


async def test_ui_response_reaches_the_child():
    pi = StubPi()
    await _dispatch_command(pi, "b", {"type": "ui_response", "id": "p1", "method": "confirm",
                                      "confirmed": True}, None, None)
    assert pi.sent == [{"type": "extension_ui_response", "id": "p1", "confirmed": True}]


async def test_a_dialog_answer_with_no_child_reports():
    pi = StubPi()
    pi.proc = None
    await _dispatch_command(pi, "b", {"type": "ui_response", "id": "p1", "method": "select",
                                      "value": "a"}, None, None)
    assert pi.browser[0]["type"] == "error"

    pi.proc = object()
    await _dispatch_command(pi, "b", {"type": "ui_response", "method": "select", "value": "a"},
                            None, None)
    assert pi.browser[-1]["type"] == "error"
    assert len(pi.sent) == 0


async def test_abort_releases_the_dialog_before_it_releases_the_run():
    pi = StubPi()
    ui_relay.register(pi)
    task = asyncio.create_task(ui_relay.ask(pi, "questionnaire", {"id": "r1", "questions": []}))
    await asyncio.sleep(0)
    await _dispatch_command(pi, "b", {"type": "abort"}, None, None)
    assert await task == {"cancelled": True, "reason": "aborted"}
    assert pi.sent == [{"type": "abort"}]


# ------------------------------------------------------------------ the child's route

@pytest.fixture
def app(tmp_path, monkeypatch):
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "DEV_MODE", False)
    return create_app(auth_manager=auth)


async def test_no_browser_means_the_question_cannot_be_asked(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        response = await http.post(
            "/api/questionnaire",
            headers={"x-preview-token": "f" * 32},
            json={"questions": [{"id": "a", "prompt": "p"}]},
        )
    # 410, not 404: the token was real once and the connection is simply gone, which is the difference
    # between "try later" and "nobody is going to answer this".
    assert response.status_code == 410
    assert "nobody can answer" in response.json()["detail"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.post("/api/questionnaire", json={"questions": []})).status_code == 410


async def test_a_malformed_questionnaire_is_refused_before_the_browser_sees_it(app):
    pi = StubPi()
    ui_relay.register(pi)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        response = await http.post(
            "/api/questionnaire",
            headers={"x-preview-token": pi.preview_token},
            json={"questions": [{"id": "a"}]},
        )
    assert response.status_code == 400
    assert pi.browser == []


async def test_the_route_waits_for_the_tab_and_answers_in_the_models_shape(app):
    pi = StubPi()
    ui_relay.register(pi)
    questions = [{"id": "method", "prompt": "Survey or interviews?", "options": [
        {"value": "survey", "label": "Survey"}, {"value": "interviews", "label": "Interviews"}]}]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        waiting = asyncio.create_task(http.post(
            "/api/questionnaire",
            headers={"x-preview-token": pi.preview_token},
            json={"questions": questions},
        ))
        await settle(browser=pi.browser)
        (request,) = pi.browser
        assert request["type"] == "questionnaire_request"
        # What the browser is handed is the filled-in form, not whatever the model wrote: the defaults
        # are applied once, server-side, so a terminal and a browser cannot render two different forms.
        assert request["questions"][0]["label"] == "Q1"
        assert request["questions"][0]["allowOther"] is True

        await _dispatch_command(pi, "b", {
            "type": "questionnaire_answer", "id": request["id"],
            "answers": [{"id": "method", "value": "interviews", "label": "Interviews"}],
        }, None, None)
        result = (await waiting).json()

    assert result["cancelled"] is False
    assert result["answers"] == [
        {"id": "method", "value": "interviews", "label": "Interviews", "wasCustom": False, "index": 2}
    ]


async def test_a_question_cancelled_by_stop_comes_back_cancelled(app):
    pi = StubPi()
    ui_relay.register(pi)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        waiting = asyncio.create_task(http.post(
            "/api/questionnaire",
            headers={"x-preview-token": pi.preview_token},
            json={"questions": [{"id": "a", "prompt": "p"}]},
        ))
        await settle(browser=pi.browser)
        await _dispatch_command(pi, "b", {"type": "abort"}, None, None)
        assert (await waiting).json() == {"cancelled": True, "reason": "aborted"}
