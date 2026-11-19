/**
 * A question from the child process, put in front of the person in front of this tab.
 *
 * Two shapes arrive here. `questionnaire_request` is the rich form - several questions, each with its
 * own options, single- or multi-select, and a free-text escape hatch - which pi cannot deliver over RPC
 * at all: `ctx.ui.custom()` returns `undefined` in RPC mode, so the extension that asks it needs this
 * route. `ui_request` is pi's own dialog set (`select`, `confirm`, `input`, `editor`), which does have a
 * wire format and only needs somebody on the other end of it.
 *
 * Three things govern everything below.
 *
 * 1. The questions are model output. They are untrusted input like any other text the assistant
 *    produces, so every word is written with `textContent` and nothing is built with `innerHTML`. A
 *    question that wants to look like a button gets to be a sentence instead.
 * 2. An unanswered question is a stuck run. pi blocks the tool until the answer comes back, so this
 *    component must always be able to end: Cancel answers, Escape answers, a run that goes away is told
 *    to close by the server (`ui_cancel`), and a dialog with a deadline says so and closes itself. The
 *    one thing it never does is vanish while still pending.
 * 3. The server re-reads everything the browser sends (`pi_chat/ui_relay.py`). So this file aims to send
 *    correct answers, and the server assumes it did not.
 */

/* Not a value any model would write. It is the sentinel for "the person typed their own answer", and it
   has to survive being compared against an option value that came from a model, so it is not a string
   that could plausibly appear in a list of options. */
const CUSTOM = '\u0000other';

const OTHER_LABEL = 'Type something';

const GENERIC_TITLES = {
  select: 'Choose one',
  confirm: 'Are you sure?',
  input: 'A short answer is wanted',
  editor: 'Edit the text',
};

const GENERIC_ACTIONS = { select: 'Choose', input: 'Send', editor: 'Send' };

const GENERIC_KINDS = new Set(Object.keys(GENERIC_TITLES));

export function createUiDialog({ sendCommand, onAudit = () => {} } = {}) {
  const overlay = document.getElementById('ui-question-overlay');
  const dialog = document.getElementById('ui-question-dialog');
  const title = dialog?.querySelector('[data-ui-question-title]');
  const counter = dialog?.querySelector('[data-ui-question-counter]');
  const tabs = dialog?.querySelector('[data-ui-question-tabs]');
  const prompt = dialog?.querySelector('[data-ui-question-prompt]');
  const options = dialog?.querySelector('[data-ui-question-options]');
  const note = dialog?.querySelector('[data-ui-question-note]');
  const cancelBtn = dialog?.querySelector('[data-ui-question-cancel]');
  const actionBtn = dialog?.querySelector('[data-ui-question-action]');

  if (!overlay || !dialog) {
    // The markup lives in index.html, and a build that lost it would otherwise mean a question arrives,
    // nothing appears, and the run waits out the relay's fifteen minutes in silence.
    console.error('[ui-dialog] markup missing; a child asking a question would be left waiting');
    const noop = () => {};
    return { openQuestionnaire: noop, openDialog: noop, withdrawn: noop, isOpen: () => false, openId: () => null };
  }

  /** The request being answered: id, kind, the questions, per-question state, and a deadline. */
  let current = null;
  let opener = null;
  let ticker = null;

  // ----------------------------------------------------------------------- opening

  function begin({ id, kind, questions, title: heading, timeoutMs }) {
    // A second question while one is open means the first is already gone: one child, one run, and pi
    // would not ask twice with the first still blocked. Replace it, and leave a console line rather
    // than letting a form disappear without a trace.
    if (current) console.warn('[ui-dialog] an unanswered request was replaced by a new one');
    stopTicker();
    opener = document.activeElement;
    current = {
      id,
      kind,
      questions,
      title: heading || (kind === 'questionnaire' ? 'Questions' : GENERIC_TITLES[kind]),
      index: 0,
      answers: new Map(),
      deadline: Number.isFinite(timeoutMs) && timeoutMs > 0 ? Date.now() + timeoutMs : null,
    };
    overlay.hidden = false;
    document.body.classList.add('ui-question-open');
    dialog.dataset.uiQuestionKind = kind;
    render();
    startTicker();
    onAudit('open', { kind, questions: questions.length, id });
  }

  function openQuestionnaire(payload) {
    const questions = (Array.isArray(payload.questions) ? payload.questions : [])
      .map(normaliseQuestion)
      .filter((question) => question.id);
    if (!questions.length) return withdrawn(payload.id);
    begin({ id: payload.id, kind: 'questionnaire', questions });
  }

  function openDialog(payload) {
    if (!payload?.id || !GENERIC_KINDS.has(payload.method)) return;
    begin({
      id: payload.id,
      kind: payload.method,
      title: payload.title || GENERIC_TITLES[payload.method],
      timeoutMs: payload.timeoutMs,
      questions: [{
        id: payload.id,
        label: '',
        prompt: payload.message || payload.title || 'A question',
        // pi's `select` sends bare strings; the form's options carry labels. One shape from here on.
        options: (Array.isArray(payload.options) ? payload.options : []).map((value) => ({
          value: String(value), label: String(value), description: '',
        })),
        allowOther: false,
        multiSelect: false,
        placeholder: typeof payload.placeholder === 'string' ? payload.placeholder : '',
        // What an `editor` request is asking to have edited. Without it the box is empty and the
        // request reads as an `input` with a strange title.
        prefill: typeof payload.prefill === 'string' ? payload.prefill : '',
        lines: payload.method === 'editor' ? 10 : 2,
      }],
    });
  }

  function normaliseQuestion(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      id: String(source.id ?? ''),
      label: String(source.label ?? ''),
      prompt: String(source.prompt ?? ''),
      options: (Array.isArray(source.options) ? source.options : [])
        .map((option) => ({
          value: String(option?.value ?? ''),
          label: String(option?.label ?? option?.value ?? ''),
          description: String(option?.description ?? ''),
        }))
        .filter((option) => option.value),
      allowOther: source.allowOther !== false,
      multiSelect: source.multiSelect === true,
      placeholder: '',
      prefill: '',
      lines: 2,
    };
  }

  function stateFor(question) {
    if (!current.answers.has(question.id)) {
      // An `editor` starts with its text already there, so it starts ready to send: a box holding the
      // thing you were asked to edit, with a dead Send button, is a form that contradicts itself.
      current.answers.set(question.id, question.multiSelect
        ? { picked: new Set() }
        : { picked: null, text: question.prefill || '' });
    }
    return current.answers.get(question.id);
  }

  // ----------------------------------------------------------------------- rendering

  function render() {
    if (!current) return;
    const question = current.questions[current.index];
    const many = current.kind === 'questionnaire' && current.questions.length > 1;

    title.textContent = current.title;
    counter.hidden = !many;
    counter.textContent = many ? `${current.index + 1} of ${current.questions.length}` : '';

    renderTabs(question, many);
    renderPrompt(question);
    if (current.kind === 'confirm') renderConfirm();
    else if (current.kind === 'select') renderChoices(question);
    else renderOptions(question);
    renderNote();
    renderActions();
  }

  function renderTabs(question, many) {
    tabs.replaceChildren();
    tabs.hidden = !many;
    if (!many) return;

    current.questions.forEach((entry, position) => {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'ui-question-tab';
      tab.setAttribute('role', 'tab');
      tab.textContent = entry.label || `Q${position + 1}`;
      const answered = isAnswered(entry);
      tab.dataset.answer = answered ? 'yes' : 'no';
      tab.setAttribute('aria-selected', String(entry === question));
      tab.tabIndex = entry === question ? 0 : -1;
      // The tab says which question it is; this says which ones still want an answer, which is what a
      // reader scanning the strip actually needs.
      tab.setAttribute('aria-label', `${entry.label || `Question ${position + 1}`}${answered ? ', answered' : ', unanswered'}`);
      tab.addEventListener('click', () => {
        current.index = position;
        render();
      });
      tabs.append(tab);
    });
  }

  function renderPrompt(question) {
    prompt.replaceChildren();
    const text = document.createElement('span');
    text.textContent = question.prompt;
    prompt.append(text);
    if (question.multiSelect) {
      const hint = document.createElement('span');
      hint.className = 'ui-question-hint';
      hint.textContent = 'Choose any that apply';
      prompt.append(hint);
    }
  }

  /** `select`: one click answers. There is nothing to review before committing a single choice. */
  function renderChoices(question) {
    options.replaceChildren();
    if (!question.options.length) {
      const empty = document.createElement('p');
      empty.className = 'ui-question-option-description';
      empty.textContent = 'Nothing was offered to choose from.';
      options.append(empty);
      return;
    }
    question.options.forEach((option, position) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ui-question-choice';
      const number = document.createElement('span');
      number.className = 'ui-question-option-number';
      number.textContent = String(position + 1);
      const label = document.createElement('span');
      label.textContent = option.label;
      row.append(number, label);
      row.addEventListener('click', () => respond({ value: option.value }));
      options.append(row);
    });
  }

  function renderConfirm() {
    options.replaceChildren();
    [['Yes', true], ['No', false]].forEach(([label, confirmed]) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ui-question-choice';
      row.dataset.uiQuestionAnswer = confirmed ? 'yes' : 'no';
      const spacer = document.createElement('span');
      spacer.className = 'ui-question-option-number';
      spacer.textContent = confirmed ? '\u2713' : '\u2715';
      const text = document.createElement('span');
      text.textContent = label;
      row.append(spacer, text);
      row.addEventListener('click', () => respond({ confirmed }));
      options.append(row);
    });
  }

  /** The questionnaire's option list, and the free-text field for `input`/`editor`. */
  function renderOptions(question) {
    options.replaceChildren();
    const state = stateFor(question);
    const isField = current.kind === 'input' || current.kind === 'editor';

    if (!isField) {
      const group = document.createElement('div');
      group.className = 'ui-question-group';
      group.setAttribute('role', question.multiSelect ? 'group' : 'radiogroup');
      group.setAttribute('aria-label', question.prompt || 'Answers');
      question.options.forEach((option, position) => {
        group.append(optionRow(question, state, {
          value: option.value, label: option.label, description: option.description, index: position + 1,
        }));
      });
      if (question.allowOther) {
        group.append(optionRow(question, state, { value: CUSTOM, label: OTHER_LABEL, custom: true }));
      }
      options.append(group);
    }

    if (isField || needsFreeText(question)) options.append(freeTextRow(question, state));
  }

  /* Real inputs rather than ARIA pretend-widgets: arrow keys, space, and a screen reader's idea of a
     radio group all come free, and a hand-rolled version of this is always slightly broken. */
  function optionRow(question, state, option) {
    const row = document.createElement('label');
    row.className = 'ui-question-option';

    const input = document.createElement('input');
    input.type = question.multiSelect ? 'checkbox' : 'radio';
    input.name = `ui-question-${question.id}`;
    input.value = option.value;
    input.checked = question.multiSelect
      ? state.picked.has(option.value)
      : state.picked === option.value;
    input.addEventListener('change', () => {
      if (question.multiSelect) {
        if (input.checked) state.picked.add(option.value);
        else state.picked.delete(option.value);
      } else {
        state.picked = option.value;
      }
      render();
      if (option.custom) focusFreeText();
    });
    row.append(input);

    const body = document.createElement('span');
    body.className = 'ui-question-option-body';
    const label = document.createElement('span');
    label.className = 'ui-question-option-label';
    if (option.index) {
      const number = document.createElement('span');
      number.className = 'ui-question-option-number';
      number.textContent = String(option.index);
      label.append(number);
    }
    label.append(document.createTextNode(option.label));
    body.append(label);
    if (option.description) {
      const description = document.createElement('span');
      description.className = 'ui-question-option-description';
      description.textContent = option.description;
      body.append(description);
    }
    row.append(body);
    return row;
  }

  function freeTextRow(question, state) {
    const wrap = document.createElement('div');
    wrap.className = 'ui-question-free';

    const field = document.createElement('textarea');
    field.rows = question.lines || 2;
    field.maxLength = 2000;
    field.placeholder = question.placeholder || 'Your answer';
    field.setAttribute('aria-label', 'Type your answer');
    field.value = typeof state.text === 'string' ? state.text : (question.prefill || '');
    field.addEventListener('input', () => {
      state.text = field.value;
      actionBtn.disabled = !readyToSend();
      renderNote();
    });
    field.addEventListener('keydown', (event) => {
      // Ctrl/Cmd+Enter sends. Plain Enter is a newline, which is what it means in every other text box
      // in this application, and an answer that is a sentence needs one.
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendCurrent();
      }
    });
    wrap.append(field);
    // A question with nothing to pick from is a question that wants typing, so the caret goes where the
    // answer goes instead of waiting for a click.
    if (current.kind === 'input' || current.kind === 'editor') queueMicrotask(() => field.focus());
    return wrap;
  }

  function focusFreeText() {
    const field = options.querySelector('textarea');
    if (field && document.activeElement !== field) field.focus();
  }

  function renderNote() {
    note.replaceChildren();
    if (!current) return;
    const parts = [];
    if (current.kind === 'questionnaire') {
      const missing = current.questions.filter((entry) => !isAnswered(entry)).map(labelOf);
      if (missing.length) parts.push(`Still needs: ${missing.join(', ')}`);
    }
    if (current.deadline) {
      // Rounded up, so a deadline 400 ms away says "1s" instead of announcing it has already stopped:
      // `Math.round` made the last second of every countdown read as a miss.
      const left = Math.max(0, Math.ceil((current.deadline - Date.now()) / 1000));
      parts.push(left > 0 ? `The assistant stops waiting in ${left}s.` : 'The assistant stopped waiting.');
    }
    note.hidden = parts.length === 0;
    parts.forEach((part) => {
      const line = document.createElement('span');
      line.textContent = part;
      note.append(line);
    });
  }

  function renderActions() {
    cancelBtn.textContent = 'Cancel';
    cancelBtn.hidden = false;
    if (current.kind === 'questionnaire') {
      actionBtn.hidden = false;
      actionBtn.textContent = 'Submit answers';
    } else if (current.kind === 'confirm') {
      // Yes and No are the answers, and they are in the body. A third button meaning "no" twice would
      // be a coin flip.
      actionBtn.hidden = true;
    } else {
      actionBtn.hidden = false;
      actionBtn.textContent = GENERIC_ACTIONS[current.kind] || 'Send';
    }
    actionBtn.disabled = !readyToSend();
  }

  // ----------------------------------------------------------------------- answering

  function labelOf(question) {
    return question.label || question.prompt.slice(0, 40) || 'Untitled';
  }

  function needsFreeText(question) {
    const state = stateFor(question);
    return question.multiSelect ? state.picked.has(CUSTOM) : state.picked === CUSTOM;
  }

  function isAnswered(question) {
    const state = stateFor(question);
    if (question.multiSelect) {
      if (!state.picked.size) return false;
      return !state.picked.has(CUSTOM) || Boolean(textOf(state));
    }
    if (state.picked == null) return false;
    return state.picked === CUSTOM ? Boolean(textOf(state)) : true;
  }

  function readyToSend() {
    if (!current) return false;
    if (current.kind === 'questionnaire') return current.questions.every(isAnswered);
    const state = stateFor(current.questions[0]);
    return Boolean(textOf(state)) || (state.picked != null && state.picked !== CUSTOM && state.picked !== '');
  }

  function textOf(state) {
    return String(state.text || '').trim();
  }

  /** The answer for one question, in the shape the tool reports to the model. */
  function answerFor(question) {
    const state = stateFor(question);

    if (question.multiSelect) {
      // Option order, free text last: the same ticks have to read the same way twice, and the order the
      // boxes were clicked in is not information anybody asked for.
      const selections = question.options
        .map((option, position) => ({ option, position }))
        .filter(({ option }) => state.picked.has(option.value))
        .map(({ option, position }) => ({
          value: option.value, label: option.label, index: position + 1, wasCustom: false,
        }));
      if (state.picked.has(CUSTOM)) {
        selections.push({ value: textOf(state), label: textOf(state), wasCustom: true });
      }
      return { id: question.id, selections };
    }

    if (state.picked === CUSTOM) {
      return { id: question.id, value: textOf(state), label: textOf(state), wasCustom: true };
    }
    const position = question.options.findIndex((option) => option.value === state.picked);
    return {
      id: question.id,
      value: state.picked,
      label: position >= 0 ? question.options[position].label : String(state.picked),
      index: position >= 0 ? position + 1 : undefined,
      wasCustom: false,
    };
  }

  /** Everything the child needs, in one place: the answer, then close. */
  function respond({ value, confirmed }) {
    if (!current) return;
    const { id, kind } = current;
    if (kind === 'questionnaire') {
      sendCommand({ type: 'questionnaire_answer', id, answers: current.questions.map(answerFor) });
      onAudit('answered', { id, questions: current.questions.length });
    } else if (confirmed !== undefined) {
      sendCommand({ type: 'ui_response', id, method: kind, confirmed });
      onAudit('answered', { id, method: kind, confirmed });
    } else {
      sendCommand({ type: 'ui_response', id, method: kind, value: String(value ?? '') });
      onAudit('answered', { id, method: kind });
    }
    finish();
  }

  function sendCurrent() {
    if (!current || !readyToSend()) return;
    if (current.kind === 'questionnaire') {
      respond({});
      return;
    }
    const state = stateFor(current.questions[0]);
    respond({ value: state.picked === CUSTOM || state.picked == null ? textOf(state) : state.picked });
  }

  function cancel() {
    if (!current) return;
    const { id, kind } = current;
    sendCommand(kind === 'questionnaire'
      ? { type: 'questionnaire_answer', id, cancelled: true }
      : { type: 'ui_response', id, method: kind, cancelled: true });
    onAudit('cancelled', { id, method: kind });
    finish();
  }

  function finish() {
    stopTicker();
    current = null;
    overlay.hidden = true;
    document.body.classList.remove('ui-question-open');
    if (opener?.isConnected && typeof opener.focus === 'function') opener.focus();
    opener = null;
  }

  /** The server already released this request (Stop, disconnect, timeout), so there is nothing to answer. */
  function withdrawn(id) {
    if (!current || (id && current.id !== id)) return false;
    onAudit('withdrawn', { id: current.id });
    finish();
    return true;
  }

  // ----------------------------------------------------------------------- the clock

  function startTicker() {
    if (!current?.deadline) return;
    ticker = setInterval(() => {
      if (!current) return stopTicker();
      if (Date.now() >= current.deadline) {
        // pi resolves the tool itself when its own timeout expires, so an answer sent now would be an
        // answer to a run that has moved on. Close, and say nothing to the child.
        onAudit('expired', { id: current.id });
        finish();
        return;
      }
      renderNote();
    }, 250);
  }

  function stopTicker() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  // ----------------------------------------------------------------------- wiring

  cancelBtn.addEventListener('click', cancel);
  actionBtn.addEventListener('click', sendCurrent);

  overlay.addEventListener('click', (event) => {
    if (event.target !== overlay) return;
    // The backdrop means "get this out of my way", which for a dismissible detail view is a close and
    // for a blocking question is a refusal. It is treated as a refusal, on purpose: the alternative is a
    // dialog that vanishes while the run still waits for it, which is the failure this exists to avoid.
    cancel();
  });

  document.addEventListener('keydown', (event) => {
    if (!current || overlay.hidden) return;
    if (event.key === 'Escape') {
      event.stopPropagation();
      event.preventDefault();
      cancel();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      sendCurrent();
      return;
    }
    if (current.questions.length > 1 && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
      // Only when focus is not in a text box, where those keys belong to the caret.
      const tag = document.activeElement?.tagName;
      if (tag === 'TEXTAREA' || tag === 'INPUT') return;
      event.preventDefault();
      const step = event.key === 'ArrowRight' ? 1 : -1;
      current.index = (current.index + step + current.questions.length) % current.questions.length;
      render();
    }
  });

  return {
    openQuestionnaire,
    openDialog,
    withdrawn,
    isOpen: (id) => Boolean(current) && (!id || current.id === id),
    openId: () => current?.id || null,
    /** Close without answering: for a run that was replaced under an open dialog. */
    discard: () => { if (current) { onAudit('discarded', { id: current.id }); finish(); } },
    /** Test seam: the pending request, so a harness can assert state without clicking. */
    debugState: () => (current ? { id: current.id, kind: current.kind, index: current.index } : null),
  };
}
