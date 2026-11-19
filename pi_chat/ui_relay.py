"""Asking the human something, from the child's side of the boundary.

pi's RPC mode gives an extension two ways to reach a person. One is documented and built in: a dialog
method (`select`, `confirm`, `input`, `editor`) becomes an `extension_ui_request` on stdout and blocks
until the client answers with an `extension_ui_response` on stdin (`pi/docs/rpc-extension-ui.md`). This
module relays those to the browser, which is what makes any extension able to ask a question here
rather than only in a terminal.

The other is the one the questionnaire needs, and pi has no surface for it: `ctx.ui.custom()` returns
`undefined` in RPC mode because a browser cannot host a terminal widget. That is the whole reason a
`questionnaire` tool fails in this application with `Cannot read properties of undefined (reading
'cancelled')` - it guards on `ctx.hasUI`, which RPC mode reports as true, and then calls the one method
that is not there.

So the rich, multi-question form travels on the channel `preview.ts` already opened: the child POSTs to
the server with the per-connection token, the server pushes a request down the browser socket and parks
an `asyncio.Future`, and the browser's answer resolves it. One transport for both shapes, because the
pending map, the cancellation, and the "the tab went away" path are the same problem twice.

What is *not* here is any opinion about what a question looks like. The browser decides that; this
module only moves a request and its answer, and guarantees that a request never outlive the connection
that was asked to answer it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("pi-chat")

# How long an unanswered request is allowed to hold a run open. A terminal waits forever, and a person
# who walked away mid-question expects it to still be there when they get back; a browser tab that was
# closed is a different thing, and its disconnect already tears the child down. This is the belt for
# the case between those two: a tab that is alive, visible, and being ignored.
DEFAULT_TIMEOUT_SECONDS = 15 * 60

# The pi dialog methods that expect an answer. The fire-and-forget ones (`notify`, `setStatus`,
# `setWidget`, `setTitle`, `set_editor_text`) are display hints a client may ignore, and none of them
# may be answered, so they are not relayed as dialogs.
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

# Requests the browser has been asked but has not answered, keyed by the id the browser sees.
PENDING: dict[str, "Pending"] = {}

# Live browser connections, keyed by process identity rather than by token: `spawn()` mints a new
# token, so a connection that starts a second session would otherwise be looking up a token nothing
# registered.
CONNECTIONS: dict[int, object] = {}


@dataclass
class Pending:
    """One question in flight."""

    id: str
    token: str
    kind: str
    future: "asyncio.Future[dict]"
    asked_at: float = field(default_factory=time.time)

    def settle(self, answer: dict) -> bool:
        if self.future.done():
            return False
        self.future.set_result(answer)
        return True


def register(pi) -> None:
    """Track a browser connection while it is open."""
    CONNECTIONS[id(pi)] = pi


async def unregister(pi, *, reason: str = "closed") -> int:
    """Forget a connection and release everything it was asked.

    An unanswered question has to resolve *somehow* when the tab goes: a future left pending is a tool
    call that never returns, and behind it a pi child that never settles. Cancelling with a reason is
    also what lets the extension say "the browser went away" instead of reporting that the person
    declined.
    """
    CONNECTIONS.pop(id(pi), None)
    return await cancel(pi, reason=reason)


def connection_for_token(token: str) -> object | None:
    """The connection whose child this token belongs to, or None.

    Linear on purpose: there are a handful of connections on this application, and comparing the token
    the child was spawned with is the only lookup that survives a respawn minting a new one.
    """
    if not token:
        return None
    for pi in CONNECTIONS.values():
        if getattr(pi, "preview_token", None) == token:
            return pi
    return None


async def ask(pi, kind: str, payload: dict, *, timeout: float | None = None) -> dict:
    """Push one request at the browser and wait for the answer.

    The send goes through `send_browser_json`, which is the one bounded outbox every browser write
    shares (invariant 8): a request that jumped the queue could arrive before the tool call that
    caused it, and a dialog that appears before its own transcript row reads as a glitch.
    """
    request_id = str(payload.get("id") or uuid.uuid4())
    loop = asyncio.get_running_loop()
    pending = Pending(
        id=request_id,
        token=getattr(pi, "preview_token", "") or "",
        kind=kind,
        future=loop.create_future(),
    )
    PENDING[request_id] = pending
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        await pi.send_browser_json({"type": f"{kind}_request", "id": request_id, **{
            key: value for key, value in payload.items() if key != "id"
        }})
    except Exception:
        PENDING.pop(request_id, None)
        raise

    try:
        if limit and limit > 0:
            return await asyncio.wait_for(pending.future, timeout=limit)
        return await pending.future
    except asyncio.TimeoutError:
        log.warning(
            "a %s request sat unanswered for %.0fs; releasing it as a cancel",
            kind,
            limit,
        )
        await notify_cancel(pi, request_id)
        return {"cancelled": True, "reason": "timeout"}
    finally:
        PENDING.pop(request_id, None)


def answer(request_id: str, payload: dict) -> bool:
    """Resolve a pending request with the browser's answer. False means nobody is waiting."""
    pending = PENDING.get(request_id)
    if pending is None:
        # Normal and expected: the person pressed Stop a moment before they clicked. It is a debug
        # line rather than a warning because the two events race on a fast double-click.
        log.debug("answer for unknown or settled request %s", request_id)
        return False
    settled = pending.settle({**payload, "id": request_id})
    if settled:
        PENDING.pop(request_id, None)
    return settled


async def notify_cancel(pi, request_id: str) -> None:
    """Tell the browser to close a dialog the server has already given up on."""
    try:
        await pi.send_browser_json({"type": "ui_cancel", "id": request_id})
    except Exception as error:  # a connection mid-teardown cannot hear anything
        log.debug("could not cancel request %s: %s", request_id, error)


async def cancel(pi, *, reason: str) -> int:
    """Release every request this connection was asked, and close their dialogs.

    Called when the person presses Stop, when the tab goes, and when the child dies. Every path has to
    end the same way: the tool returns, the run settles, and no half-answered dialog is left on screen
    claiming it is still waiting.
    """
    token = getattr(pi, "preview_token", None)
    targets = [p for p in PENDING.values() if not token or p.token == token]
    for pending in targets:
        if pending.settle({"cancelled": True, "reason": reason}):
            await notify_cancel(pi, pending.id)
    return len(targets)


def pending_count() -> int:
    return len(PENDING)


# ---------------------------------------------------------------------------
# the questionnaire's shape, checked on the way in
# ---------------------------------------------------------------------------

# The tool's own schema (`@that-yolanda/pi-questionnaire`, mirrored by pi's example extension) is
# permissive because a model writes it. This is the same shape arriving over HTTP from a child process,
# and a browser is about to render it, so the limits live here: a runaway model that asks for nine
# hundred questions gets a tool error, not a tab that stops responding.
MAX_QUESTIONS = 12
MAX_OPTIONS = 40
MAX_TEXT = 1200


class QuestionError(ValueError):
    """A questionnaire the browser should not be asked to render. Becomes a 400 and a tool error."""


def _text(value: object, *, field: str, limit: int = MAX_TEXT, required: bool = False) -> str:
    """Read one string field. Absent is only allowed where the field is genuinely optional.

    The distinction matters in both directions: a model that omits `label` means "call it Q2", while a
    model that sends `label: 42` has produced something no browser should try to render, and silently
    coercing that is how a form full of `42` reaches a person.
    """
    if value is None:
        if required:
            raise QuestionError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise QuestionError(f"{field} must be a string")
    return value.strip()[:limit]


def normalise_questions(body: object) -> list[dict]:
    """Validate and fill in one questionnaire, in the shape the browser renders.

    Defaults match the extension's, on purpose: `allowOther` is on unless the model turned it off, and
    a question with no label is called Q1, Q2. If the two ever disagree, the model gets a different
    form depending on which client asked, which is a bad way to discover it.
    """
    raw = body.get("questions") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw:
        raise QuestionError("questions must be a non-empty array")
    if len(raw) > MAX_QUESTIONS:
        raise QuestionError(f"too many questions: {len(raw)} over the limit of {MAX_QUESTIONS}")

    questions: list[dict] = []
    seen_ids: set[str] = set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise QuestionError(f"question {position} must be an object")
        question_id = _text(item.get("id"), field=f"question {position} id", limit=120, required=True)
        if not question_id:
            raise QuestionError(f"question {position} needs an id")
        if question_id in seen_ids:
            raise QuestionError(f"duplicate question id: {question_id}")
        seen_ids.add(question_id)

        prompt = _text(item.get("prompt"), field=f"question {question_id} prompt", required=True)
        if not prompt:
            raise QuestionError(f"question {question_id} needs a prompt")
        label = _text(item.get("label"), field=f"question {question_id} label", limit=80) or f"Q{position}"

        raw_options = item.get("options")
        if raw_options is None:
            raw_options = []
        if not isinstance(raw_options, list):
            raise QuestionError(f"question {question_id} options must be an array")
        if len(raw_options) > MAX_OPTIONS:
            raise QuestionError(
                f"question {question_id} has {len(raw_options)} options, over the limit of {MAX_OPTIONS}"
            )

        options: list[dict] = []
        for option_position, option in enumerate(raw_options, start=1):
            if not isinstance(option, dict):
                raise QuestionError(f"question {question_id} option {option_position} must be an object")
            value = _text(option.get("value"), field=f"question {question_id} option value", limit=400,
                          required=True)
            if not value:
                raise QuestionError(f"question {question_id} option {option_position} needs a value")
            entry = {
                "value": value,
                "label": _text(option.get("label"), field=f"question {question_id} option label", limit=400)
                or value,
            }
            description = option.get("description")
            if isinstance(description, str) and description.strip():
                entry["description"] = description.strip()[:MAX_TEXT]
            options.append(entry)

        questions.append({
            "id": question_id,
            "label": label,
            "prompt": prompt,
            "options": options,
            "allowOther": item.get("allowOther") is not False,
            "multiSelect": item.get("multiSelect") is True,
        })

    return questions


def normalise_answers(questions: list[dict], raw: object) -> list[dict]:
    """Filter an answer set down to answers for the questions that were actually asked.

    The browser is a client like any other: it may be an older tab, or a hand-rolled one. An answer
    whose id is not on the form is dropped rather than trusted, because it would otherwise be handed to
    the model as something the person chose.
    """
    by_id = {question["id"]: question for question in questions}
    if not isinstance(raw, list):
        return []
    answers: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = by_id.get(str(item.get("id") or ""))
        if question is None:
            continue
        answer = _normalise_answer(question, item)
        if answer is not None:
            # One answer per question, last one wins: an answer form can be resubmitted, and the
            # second submission is the one the person meant.
            answers[question["id"]] = answer
    # Order is the order they were asked, not the order they arrived: the text handed to the model
    # reads as a form, and a form that changes its order per reload does not read as anything.
    return [answers[question["id"]] for question in questions if question["id"] in answers]


def _normalise_answer(question: dict, item: dict) -> "dict | None":
    question_id = question["id"]
    if question["multiSelect"]:
        selections = _normalise_selections(question, item.get("selections"))
        if not selections:
            return None
        return {"id": question_id, "selections": selections, "label": ", ".join(
            str(entry.get("label") or "") for entry in selections
        )}

    if "value" not in item:
        return None
    value = _text(item.get("value"), field="answer value", limit=2000)
    known = next((option for option in question["options"] if option["value"] == value), None)
    was_custom = known is None
    if was_custom and not question["allowOther"]:
        # Free text on a closed list is not an answer; it is a client making one up.
        return None
    label = _text(item.get("label"), field="answer label", limit=2000) or (known or {}).get("label") or value
    index = next(
        (position for position, option in enumerate(question["options"], start=1) if option["value"] == value),
        None,
    )
    answer = {
        "id": question_id,
        "value": value,
        "label": label,
        # `wasCustom` is the difference between "picked the second option" and "wrote this sentence",
        # and the model is told both. Losing it would quietly misreport what the person did.
        "wasCustom": was_custom,
    }
    if index is not None:
        answer["index"] = index
    return answer


def _normalise_selections(question: dict, raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    known = {option["value"]: (position, option)
             for position, option in enumerate(question["options"], start=1)}
    selections: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = _text(item.get("value"), field="selection value", limit=2000, required=True)
        if not value:
            continue
        match = known.get(value)
        was_custom = match is None
        if was_custom and not question["allowOther"]:
            continue
        entry = {
            "value": value,
            "label": _text(item.get("label"), field="selection label", limit=2000)
            or (match[1].get("label") if match else None) or value,
            "wasCustom": was_custom,
        }
        if match is not None:
            entry["index"] = match[0]
        selections.append(entry)
    return selections


# ---------------------------------------------------------------------------
# pi's own dialogs, turned into something a browser can render
# ---------------------------------------------------------------------------

def dialog_request_payload(event: dict) -> "dict | None":
    """Turn an `extension_ui_request` into the message the browser renders, or None to ignore it.

    pi already puts these on stdout and this application already forwards every event to the browser as
    `pi_event`, so an extension's `confirm` could in principle be parsed out of the raw stream. It is
    derived here instead for the reason the protocol document states twice: the browser must not trust
    pi's shape. A named message with the fields it needs is a contract; a renderer that digs through
    another program's event payloads breaks the day that program renames a field, and it breaks
    silently, because the dialog simply never appears and the child waits forever.

    pi's own request `id` is carried through unchanged, because the response has to name it.
    """
    if event.get("type") != "extension_ui_request":
        return None
    method = event.get("method")
    if method not in DIALOG_METHODS:
        return None
    request_id = event.get("id")
    if not isinstance(request_id, str) or not request_id:
        log.warning("a %s dialog arrived with no id; nothing could answer it", method)
        return None

    payload: dict = {"type": "ui_request", "id": request_id, "method": method}
    for field, key in (("title", "title"), ("message", "message"), ("placeholder", "placeholder"),
                       # `editor` arrives with the text to edit and no message at all (captured
                       # 2026-11-19: `{"method":"editor","title":"Fix the title","prefill":"..."}`), so
                       # dropping this field would hand the browser an empty box to "edit".
                       ("prefill", "prefill")):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            payload[field] = value[:4000]
    if method == "select":
        options = event.get("options")
        if not isinstance(options, list):
            options = []
        payload["options"] = [option[:4000] for option in options[:200] if isinstance(option, str)]
    timeout = event.get("timeout")
    if isinstance(timeout, (int, float)) and timeout > 0:
        # pi resolves the tool itself when this expires. The browser has to close the dialog on the
        # same clock, or the person is left choosing between two options the model has already given up
        # on - and an answer that arrives after pi moved on is worse than no answer.
        payload["timeoutMs"] = int(timeout)
    return payload


def dialog_response(payload: dict) -> "dict | None":
    """Turn the browser's answer into the `extension_ui_response` pi is waiting for, or None.

    The three shapes are exactly pi's (`pi/docs/rpc-extension-ui.md`): `value` for select/input/editor,
    `confirmed` for confirm, and `cancelled` for any of them. Anything else is dropped rather than
    forwarded, because a malformed response leaves pi waiting on a dialog nobody will answer.
    """
    request_id = payload.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if payload.get("cancelled") is True:
        return {"type": "extension_ui_response", "id": request_id, "cancelled": True}
    method = payload.get("method")
    if method == "confirm":
        confirmed = payload.get("confirmed")
        if not isinstance(confirmed, bool):
            return None
        return {"type": "extension_ui_response", "id": request_id, "confirmed": confirmed}
    if method in {"select", "input", "editor"}:
        value = payload.get("value")
        if not isinstance(value, str):
            return None
        return {"type": "extension_ui_response", "id": request_id, "value": value}
    return None
